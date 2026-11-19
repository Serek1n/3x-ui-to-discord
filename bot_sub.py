"""Подписка, которую выдаёт сам бот: {NEXUS_LINK_BASE}/sub/<token>.

Бот забирает подписку человека с каждого сервера 3x-ui (их обычные ссылки
подписки), склеивает конфиги в одну и отдаёт приложению (Happ, v2rayN, Nexus).
Поэтому все серверы равноправны: страны можно добавлять, убирать и менять
местами, а ссылка у человека остаётся прежней.

- Сервер не ответил - берутся его конфиги из последнего удачного ответа
  (до STALE_SEC), чтобы страна не пропадала у людей из-за минутного сбоя.
- Названия конфигов заменяются на название сервера из .env (XUI_NAME, XUIn_NAME),
  например «🇳🇱 Нидерланды».
- Трафик и срок (заголовок subscription-userinfo) суммируются по серверам.
"""
import asyncio
import base64
import binascii
import json
import logging
import re
import time
from urllib.parse import quote

import httpx

log = logging.getLogger("bot_sub")

CACHE_SEC = 60          # один и тот же ответ не собираем чаще раза в минуту
STALE_SEC = 24 * 3600   # сколько держать конфиги сервера, который перестал отвечать
FETCH_TIMEOUT = 10
FETCH_UA = "v2rayN/7.0"  # 3x-ui отдаёт обычный список ссылок (base64)
_URI = re.compile(r"^[a-z0-9+.-]+://", re.I)


def decode_body(body: str) -> list:
    """Тело подписки 3x-ui -> список ссылок-конфигов. Понимает base64 и простой текст."""
    text = (body or "").strip()
    if text and "://" not in text:
        compact = re.sub(r"\s+", "", text)
        try:
            text = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return []
    return [line.strip() for line in text.splitlines() if _URI.match(line.strip())]


def rename(uri: str, name: str) -> str:
    """Меняет название конфига (то, что видно в приложении)."""
    if uri.lower().startswith("vmess://"):
        raw = uri[8:]
        try:
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return uri
        data["ps"] = name
        return "vmess://" + base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
    return uri.split("#", 1)[0] + "#" + quote(name, safe="")


def parse_userinfo(value: str) -> dict:
    out = {}
    for part in (value or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k and v.strip().lstrip("-").isdigit():
            out[k.strip()] = int(v)
    return out


def merge_userinfo(infos: list) -> str:
    """Трафик - сумма по серверам, лимит - наибольший, срок - самый ранний (0 - бессрочно)."""
    infos = [i for i in infos if i]
    if not infos:
        return ""
    up = sum(i.get("upload", 0) for i in infos)
    down = sum(i.get("download", 0) for i in infos)
    total = max(i.get("total", 0) for i in infos)
    expires = [i["expire"] for i in infos if i.get("expire", 0) > 0]
    return f"upload={up}; download={down}; total={total}; expire={min(expires) if expires else 0}"


class SubAggregator:
    """sources(sub) -> [(название, url подписки на сервере или None, verify_ssl)]
    Название сервера используется для конфигов; None - на сервере нет клиента."""

    def __init__(self, sources, *, title: str = "NEXUS VPN", rename_configs: bool = True,
                 update_hours: int = 12):
        self.sources = sources
        self.title = title
        self.rename_configs = rename_configs
        self.update_hours = update_hours
        self._last_good: dict = {}   # (сервер, email) -> (время, [ссылки], userinfo)
        self._cache: dict = {}       # token -> (время, тело, заголовки)
        self._locks: dict = {}

    async def _fetch(self, url: str, verify: bool):
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, verify=verify, follow_redirects=True,
                                     headers={"User-Agent": FETCH_UA}) as http:
            r = await http.get(url)
        r.raise_for_status()
        links = decode_body(r.text)
        if not links:
            raise ValueError("пустая подписка")
        return links, parse_userinfo(r.headers.get("subscription-userinfo", ""))

    async def _server(self, email: str, name: str, url, verify: bool):
        key = (name, email)
        if url:
            try:
                links, info = await self._fetch(url, verify)
                self._last_good[key] = (time.time(), links, info)
                return name, links, info
            except Exception as e:
                log.warning("Подписка сервера «%s» не получена: %s", name, type(e).__name__)
        cached = self._last_good.get(key)
        if cached and time.time() - cached[0] < STALE_SEC:
            return name, cached[1], cached[2]
        return name, [], {}

    def _headers(self, infos: list) -> dict:
        h = {
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "no-store",
            "profile-title": "base64:" + base64.b64encode(self.title.encode()).decode(),
            "profile-update-interval": str(self.update_hours),
            "X-Robots-Tag": "noindex, nofollow",
        }
        info = merge_userinfo(infos)
        if info:
            h["subscription-userinfo"] = info
        return h

    async def build(self, token: str, sub: dict):
        """(тело, заголовки) или None, если ни один сервер ничего не отдал."""
        cached = self._cache.get(token)
        if cached and time.time() - cached[0] < CACHE_SEC:
            return cached[1], cached[2]
        lock = self._locks.setdefault(token, asyncio.Lock())
        async with lock:
            cached = self._cache.get(token)
            if cached and time.time() - cached[0] < CACHE_SEC:
                return cached[1], cached[2]
            sources = await self.sources(sub)
            results = await asyncio.gather(*(self._server(sub["email"], n, u, v) for n, u, v in sources))
            lines, infos = [], []
            for name, links, info in results:
                infos.append(info)
                for i, link in enumerate(links, 1):
                    label = name if len(links) == 1 else f"{name} {i}"
                    lines.append(rename(link, label) if self.rename_configs else link)
            if not lines:
                return None
            body = base64.b64encode("\n".join(lines).encode()).decode()
            headers = self._headers(infos)
            self._cache[token] = (time.time(), body, headers)
            if len(self._cache) > 5000:
                self._cache.clear()
            return body, headers

    def forget(self, token: str = None, email: str = None):
        """Сбросить кэш (после перевыпуска или удаления)."""
        if token:
            self._cache.pop(token, None)
            self._locks.pop(token, None)
        if email:
            for key in [k for k in self._last_good if k[1] == email]:
                self._last_good.pop(key, None)
