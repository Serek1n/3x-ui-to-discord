"""
HTTPS-переходник для кнопки «Добавить в Nexus».

Discord разрешает в кнопках-ссылках только http(s), поэтому кнопка ведёт на
https://<домен>:<порт>/add/<subId>, а эта страница сразу открывает приложение
ссылкой nexus://import?url=<подписка>. Если Nexus не открылся - на странице
есть повторная кнопка, ссылка на скачивание и копирование подписки.

Страница отдаётся только для подписок, которые есть в базе бота (subId
известен), поэтому переходник нельзя использовать, чтобы подсунуть чужую
ссылку. Запись истёкшего trial живёт в базе до часовой проверки - такая
подписка откроется, но просто не загрузится.

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
from urllib.parse import quote

from aiohttp import web

log = logging.getLogger("link_server")

SUB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def nexus_deeplink(sub_url: str) -> str:
    return "nexus://import?url=" + quote(sub_url, safe="")


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Добавить в Nexus</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 24px 16px; background: #05061A; color: #E6E8FF;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background-image: radial-gradient(circle at 15% 10%, rgba(47,107,255,.28), transparent 45%),
                    radial-gradient(circle at 85% 90%, rgba(224,60,255,.24), transparent 45%);
}}
main {{ width: 100%; max-width: 440px; text-align: center; }}
.logo {{
  width: 96px; height: 96px; margin: 0 auto 20px; border-radius: 26px; display: flex;
  align-items: center; justify-content: center; font-weight: 800; font-size: 50px; color: #fff;
  background: linear-gradient(135deg, #3D6BFF 0%, #8B5CFF 55%, #FF5FD2 100%);
  box-shadow: 0 0 40px rgba(139,92,255,.55); overflow: hidden;
}}
.logo img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
h1 {{ margin: 0 0 8px; font-size: 26px; }}
p {{ margin: 0 0 22px; color: #AEB3D6; line-height: 1.5; }}
.btn {{
  display: block; width: 100%; padding: 16px 20px; margin-bottom: 12px; border-radius: 14px;
  font-size: 17px; font-weight: 700; text-decoration: none; border: 0; cursor: pointer;
}}
.primary {{ color: #fff; background: linear-gradient(90deg, #4F7BFF, #B455FF 60%, #FF5FD2); }}
.secondary {{ color: #E6E8FF; background: rgba(124,107,255,.12); border: 1px solid rgba(150,120,255,.4); }}
.btn:focus-visible {{ outline: 3px solid #FF5FD2; outline-offset: 3px; }}
small {{ display: block; margin-top: 18px; color: #7C82A8; line-height: 1.5; }}
#done {{ min-height: 1.4em; color: #57F287; font-weight: 600; }}
</style>
</head>
<body>
<main>
  <div class="logo" aria-hidden="true">{logo}</div>
  <h1>Открываем Nexus…</h1>
  <p>Приложение спросит, добавить ли подписку NEXUS VPN — нажмите «Добавить».</p>
  <a class="btn primary" id="open" href="{deeplink}">Открыть в Nexus</a>
  {download}
  <button class="btn secondary" id="copy" type="button" data-link="{sub_url}">Скопировать ссылку подписки</button>
  <div id="done" role="status" aria-live="polite"></div>
  <small>Ничего не произошло? Значит, Nexus не установлен — скачайте его кнопкой выше.
  Ссылку можно вставить в Nexus или Happ вручную: «+» → «Вставить из буфера».</small>
</main>
<script>
  setTimeout(function () {{ window.location.href = document.getElementById("open").href; }}, 300);
  document.getElementById("copy").addEventListener("click", function () {{
    var link = this.getAttribute("data-link"), done = document.getElementById("done");
    function ok() {{ done.textContent = "Скопировано ✓"; }}
    if (navigator.clipboard) {{
      navigator.clipboard.writeText(link).then(ok, function () {{ window.prompt("Скопируйте ссылку:", link); }});
    }} else {{ window.prompt("Скопируйте ссылку:", link); }}
  }});
</script>
</body>
</html>
"""

NOT_FOUND = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>Ссылка устарела</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;
background:#05061A;color:#E6E8FF;font-family:system-ui,sans-serif;text-align:center}
p{color:#AEB3D6;max-width:420px;line-height:1.5}</style></head>
<body><main><h1>Ссылка устарела</h1>
<p>Эта подписка больше не действует (её перевыпустили или удалили). Откройте <b>/myvpn</b> в Discord и нажмите «Добавить в Nexus» ещё раз.</p>
</main></body></html>
"""

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
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

    def _render(self, sub_url: str) -> str:
        download = ""
        if self.download_url:
            download = (f'<a class="btn secondary" href="{html.escape(self.download_url, quote=True)}" '
                        f'rel="noopener">Скачать Nexus</a>')
        return PAGE.format(
            deeplink=html.escape(nexus_deeplink(sub_url), quote=True),
            sub_url=html.escape(sub_url, quote=True),
            download=download,
            logo='<img src="/logo.png" alt="">' if self.logo_path else "N",
        )

    async def handle_logo(self, request: web.Request):
        if not self.logo_path:
            raise web.HTTPNotFound()
        return web.FileResponse(self.logo_path, headers={"Cache-Control": "public, max-age=86400"})

    async def handle_add(self, request: web.Request) -> web.Response:
        sub_id = request.match_info.get("sub_id", "")
        sub_url = self.sub_url_for(sub_id) if SUB_ID_RE.match(sub_id) else None
        if not sub_url:
            return web.Response(text=NOT_FOUND, status=404, content_type="text/html", headers=HEADERS)
        return web.Response(text=self._render(sub_url), content_type="text/html", headers=HEADERS)

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
