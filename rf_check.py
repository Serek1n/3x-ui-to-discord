"""
Проверка, доступен ли сервер из России, через узлы check-host.net в РФ.

Делается TCP-подключение к host:port с российских узлов (Москва, Санкт-
Петербург и др.). Это показывает, не заблокирован ли IP/порт сервера.
Блокировку самого VPN-протокола по DPI так обнаружить нельзя - подключение
по TCP при этом обычно проходит.

Результат кэшируется на CACHE_SEC, чтобы не дёргать сервис на каждый
/status_server.
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger("rf_check")

API = "https://check-host.net"
CACHE_SEC = 300
NODES_CACHE_SEC = 24 * 3600
MAX_NODES = 5

MIN_NODES_FOR_BLOCKED = 3
_nodes_cache: tuple[float, list] = (0.0, [])
_result_cache: dict = {}


class RFResult:
    def __init__(self, ok: int = 0, total: int = 0, cities=None, error: str = ""):
        self.ok = ok
        self.total = total
        self.cities = cities or []
        self.error = error

    @property
    def status(self) -> str:
        """'ok' | 'partial' | 'blocked' | 'unknown'"""
        if self.error or self.total == 0:
            return "unknown"
        if self.ok == self.total:
            return "ok"
        if self.ok == 0:
            # Один-два ответивших узла - слишком мало, чтобы утверждать о
            # блокировке: у самих узлов check-host бывают сбои.
            return "blocked" if self.total >= MIN_NODES_FOR_BLOCKED else "unknown"
        return "partial"

    @property
    def label(self) -> str:
        return {
            "ok": "Доступен",
            "partial": f"Частично ({self.ok}/{self.total})",
            "blocked": "Недоступен",
            "unknown": "Нет данных",
        }[self.status]

    def __repr__(self):
        return f"RFResult({self.status}, {self.ok}/{self.total}, {self.error!r})"


async def _ru_nodes(http: httpx.AsyncClient) -> list:
    global _nodes_cache
    ts, nodes = _nodes_cache
    if nodes and time.time() - ts < NODES_CACHE_SEC:
        return nodes
    r = await http.get(f"{API}/nodes/hosts")
    r.raise_for_status()
    data = r.json().get("nodes") or {}
    ru = [(host, (info.get("location") or ["", "", ""])[2] if len(info.get("location") or []) > 2 else "")
          for host, info in data.items()
          if (info.get("location") or [""])[0].lower() == "ru"]
    ru = ru[:MAX_NODES]
    _nodes_cache = (time.time(), ru)
    return ru


async def check_targets(targets: dict, timeout: float = 15.0) -> dict:
    """targets: {название: "host:port"} -> {название: RFResult}."""
    results: dict = {}
    pending = {}
    now = time.time()
    for name, target in targets.items():
        cached = _result_cache.get(target)
        if cached and now - cached[0] < CACHE_SEC:
            results[name] = cached[1]
        else:
            pending[name] = target
    if not pending:
        return results

    try:
        async with httpx.AsyncClient(timeout=10, headers={"Accept": "application/json"}) as http:
            nodes = await _ru_nodes(http)
            if not nodes:
                raise RuntimeError("у check-host.net нет узлов в РФ")
            city_by_node = dict(nodes)

            request_ids = {}
            for name, target in pending.items():
                params = [("host", target)] + [("node", host) for host, _ in nodes]
                r = await http.get(f"{API}/check-tcp", params=params)
                r.raise_for_status()
                request_ids[name] = r.json()["request_id"]

            deadline = time.monotonic() + timeout
            raw = {}
            while request_ids and time.monotonic() < deadline:
                await asyncio.sleep(1.5)
                for name, rid in list(request_ids.items()):
                    r = await http.get(f"{API}/check-result/{rid}")
                    r.raise_for_status()
                    data = r.json()
                    raw[name] = data
                    if all(v is not None for v in data.values()):
                        del request_ids[name]

            for name in pending:
                data = raw.get(name) or {}
                ok, total, cities = 0, 0, []
                for node, value in data.items():
                    if value is None:
                        continue  # узел не успел ответить
                    total += 1
                    first = value[0] if isinstance(value, list) and value else None
                    if isinstance(first, dict) and "time" in first and not first.get("error"):
                        ok += 1
                        if city_by_node.get(node):
                            cities.append(city_by_node[node])
                res = RFResult(ok, total, cities)
                results[name] = res
                _result_cache[pending[name]] = (time.time(), res)
    except Exception as e:
        log.warning("Проверка из РФ не удалась: %s", e)
        for name in pending:
            results.setdefault(name, RFResult(error=str(e)))
    return results
