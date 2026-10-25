"""
Клиент REST API панели 3x-ui, построенный на headless-браузере (Playwright).

Почему браузер, а не requests/curl_cffi: изначально казалось, что дело в
защите от ботов (CSRF/TLS-отпечаток), поэтому весь обмен идёт через настоящий
Chromium. По факту финальная причина была проще - панель оказалась v3 с новой
JSON-схемой запросов (client + inboundIds вместо старой id + settings-строка),
и без браузера мы бы всё равно не увидели этого через обычные попытки угадать
формат. Раз код уже написан под браузер и он рабочий - оставляем как есть,
переписывать на чистый requests сейчас не требуется.

Все методы асинхронные (async/await), так как Playwright работает через
asyncio - это совместимо с discord.py, который тоже асинхронный.

Использование:
    xui = await XUIClient.create(base_url, username, password, ...)
    ...
    await xui.close()   # при остановке бота, если нужно
"""

import asyncio
import json
import logging
import re
import time

from playwright.async_api import async_playwright


log = logging.getLogger("xui")


class XUIError(RuntimeError):
    pass


class XUIClientNotFound(XUIError):
    """Клиент с таким email в панели не найден."""


class XUIClient:
    def __init__(self):
        # Не создавайте напрямую - используйте await XUIClient.create(...)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.base_url = None
        self.username = None
        self.password = None
        self.timeout_ms = 20000
        self.login_debug_info = ""
        self._csrf_token = None
        self._static_csrf_token = None
        self._verify_ssl = True
        # Браузерная вкладка одна, поэтому все операции с панелью выполняются
        # строго по очереди - иначе два одновременных события (например, двум
        # людям выдали роль) начинают кликать по одной и той же странице.
        self._lock = asyncio.Lock()

    @classmethod
    async def create(cls, base_url: str, username: str, password: str, timeout: int = 20,
                      verify_ssl: bool = True, web_base_path: str = "", static_csrf_token: str = ""):
        self = cls()

        host = base_url.rstrip("/")
        if host.lower().endswith("/login"):
            host = host[: -len("/login")]
        path = web_base_path.strip("/")
        if path:
            host = f"{host}/{path}"

        self.base_url = host
        self.username = username
        self.password = password
        self.timeout_ms = timeout * 1000
        # Если задан явно (обнаружено, что панель использует статичный токен,
        # одинаковый для всех сессий, а не выдаваемый сервером динамически) -
        # используем его сразу и не пытаемся ничего "поймать" автоматически.
        self._static_csrf_token = static_csrf_token or None

        self._verify_ssl = verify_ssl
        if self._static_csrf_token:
            self._csrf_token = self._static_csrf_token

        await self._start_browser()
        await self._login()
        await self._park_page()
        return self

    async def _start_browser(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            ignore_https_errors=not self._verify_ssl,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )
        # Скрываем самые очевидные признаки автоматизации ДО загрузки любой страницы.
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """)
        self._page = await self._context.new_page()

        # Панель может отдавать CSRF-токен в заголовке ответа - запоминаем его,
        # как только он где-то появится (если static_csrf_token не задан).
        def _on_response(response):
            if self._static_csrf_token:
                return
            headers = response.headers
            for key in ("x-csrf-token", "csrf-token", "x-csrf token"):
                val = headers.get(key)
                if val:
                    self._csrf_token = val

        self._page.on("response", _on_response)

    async def _restart_browser(self):
        log.warning("Перезапускаю headless-браузер")
        await self.close()
        await self._start_browser()
        await self._login()

    def _browser_alive(self) -> bool:
        try:
            return bool(self._browser and self._browser.is_connected()
                        and self._page and not self._page.is_closed())
        except Exception:
            return False

    # Лёгкая страница того же origin, что и панель: обычный JSON-ответ без JS
    # и графиков. На ней вкладка «стоит» между операциями - так fetch() из
    # _api_call продолжает работать (cookie и origin те же), а headless-Chromium
    # не перерисовывает живые графики дашборда и не грузит CPU.
    IDLE_PAGE_PATH = "/panel/api/inbounds/getClientTraffics/__idle__"

    def _on_idle_page(self) -> bool:
        try:
            return bool(self._page) and self._page.url.startswith(
                f"{self.base_url}{self.IDLE_PAGE_PATH}")
        except Exception:
            return False

    async def _park_page(self):
        """Уводит вкладку с тяжёлых страниц панели (дашборд, Clients) на
        лёгкую страницу-заглушку. Ошибки не критичны - просто логируем."""
        if not self._browser_alive() or self._on_idle_page():
            return
        # Если токен был только в <meta> текущей страницы - запоминаем его,
        # пока мы ещё на ней (на JSON-странице meta уже не будет).
        if not self._csrf_token:
            try:
                meta = await self._page.evaluate(
                    "() => { const m = document.querySelector('meta[name=\"csrf-token\"]');"
                    " return m ? m.getAttribute('content') : null; }"
                )
                if meta:
                    self._csrf_token = meta
            except Exception:
                pass
        try:
            await self._page.goto(
                f"{self.base_url}{self.IDLE_PAGE_PATH}",
                timeout=self.timeout_ms, wait_until="domcontentloaded",
            )
        except Exception as e:
            log.debug("Не удалось увести вкладку на страницу-заглушку: %s", e)

    async def _recover(self):
        """Восстановление после ошибки: перезапуск браузера, если он упал,
        или повторный вход, если сессия панели истекла."""
        if not self._browser_alive():
            await self._restart_browser()
            return
        try:
            await self._login()
        except XUIError:
            # Страница в странном состоянии - начинаем с чистого браузера.
            await self._restart_browser()

    async def _run(self, fn, *args, **kwargs):
        """Выполняет операцию с панелью под замком; при сбое один раз
        восстанавливает сессию/браузер и повторяет."""
        async with self._lock:
            try:
                try:
                    return await fn(*args, **kwargs)
                except XUIClientNotFound:
                    raise
                except Exception as e:
                    log.warning("Операция %s не удалась (%s), пробую восстановить сессию", fn.__name__, e)
                    try:
                        await self._recover()
                    except Exception as rec_e:
                        raise XUIError(f"{e} (восстановить сессию не удалось: {rec_e})")
                    try:
                        return await fn(*args, **kwargs)
                    except XUIError:
                        raise
                    except Exception as e2:
                        raise XUIError(str(e2))
            finally:
                # После любой операции (успешной или нет) не оставляем вкладку
                # на дашборде/Clients - иначе живые графики грузят CPU сутками.
                await self._park_page()

    async def close(self):
        for obj, method in ((self._context, "close"), (self._browser, "close"),
                            (self._playwright, "stop")):
            if obj is None:
                continue
            try:
                await getattr(obj, method)()
            except Exception:
                pass
        self._context = self._browser = self._playwright = self._page = None

    # ---------- логин ----------

    async def _login(self):
        try:
            # Важно: открываем корень ("/"), а не "/login" - у 3x-ui это одностраничное
            # приложение (SPA), маршрут /login существует только внутри уже
            # загруженного JS, отдельного серверного роута для него как страницы нет
            # (прямой GET /login отдаёт 404). Форма логина отрисовывается на "/",
            # если пользователь ещё не аутентифицирован.
            response = await self._page.goto(
                f"{self.base_url}/", timeout=self.timeout_ms, wait_until="load",
            )
        except Exception as e:
            raise XUIError(f"Не удалось открыть панель ({self.base_url}/): {e}")

        nav_status = response.status if response else None
        nav_headers = dict(response.headers) if response else {}

        # Даём странице время дорендериться (SPA может подгружать JS/данные уже после "load").
        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # Сессия ещё жива - панель сразу открылась без формы входа.
        if "/panel" in self._page.url:
            try:
                pw_visible = await self._page.locator('input[type="password"]').first.is_visible()
            except Exception:
                pw_visible = False
            if not pw_visible:
                self.login_debug_info = f"сессия активна, URL: {self._page.url}"
                return

        try:
            await self._page.wait_for_selector(
                'input[type="password"]', timeout=self.timeout_ms, state="visible",
            )
        except Exception as e:
            title = await self._page.title()
            html = await self._page.content()
            screenshot_path = "login_debug_screenshot.png"
            try:
                await self._page.screenshot(path=screenshot_path)
                screenshot_note = f"Скриншот сохранён: {screenshot_path}"
            except Exception:
                screenshot_note = "Не удалось сохранить скриншот"
            raise XUIError(
                f"Не нашёл поле пароля на странице {self.base_url}/: {e}. "
                f"Код ответа навигации: {nav_status}. Заголовки ответа: {nav_headers}. "
                f"Заголовок страницы: {title!r}. Длина HTML: {len(html)} символов. "
                f"Начало HTML: {html[:600]!r}. {screenshot_note}"
            )

        try:
            await self._page.fill('input[type="password"]', self.password, timeout=self.timeout_ms)
            # Логин обычно единственное текстовое поле на странице, отличное от пароля.
            username_input = self._page.locator('input:not([type="password"])').first
            await username_input.fill(self.username, timeout=self.timeout_ms)
        except Exception as e:
            html_snippet = (await self._page.content())[:400]
            raise XUIError(
                f"Нашёл поле пароля, но не смог заполнить форму логина: {e}. "
                f"HTML: {html_snippet}"
            )

        login_response_json = None
        try:
            async with self._page.expect_response(
                lambda r: r.request.method == "POST" and "/login" in r.url,
                timeout=self.timeout_ms,
            ) as resp_info:
                try:
                    await self._page.click('button[type="submit"]', timeout=5000)
                except Exception:
                    try:
                        await self._page.keyboard.press("Enter")
                    except Exception as e:
                        raise XUIError(f"Не удалось отправить форму логина: {e}")
            login_response = await resp_info.value
            try:
                login_response_json = await login_response.json()
            except Exception:
                login_response_json = None
        except XUIError:
            raise
        except Exception:
            # не смогли перехватить сетевой ответ - не критично, попробуем без токена из него
            pass

        try:
            await self._page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        except Exception:
            pass  # не критично, проверим по итоговому URL/содержимому ниже

        current_url = self._page.url
        if "/login" in current_url:
            html_snippet = (await self._page.content())[:400]
            raise XUIError(
                f"После отправки формы логина всё ещё на странице входа ({current_url}). "
                f"Скорее всего неверный логин или пароль. Фрагмент страницы: {html_snippet}"
            )

        # Ищем токен в ответе на логин (частый паттерн: токен возвращается в теле
        # POST /login, а не кладётся в cookie) и в localStorage/sessionStorage -
        # как запасной вариант, если слушатель ответов (_on_response) ничего не поймал.
        token_keys = ["token", "csrfToken", "csrf_token", "csrf", "access_token"]

        def _dig(obj):
            if not isinstance(obj, dict):
                return None
            for k in token_keys:
                if obj.get(k):
                    return obj[k]
            for v in obj.values():
                if isinstance(v, dict):
                    r = _dig(v)
                    if r:
                        return r
            return None

        # self._csrf_token уже мог быть заполнен слушателем ответов (_on_response) -
        # это приоритетный источник, т.к. мы точно знаем, что панель отдаёт токен
        # именно так.
        if not self._csrf_token and login_response_json:
            self._csrf_token = _dig(login_response_json)

        if not self._csrf_token:
            try:
                storage_dump = await self._page.evaluate("""
                    () => {
                        const out = {};
                        for (let i = 0; i < localStorage.length; i++) {
                            const k = localStorage.key(i);
                            out[k] = localStorage.getItem(k);
                        }
                        for (let i = 0; i < sessionStorage.length; i++) {
                            const k = sessionStorage.key(i);
                            out['session:' + k] = sessionStorage.getItem(k);
                        }
                        return out;
                    }
                """)
                for key, value in storage_dump.items():
                    lk = key.lower()
                    if "token" in lk or "csrf" in lk:
                        self._csrf_token = value
                        break
            except Exception:
                pass

        # Если токен всё ещё не найден - панель отдаёт его только в ответ на
        # успешный запрос (не при обычной загрузке страницы). Делаем один
        # "прогревочный" GET, чтобы слушатель ответов поймал токен до того,
        # как мы попробуем что-то создать через POST.
        if not self._csrf_token:
            try:
                await self._api_call(
                    "GET", "/panel/api/inbounds/getClientTraffics/__csrf_warmup__",
                )
            except XUIError:
                pass  # не важно, успешен ли сам запрос - важно, поймал ли листенер токен

        self.login_debug_info = (
            f"успешно, итоговый URL: {current_url}, "
            f"токен для API найден: {'да' if self._csrf_token else 'нет'}"
        )

    # ---------- низкоуровневый запрос через fetch() внутри страницы ----------

    async def _api_call(self, method: str, path: str, body=None):
        url = f"{self.base_url}{path}"
        # fetch() должен идти со страницы панели (тот же origin и cookie).
        try:
            if self._page and not self._page.url.startswith(self.base_url):
                await self._page.goto(
                    f"{self.base_url}{self.IDLE_PAGE_PATH}",
                    timeout=self.timeout_ms, wait_until="domcontentloaded",
                )
        except Exception:
            pass
        script = """
        async ({method, url, body, presetToken}) => {
            function getCookie(name) {
                const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
                return m ? decodeURIComponent(m[1]) : null;
            }
            let token = presetToken || null;
            if (!token) {
                const cookieNames = ['csrf_token', 'csrfToken', 'XSRF-TOKEN', 'csrf', '_csrf'];
                for (const name of cookieNames) {
                    const v = getCookie(name);
                    if (v) { token = v; break; }
                }
            }
            if (!token) {
                const meta = document.querySelector('meta[name="csrf-token"]');
                if (meta) token = meta.getAttribute('content');
            }

            const opts = {
                method,
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
                credentials: 'include',
            };
            if (token) {
                opts.headers['X-CSRF-Token'] = token;
                opts.headers['X-Csrf-Token'] = token;
                opts.headers['X-XSRF-TOKEN'] = token;
                opts.headers['Authorization'] = 'Bearer ' + token;
            }
            if (body !== null && body !== undefined) { opts.body = JSON.stringify(body); }

            const res = await fetch(url, opts);
            const text = await res.text();
            const headers = {};
            res.headers.forEach((v, k) => { headers[k] = v; });
            return { status: res.status, text, headers, tokenUsed: token, cookies: document.cookie };
        }
        """
        try:
            result = await self._page.evaluate(
                script,
                {"method": method, "url": url, "body": body, "presetToken": self._csrf_token},
            )
        except Exception as e:
            raise XUIError(f"Ошибка выполнения запроса к {path} внутри браузера: {e}")

        if result["status"] in (401, 403):
            raise XUIError(
                f"{path} вернул {result['status']}: {result['text'][:200]!r}. "
                f"CSRF-токен найден и отправлен: {bool(result.get('tokenUsed'))}. "
                f"Заголовки ответа: {result.get('headers')}. "
                f"Текущие cookie страницы: {result.get('cookies')!r}"
            )

        try:
            return json.loads(result["text"])
        except ValueError:
            raise XUIError(f"3x-ui вернул не-JSON ответ на {path}: {result['text'][:300]}")

    async def _try_path_bodies(self, method, path_bodies):
        """path_bodies: список (path, body) - у каждого пути своё тело,
        т.к. схема JSON отличается между старым и новым (v3) API."""
        errors = []
        for path, body in path_bodies:
            try:
                data = await self._api_call(method, path, body)
            except XUIError as e:
                errors.append(str(e))
                continue
            if data.get("success"):
                return data, path
            errors.append(f"{path} вернул success=false: {data}")
        raise XUIError(" | ".join(errors) if errors else "Ни один из известных путей API не сработал")

    # ---------- публичное API (всё идёт через _run: очередь + восстановление) ----------

    async def add_client(self, inbound_id: int, email: str, days: int, total_gb: int = 0,
                          ip_limit: int = 0, auto_renew_days: int = 0):
        """Создаёт клиента через интерфейс панели. Если клиент с таким email
        уже есть (остался от прошлой неудачной операции) - сначала удаляет его,
        чтобы новые параметры (срок, лимиты) точно применились."""
        return await self._run(self._add_client_impl, email, days, total_gb, ip_limit, auto_renew_days)

    async def delete_client(self, inbound_id: int, client_uuid: str, email: str = ""):
        """Удаляет клиента. Если его уже нет в панели (например, удалили
        вручную) - считается успехом, чтобы запись в БД не «застревала»."""
        if not email:
            raise XUIError("delete_client требует email клиента для поиска в интерфейсе")
        return await self._run(self._delete_client_impl, email)

    async def find_client(self, email: str):
        """Возвращает объект клиента из панели (id, subId, expiryTime, ...) или None."""
        return await self._run(self._find_client_by_email, email)

    async def get_client_traffic(self, email: str):
        """Трафик и фактический срок клиента (поля up, down, expiryTime...).
        None - если клиент не найден или панель недоступна."""
        try:
            return await self._run(self._get_traffic_impl, email)
        except XUIError:
            return None

    # ---------- реализация ----------

    async def _add_client_impl(self, email, days, total_gb, ip_limit, auto_renew_days):
        existing = await self._find_client_by_email(email)
        if existing:
            log.info("Клиент %s уже есть в панели - удаляю перед созданием заново", email)
            await self._delete_client_impl(email)

        expiry_ms = self._days_to_expiry_ms(days)
        await self._add_client_via_ui(email, days, total_gb, ip_limit, auto_renew_days)

        # После создания через UI берём реальные id/subId обычным GET-запросом.
        # Панели может понадобиться пара секунд, поэтому несколько попыток.
        found = None
        for _ in range(5):
            found = await self._find_client_by_email(email)
            if found and found.get("subId"):
                break
            await asyncio.sleep(1)
        if not found or not found.get("subId"):
            raise XUIError(f"Клиент {email} не появился в панели после создания")

        return {"uuid": found.get("id"), "subId": found.get("subId"), "expiryMs": expiry_ms}

    async def _get_traffic_impl(self, email):
        data, _ = await self._try_path_bodies("GET", [
            (f"/panel/api/inbounds/getClientTraffics/{email}", None),
            (f"/panel/api/clients/traffic/{email}", None),
        ])
        return data.get("obj")

    CLIENTS_PAGE_PATH = "/panel/clients"
    ADD_CLIENT_BUTTON_SELECTOR = (
        "#content-layout > div > div > div > div:nth-child(2) > div > "
        "div.ant-card-head > div > div > div > button.ant-btn.xui.ant-btn-primary"
    )
    SAVE_CLIENT_BUTTON_SELECTOR = "div.ant-modal-footer button.ant-btn.xui.ant-btn-primary"
    SEARCH_INPUT_SELECTOR = 'input[aria-label="Search"]'
    DELETE_CONFIRM_BUTTON_SELECTOR = ".ant-modal-confirm-btns button.ant-btn-dangerous"
    TABLE_ROWS_SELECTOR = (
        ".xui.ant-table-css-var tbody tr"
        ":not(.ant-table-measure-row):not(.ant-table-placeholder)"
    )

    async def _open_clients_page(self):
        try:
            await self._page.goto(
                f"{self.base_url}{self.CLIENTS_PAGE_PATH}",
                timeout=self.timeout_ms, wait_until="load",
            )
        except Exception as e:
            raise XUIError(f"Не удалось открыть страницу Clients: {e}")
        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        # Сессия истекла - панель показала форму входа вместо списка клиентов.
        try:
            if await self._page.locator('input[type="password"]').first.is_visible():
                raise XUIError("Сессия панели истекла (открылась форма входа)")
        except XUIError:
            raise
        except Exception:
            pass

    async def _add_client_via_ui(self, email: str, days: int, total_gb: int = 0,
                                  ip_limit: int = 0, auto_renew_days: int = 0):
        await self._open_clients_page()

        try:
            await self._page.click(self.ADD_CLIENT_BUTTON_SELECTOR, timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не нашёл кнопку добавления клиента на странице Clients: {e}")

        modal = self._page.locator(".ant-modal-body")
        try:
            await modal.wait_for(state="visible", timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не открылось окно добавления клиента: {e}")

        try:
            email_input = modal.locator("input").first
            await email_input.fill(email, timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не смог заполнить поле Email в форме: {e}")

        if ip_limit and ip_limit > 0:
            # Поле IP Limit - третье по порядку в форме (после Email и Traffic
            # Limit (GB)). Необязательный шаг.
            try:
                ip_limit_input = modal.locator("input").nth(2)
                await ip_limit_input.fill(str(ip_limit), timeout=3000)
            except Exception:
                log.warning("Не удалось заполнить IP Limit для %s", email)

        if auto_renew_days and auto_renew_days > 0:
            try:
                auto_renew_input = self._page.locator("text=Auto Renew").locator(
                    "xpath=following::input[1]"
                )
                await auto_renew_input.fill(str(auto_renew_days), timeout=3000)
            except Exception:
                log.warning("Не удалось заполнить Auto Renew для %s", email)

        # Отсчёт срока начинается с первого подключения клиента.
        try:
            start_after_use_toggle = self._page.locator("text=Start After First Use").locator(
                "xpath=following::button[@role='switch'][1]"
            )
            await start_after_use_toggle.click(timeout=3000)
        except Exception:
            log.warning("Не удалось включить Start After First Use для %s", email)

        if days > 0:
            try:
                expiry_ms = self._days_to_expiry_ms(days)
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
                expiry_input = self._page.locator("text=Expiry").locator("xpath=following::input[1]")
                await expiry_input.click(timeout=3000)
                await expiry_input.fill(dt.strftime("%Y-%m-%d %H:%M:%S"), timeout=3000)
                await self._page.keyboard.press("Enter")
            except Exception:
                log.warning("Не удалось заполнить срок для %s", email)

        try:
            select_all = self._page.get_by_text("Select all", exact=True)
            await select_all.click(timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не смог выбрать inbound'ы (кнопка Select all): {e}")

        try:
            await self._page.click(self.SAVE_CLIENT_BUTTON_SELECTOR, timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не смог нажать кнопку создания клиента: {e}")

        try:
            await modal.wait_for(state="hidden", timeout=self.timeout_ms)
        except Exception:
            html = (await self._page.content())[:400]
            raise XUIError(
                f"Окно создания клиента не закрылось - вероятно, панель показала "
                f"ошибку валидации формы. Фрагмент страницы: {html}"
            )

    async def _find_client_by_email(self, email: str):
        # В 3x-ui v3 клиенты могут лежать и в списке inbound'ов, и в отдельном
        # списке клиентов - проверяем все известные пути.
        errors = []
        any_ok = False
        for path in ("/panel/api/inbounds/list", "/panel/api/clients/list", "/panel/api/clients"):
            try:
                data = await self._api_call("GET", path)
            except XUIError as e:
                errors.append(str(e))
                continue
            if not isinstance(data, dict) or not data.get("success"):
                errors.append(f"{path}: success=false")
                continue
            any_ok = True
            found = self._search_email_in_obj(data.get("obj"), email)
            if found:
                return found
        if any_ok:
            return None  # список получен, клиента в нём нет
        raise XUIError("Не удалось получить список клиентов: " + " | ".join(errors)[:500])

    @staticmethod
    def _search_email_in_obj(obj, email):
        """Ищет клиента по email во всей структуре ответа (включая settings,
        который приходит вложенной JSON-строкой). Под одним email может
        встретиться настоящий UUID клиента и числовой id записи статистики
        трафика - предпочитаем тот, чей id выглядит как UUID."""
        candidates = []

        def walk(o):
            if isinstance(o, dict):
                if o.get("email") == email and o.get("id") is not None:
                    candidates.append(o)
                settings = o.get("settings")
                if isinstance(settings, str):
                    try:
                        walk(json.loads(settings))
                    except Exception:
                        pass
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for item in o:
                    walk(item)

        walk(obj)
        if not candidates:
            return None

        def looks_like_uuid(v):
            return isinstance(v, str) and len(v) == 36 and v.count("-") == 4

        uuid_candidates = [c for c in candidates if looks_like_uuid(c.get("id"))]
        return (uuid_candidates or candidates)[0]

    @staticmethod
    def _row_matches_email(row_text: str, email: str) -> bool:
        """Точное совпадение email в тексте строки: discord_123 не должен
        совпасть со строкой клиента discord_1234."""
        return re.search(re.escape(email) + r"(?![\w.@-])", row_text) is not None

    async def _delete_client_impl(self, email: str):
        await self._open_clients_page()

        try:
            search_input = self._page.locator(self.SEARCH_INPUT_SELECTOR)
            await search_input.fill(email, timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не нашёл поле поиска на странице Clients: {e}")

        # AntD фильтрует таблицу не мгновенно - ждём нужную строку до ~6 секунд.
        row = None
        for _ in range(12):
            await self._page.wait_for_timeout(500)
            for candidate in await self._page.locator(self.TABLE_ROWS_SELECTOR).all():
                try:
                    text = await candidate.inner_text(timeout=2000)
                except Exception:
                    continue
                if self._row_matches_email(text, email):
                    row = candidate
                    break
            if row is not None:
                break

        if row is None:
            # В таблице не нашли - проверяем через API, есть ли клиент вообще.
            if await self._find_client_by_email(email) is None:
                log.info("Клиент %s уже отсутствует в панели - считаю удалённым", email)
                return
            raise XUIError(f"Клиент {email} есть в панели, но не найден в таблице после поиска")

        try:
            # Иконки в столбце "Действия": grid / info / duplicate / edit / delete -
            # корзина пятая (индекс 4).
            delete_icon = row.locator("button").nth(4)
            await delete_icon.click(timeout=self.timeout_ms)
        except Exception as e:
            html_snippet = (await row.inner_html())[:300]
            raise XUIError(f"Не нашёл/не смог нажать иконку удаления в строке клиента: {e}. Строка: {html_snippet}")

        try:
            confirm_btn = self._page.locator(self.DELETE_CONFIRM_BUTTON_SELECTOR)
            await confirm_btn.wait_for(state="visible", timeout=self.timeout_ms)
            await confirm_btn.click(timeout=self.timeout_ms)
        except Exception as e:
            raise XUIError(f"Не нашёл/не смог нажать кнопку подтверждения удаления: {e}")

        try:
            await row.wait_for(state="hidden", timeout=self.timeout_ms)
        except Exception:
            pass

        # Проверяем, что клиент действительно исчез.
        for _ in range(3):
            if await self._find_client_by_email(email) is None:
                return
            await asyncio.sleep(1)
        raise XUIError(f"После удаления клиент {email} всё ещё есть в панели")

    @staticmethod
    def _days_to_expiry_ms(days: int) -> int:
        if days <= 0:
            return 0
        return int((time.time() + days * 86400) * 1000)
