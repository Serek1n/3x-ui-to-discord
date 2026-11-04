"""
Клиент 3x-ui через официальный API с токеном (Bearer).

Токен создаётся в панели: Настройки → Безопасность → API Token (scope admin).
С токеном изменяющие запросы (создание/удаление клиента, внешние подписки)
проходят без CSRF и без браузера - это быстрее, надёжнее и не ломается при
обновлении вёрстки панели. Документация: https://docs.sanaei.dev/docs/reference/api/

Интерфейс совместим с браузерным XUIClient (add_client, delete_client,
find_client, get_client_traffic, close) + дополнительные методы, которые
доступны только через API (IP клиента, внешние подписки, статус сервера).
"""

import ipaddress
import json
import logging
import re
import secrets
import string
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from xui_client import XUIError

log = logging.getLogger("xui.api")

DAY_MS = 86_400_000


def _q(value: str) -> str:
    return quote(str(value), safe="")


def gen_sub_id(n: int = 16) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


class XUIApi:
    def __init__(self, base_url: str, token: str, *, verify_ssl: bool = True,
                 web_base_path: str = "", timeout: int = 20, name: str = "",
                 inbound_ids: str = "all"):
        host = base_url.rstrip("/")
        if host.lower().endswith("/login"):
            host = host[: -len("/login")]
        path = web_base_path.strip("/")
        if path and not host.endswith("/" + path):
            host = f"{host}/{path}"
        self.base_url = host
        self.name = name or host
        self.login_debug_info = "API-токен"
        self._inbound_ids_setting = (inbound_ids or "all").strip().lower()
        self._inbound_ids_cache: list | None = None
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=False,
        )

    @classmethod
    async def create(cls, base_url: str, token: str, **kwargs):
        self = cls(base_url, token, **kwargs)
        try:
            ids = await self.inbound_ids()
        except XUIError:
            await self.close()
            raise
        self.login_debug_info = f"API-токен, inbound'ы: {ids}"
        return self

    async def close(self):
        try:
            await self._http.aclose()
        except Exception:
            pass

    # ---------- низкий уровень ----------

    async def _req(self, method: str, path: str, body=None, *, allow_fail: bool = False):
        try:
            r = await self._http.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise XUIError(f"[{self.name}] нет связи с панелью ({path}): {e}")
        if r.status_code in (401, 403):
            raise XUIError(f"[{self.name}] панель отклонила токен ({r.status_code}) на {path} - "
                           f"проверьте API-токен (scope admin) и XUI_WEB_BASE_PATH")
        if r.status_code == 404:
            raise XUIError(f"[{self.name}] {path} не найден (404) - проверьте адрес панели и "
                           f"XUI_WEB_BASE_PATH")
        try:
            data = r.json()
        except ValueError:
            raise XUIError(f"[{self.name}] не-JSON ответ на {path} (HTTP {r.status_code}): {r.text[:200]}")
        if not isinstance(data, dict):
            raise XUIError(f"[{self.name}] неожиданный ответ на {path}: {str(data)[:200]}")
        if not data.get("success") and not allow_fail:
            raise XUIError(f"[{self.name}] {path}: {data.get('msg') or data}")
        return data

    # ---------- inbound'ы ----------

    async def inbound_ids(self) -> list:
        if self._inbound_ids_cache is not None:
            return self._inbound_ids_cache
        data = await self._req("GET", "/panel/api/inbounds/list")
        inbounds = data.get("obj") or []
        all_ids = [ib.get("id") for ib in inbounds if isinstance(ib, dict) and ib.get("id") is not None]
        enabled = [ib.get("id") for ib in inbounds
                   if isinstance(ib, dict) and ib.get("id") is not None and ib.get("enable", True)]
        if self._inbound_ids_setting in ("", "all"):
            ids = enabled or all_ids
        else:
            wanted = [int(x) for x in re.split(r"[\s,;]+", self._inbound_ids_setting) if x.isdigit()]
            ids = [i for i in wanted if i in all_ids]
            missing = set(wanted) - set(ids)
            if missing:
                log.warning("[%s] inbound'ов %s нет в панели", self.name, sorted(missing))
        if not ids:
            raise XUIError(f"[{self.name}] в панели нет подходящих inbound'ов")
        self._inbound_ids_cache = ids
        return ids

    # ---------- клиенты ----------

    async def find_client(self, email: str):
        """Объект трафика клиента (subId, uuid, expiryTime, up, down...) или None."""
        data = await self._req("GET", f"/panel/api/clients/traffic/{_q(email)}", allow_fail=True)
        obj = data.get("obj")
        if data.get("success") and isinstance(obj, dict) and obj.get("email") == email:
            return obj
        return None

    async def get_client_traffic(self, email: str):
        try:
            return await self.find_client(email)
        except XUIError as e:
            log.warning("%s", e)
            return None

    async def add_client(self, inbound_id: int, email: str, days: int, total_gb: int = 0,
                          ip_limit: int = 0, auto_renew_days: int = 0, *,
                          expiry_time: int | None = None, comment: str = ""):
        """Создаёт клиента. days>0 - срок с первого подключения (expiryTime < 0).
        expiry_time задаёт expiryTime напрямую (мс; <0 - длительность от первого
        подключения). Если клиент с таким email уже есть - он пересоздаётся."""
        if await self.find_client(email):
            log.info("[%s] клиент %s уже есть - удаляю перед созданием", self.name, email)
            await self.delete_client(inbound_id, "", email=email)

        if expiry_time is None:
            expiry_time = -days * DAY_MS if days > 0 else 0
        sub_id = gen_sub_id()
        client = {
            "email": email,
            "enable": True,
            "expiryTime": int(expiry_time),
            "totalGB": int(total_gb) * 1024 ** 3,
            "limitIp": int(ip_limit or 0),
            "subId": sub_id,
            "tgId": 0,
            "comment": comment,
            "reset": int(auto_renew_days or 0),
        }
        await self._req("POST", "/panel/api/clients/add",
                        {"client": client, "inboundIds": await self.inbound_ids()})

        found = await self.find_client(email)
        if not found:
            raise XUIError(f"[{self.name}] клиент {email} не появился после создания")
        real_sub = found.get("subId") or sub_id
        real_uuid = found.get("uuid") or str(found.get("id") or "")
        if expiry_time < 0:
            expiry_ms = int(time.time() * 1000) - int(expiry_time)
        else:
            expiry_ms = int(expiry_time)
        return {"uuid": real_uuid, "subId": real_sub, "expiryMs": expiry_ms}

    async def delete_client(self, inbound_id: int, client_uuid: str, email: str = ""):
        if not email:
            raise XUIError("delete_client требует email")
        if not await self.find_client(email):
            log.info("[%s] клиента %s уже нет - считаю удалённым", self.name, email)
            return
        await self._req("POST", f"/panel/api/clients/del/{_q(email)}?keepTraffic=0")
        if await self.find_client(email):
            raise XUIError(f"[{self.name}] после удаления клиент {email} всё ещё есть")

    async def get_client_ips(self, email: str) -> list:
        """Список строк вида 'ip (время)'."""
        data = await self._req("POST", f"/panel/api/clients/ips/{_q(email)}", allow_fail=True)
        obj = data.get("obj")
        if isinstance(obj, str):
            text = obj.strip()
            if not text or "no ip" in text.lower():
                return []
            try:
                obj = json.loads(text)
            except ValueError:
                obj = [p for p in re.split(r"[\n,]+", text) if p.strip()]
        if isinstance(obj, list):
            return [str(x).strip() for x in obj if str(x).strip()]
        return []

    async def get_external_links(self, email: str, sub_id: str) -> list:
        data = await self._req("GET", f"/panel/api/clients/get/subId/{_q(sub_id)}", allow_fail=True)
        for item in data.get("obj") or []:
            if not isinstance(item, dict):
                continue
            client = item.get("client") or {}
            if client.get("email") == email or not email:
                return list(item.get("externalLinks") or [])
        return []

    async def set_external_subscriptions(self, email: str, sub_id: str, managed: dict,
                                         managed_prefixes: list, preserve: list | None = None):
        """Прописывает клиенту внешние подписки других серверов.
        managed: {название: url}. Ссылки, которые админ добавил вручную (не с
        наших серверов), сохраняются. preserve - ссылки, снятые до пересоздания
        клиента (при перевыпуске), их ручная часть тоже сохраняется."""
        current = await self.get_external_links(email, sub_id)
        if preserve:
            known = {str(link.get("value") or "") for link in current}
            current = current + [link for link in preserve if str(link.get("value") or "") not in known]
        keep = []
        for link in current:
            value = str(link.get("value") or "")
            if any(value.startswith(p) for p in managed_prefixes if p):
                continue
            keep.append({k: link[k] for k in ("kind", "value", "remark", "enable", "expiryTime", "namePrefix")
                         if k in link})
        for remark, url in managed.items():
            keep.append({"kind": "subscription", "value": url, "remark": remark,
                         "enable": True, "expiryTime": 0, "namePrefix": ""})
        await self._req("POST", f"/panel/api/clients/{_q(email)}/externalLinks", {"externalLinks": keep})

    async def has_external_links(self, email: str, sub_id: str, urls: list) -> bool:
        values = {str(link.get("value") or "") for link in await self.get_external_links(email, sub_id)}
        return all(u in values for u in urls)

    async def server_status(self) -> dict:
        data = await self._req("GET", "/panel/api/server/status")
        return data.get("obj") or {}


# ---------- разбор IP-логов ----------

_IP_RE = re.compile(r"^\s*([0-9a-fA-F:.]+)\s*(?:\((.*?)\))?\s*$")


def _parse_ts(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        v = int(raw)
        return v / 1000 if v > 10 ** 11 else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def recent_ips(entries: list, window_sec: int = 86400, now: float | None = None) -> set:
    """Уникальные IP за последние window_sec. Запись без понятного времени
    считается свежей."""
    now = now or time.time()
    out = set()
    for entry in entries:
        m = _IP_RE.match(str(entry))
        if not m:
            continue
        try:
            ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        ts = _parse_ts(m.group(2) or "")
        if ts is None or now - ts <= window_sec:
            out.add(m.group(1))
    return out
