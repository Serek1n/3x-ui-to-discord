"""
HTTPS-переходник для кнопки «Добавить в Nexus».

Discord разрешает в кнопках-ссылках только http(s), поэтому кнопка ведёт на
https://<домен>:<порт>/add/<subId>, а эта страница сразу открывает приложение
ссылкой nexus://import?url=<подписка>. Если Nexus не открылся - на странице
есть повторная кнопка, ссылка на скачивание и копирование подписки.

Страница отдаётся только для подписок из базы бота (subId известен),
поэтому переходник нельзя использовать, чтобы подсунуть чужую ссылку. Частота
запросов с одного IP ограничена, чтобы subId нельзя было подобрать перебором.

Без сертификата ссылка подписки шла бы по сети открытым текстом, поэтому
без NEXUS_LINK_CERT/NEXUS_LINK_KEY сервер слушает только 127.0.0.1 (вариант
«за nginx»), а если задан только один из двух файлов - не запускается.

Сервер работает внутри бота (aiohttp уже есть в зависимостях discord.py).
Сертификат - тот же, что у панели 3x-ui; файлы перечитываются раз в сутки,
так что продление сертификата подхватывается без перезапуска.
"""

import asyncio
import html
import logging
import os
import re
import ssl
import time
from urllib.parse import quote

from aiohttp import web

log = logging.getLogger("link_server")

SUB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# Ограничение частоты: с одного IP не больше RATE_REQUESTS открытий страницы в
# минуту и не больше RATE_MISSES неверных subId за 10 минут - так subId нельзя
# подбирать перебором.
RATE_REQUESTS = 30
RATE_MISSES = 10
MISS_WINDOW = 600


def nexus_deeplink(sub_url: str) -> str:
    return "nexus://import?url=" + quote(sub_url, safe="")


FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
FONT_FILES = {"unbounded-cyrillic-700.woff2", "unbounded-latin-700.woff2"}

# Цвета взяты с логотипа: индиго фона, синий, фиолетовый и розовый пиксели.
CSS = """
@font-face { font-family: "Unbounded"; font-weight: 700; font-display: swap;
  src: url(/fonts/unbounded-cyrillic-700.woff2) format("woff2");
  unicode-range: U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116; }
@font-face { font-family: "Unbounded"; font-weight: 700; font-display: swap;
  src: url(/fonts/unbounded-latin-700.woff2) format("woff2");
  unicode-range: U+0000-00FF, U+2000-206F, U+20AC, U+2122; }
:root {
  color-scheme: dark;
  --bg: #0C0B2B; --line: #2B2969; --field: #13123C;
  --text: #EEEDFF; --muted: #A9A7D8;
  --blue: #4F7BFF; --violet: #8C5BFF; --pink: #FF5CC6;
}
* { box-sizing: border-box; }
html { background: var(--bg); }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  color: var(--text); font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { width: 100%; max-width: 27rem; margin: 0 auto; padding: 48px 20px 40px; }
.brand { display: flex; align-items: center; gap: 12px; }
.mark {
  width: 44px; height: 44px; border-radius: 12px; overflow: hidden; flex: none;
  display: grid; place-items: center; background: #070726;
  font: 700 22px/1 "Unbounded", system-ui, sans-serif; color: #fff;
}
.mark img { width: 100%; height: 100%; object-fit: cover; display: block; }
.word { font: 700 14px/1 "Unbounded", system-ui, sans-serif; letter-spacing: .04em; }
h1 {
  margin: 40px 0 12px; font: 700 29px/1.22 "Unbounded", system-ui, sans-serif;
  letter-spacing: -.01em; text-wrap: balance;
}
.lead { margin: 0; color: var(--muted); font-size: 17px; }
a { color: #CDBDFF; text-underline-offset: 3px; }
a:hover { color: #fff; }
:focus-visible { outline: 3px solid var(--pink); outline-offset: 3px; }
"""

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Добавить в Nexus</title>
<style>{css}
.status {{ display: flex; align-items: center; gap: 12px; margin: 28px 0 16px; font-size: 15px; min-height: 24px; }}
.dots {{ display: inline-flex; gap: 5px; flex: none; }}
.dots i {{ width: 8px; height: 8px; display: block; animation: blink 1.2s infinite step-end; }}
.dots i:nth-child(1) {{ background: var(--blue); }}
.dots i:nth-child(2) {{ background: var(--violet); animation-delay: .2s; }}
.dots i:nth-child(3) {{ background: var(--pink); animation-delay: .4s; }}
@keyframes blink {{ 50% {{ opacity: .25; }} }}
.status.waited .dots i {{ animation: none; opacity: .3; }}
.open {{
  display: block; padding: 16px 20px; border-radius: 12px; text-align: center; text-decoration: none;
  background: var(--violet); color: #fff; font-weight: 650; font-size: 17px;
}}
.open:hover {{ background: #7B47FF; color: #fff; }}
.open:active {{ transform: translateY(1px); }}
.help {{ margin-top: 40px; padding-top: 22px; border-top: 1px solid var(--line); }}
.help h2 {{ margin: 0 0 6px; font-size: 16px; font-weight: 650; }}
.help p {{ margin: 0 0 10px; color: var(--muted); font-size: 15px; }}
.field {{
  display: flex; margin-top: 16px; border: 1px solid var(--line); border-radius: 10px;
  background: var(--field); overflow: hidden;
}}
.field input {{
  flex: 1; min-width: 0; padding: 12px 14px; border: 0; background: transparent;
  color: var(--text); font: inherit; font-size: 14px; text-overflow: ellipsis;
}}
.field input:focus-visible {{ outline-offset: -3px; }}
.field button {{
  flex: none; padding: 0 16px; border: 0; border-left: 1px solid var(--line); background: transparent;
  color: var(--text); font: inherit; font-size: 14px; font-weight: 650; cursor: pointer;
}}
.field button:hover {{ background: #1C1A52; }}
@media (prefers-reduced-motion: reduce) {{ .dots i {{ animation: none; }} }}
</style>
</head>
<body>
<main>
  <div class="brand">
    <span class="mark" aria-hidden="true">{logo}</span>
    <span class="word">NEXUS VPN</span>
  </div>

  <h1>Добавляем подписку в&nbsp;Nexus</h1>
  <p class="lead">Nexus откроется сам и спросит, добавить ли подписку. Нажмите «Добавить».</p>

  <p class="status" id="status" role="status" aria-live="polite">
    <span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
    <span id="status-text">Открываем приложение</span>
  </p>

  <a class="open" id="open" href="{deeplink}">Открыть Nexus</a>

  <section class="help" aria-labelledby="help-title">
    <h2 id="help-title">Nexus не открылся</h2>
    <p>{download}</p>
    <p>На iPhone Nexus пока нет. Скопируйте ссылку и добавьте её в Happ: «+», затем «Вставить из буфера».</p>
    <div class="field">
      <input id="link" type="text" readonly value="{sub_url}" aria-label="Ссылка подписки">
      <button id="copy" type="button">Копировать</button>
    </div>
  </section>
</main>
<script>
  var open = document.getElementById("open");
  setTimeout(function () {{ window.location.href = open.href; }}, 400);
  setTimeout(function () {{
    document.getElementById("status").classList.add("waited");
    document.getElementById("status-text").textContent = "Окно не появилось? Нажмите кнопку ниже.";
  }}, 3000);
  document.getElementById("copy").addEventListener("click", function () {{
    var btn = this, input = document.getElementById("link");
    function done() {{ btn.textContent = "Скопировано"; setTimeout(function () {{ btn.textContent = "Копировать"; }}, 2000); }}
    if (navigator.clipboard && window.isSecureContext) {{
      navigator.clipboard.writeText(input.value).then(done, function () {{ input.select(); }});
    }} else {{ input.select(); document.execCommand("copy"); done(); }}
  }});
</script>
</body>
</html>
"""

NOT_FOUND = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Ссылка не работает</title>
<style>{css}</style>
</head>
<body>
<main>
  <div class="brand">
    <span class="mark" aria-hidden="true">{logo}</span>
    <span class="word">NEXUS VPN</span>
  </div>
  <h1>Эта ссылка больше не&nbsp;работает</h1>
  <p class="lead">Подписку перевыпустили или удалили. Откройте /myvpn в Discord и нажмите «Добавить в Nexus» ещё раз.</p>
</main>
</body>
</html>
"""

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; font-src 'self'; style-src 'unsafe-inline'; "
                               "script-src 'unsafe-inline'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


class LinkServer:
    """sub_url_for(sub_id) -> ссылка подписки или None, если такой подписки нет."""

    def __init__(self, sub_url_for, *, host: str = "0.0.0.0", port: int = 8444,
                 cert: str = "", key: str = "", download_url: str = "", logo_path: str = ""):
        self.sub_url_for = sub_url_for
        self.host = host
        self.port = port
        self.cert = cert
        self.key = key
        self.download_url = download_url
        self.logo_path = logo_path if logo_path and os.path.isfile(logo_path) else ""
        self._runner = None
        self._ssl = None
        self._reload_task = None
        self._hits: dict = {}     # ip -> [время запросов за минуту]
        self._misses: dict = {}   # ip -> [время неверных subId за MISS_WINDOW]

    def _limited(self, ip: str, now: float) -> bool:
        hits = [t for t in self._hits.get(ip, ()) if now - t < 60]
        misses = [t for t in self._misses.get(ip, ()) if now - t < MISS_WINDOW]
        self._hits[ip], self._misses[ip] = hits, misses
        if len(self._hits) > 10000:   # не даём словарю расти бесконечно
            self._hits.clear()
            self._misses.clear()
        return len(hits) >= RATE_REQUESTS or len(misses) >= RATE_MISSES

    def _logo(self) -> str:
        return '<img src="/logo.png" alt="">' if self.logo_path else "N"

    def _render(self, sub_url: str) -> str:
        if self.download_url:
            download = (f'<a href="{html.escape(self.download_url, quote=True)}" rel="noopener">'
                        f'Скачайте Nexus для Android или Windows</a>, установите и нажмите «Открыть Nexus» ещё раз.')
        else:
            download = "Установите Nexus и нажмите «Открыть Nexus» ещё раз."
        return PAGE.format(
            css=CSS,
            deeplink=html.escape(nexus_deeplink(sub_url), quote=True),
            sub_url=html.escape(sub_url, quote=True),
            download=download,
            logo=self._logo(),
        )

    async def handle_logo(self, request: web.Request):
        if not self.logo_path:
            raise web.HTTPNotFound()
        return web.FileResponse(self.logo_path, headers={"Cache-Control": "public, max-age=86400"})

    async def handle_add(self, request: web.Request) -> web.Response:
        ip = request.remote or "?"
        now = time.time()
        if self._limited(ip, now):
            return web.Response(text="Слишком много запросов, попробуйте через несколько минут.",
                                status=429, headers={**HEADERS, "Retry-After": "600"})
        self._hits.setdefault(ip, []).append(now)
        sub_id = request.match_info.get("sub_id", "")
        sub_url = self.sub_url_for(sub_id) if SUB_ID_RE.match(sub_id) else None
        if not sub_url:
            self._misses.setdefault(ip, []).append(now)
            return web.Response(text=NOT_FOUND.format(css=CSS, logo=self._logo()), status=404,
                                content_type="text/html", headers=HEADERS)
        return web.Response(text=self._render(sub_url), content_type="text/html", headers=HEADERS)

    async def handle_font(self, request: web.Request):
        name = request.match_info.get("name", "")
        path = os.path.join(FONTS_DIR, name)
        if name not in FONT_FILES or not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=2592000",
                                               "Content-Type": "font/woff2"})

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok", headers={"Cache-Control": "no-store"})

    def _make_ssl(self):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert, self.key)
        return ctx

    async def _reload_cert_loop(self):
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                # Новые подключения пойдут уже с продлённым сертификатом.
                self._ssl.load_cert_chain(self.cert, self.key)
                log.info("Сертификат переходника перечитан")
            except Exception as e:
                log.warning("Не удалось перечитать сертификат: %s", e)

    async def start(self):
        app = web.Application()
        app.router.add_get("/add/{sub_id}", self.handle_add)
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/logo.png", self.handle_logo)
        app.router.add_get("/fonts/{name}", self.handle_font)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        host = self.host
        if bool(self.cert) != bool(self.key):
            await self._runner.cleanup()
            raise ValueError("нужны оба файла: NEXUS_LINK_CERT и NEXUS_LINK_KEY")
        if self.cert and self.key:
            self._ssl = self._make_ssl()
            self._reload_task = asyncio.create_task(self._reload_cert_loop())
        elif host not in ("127.0.0.1", "localhost", "::1"):
            log.warning("Сертификат не задан - переходник слушает только 127.0.0.1 "
                        "(доступ снаружи - через nginx с https)")
            host = "127.0.0.1"
        site = web.TCPSite(self._runner, host, self.port, ssl_context=self._ssl)
        await site.start()
        log.info("Переходник «Добавить в Nexus» слушает %s:%s (%s)", host, self.port,
                 "HTTPS" if self._ssl else "HTTP")

    async def stop(self):
        if self._reload_task:
            self._reload_task.cancel()
        if self._runner:
            await self._runner.cleanup()
