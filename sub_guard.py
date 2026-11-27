"""Защита адреса подписки от блокировки IP в РФ.

Подписку выдаёт бот по домену (NEXUS_LINK_BASE). Если IP, на который указывает
домен, заблокируют в России, приложения не смогут обновить подписку, хотя сами
VPN-серверы работают. Поэтому:

- раз в SUB_GUARD_MINUTES бот проверяет через check-host.net, открывается ли
  адрес подписки из России;
- две проверки подряд «недоступен» - бот выбирает точку входа, которая из
  России открывается: сервер бота или одну из запасных (deploy/relay.sh на
  VPN-серверах, SUB_RELAYS), и переключает на неё домен через DuckDNS;
- ссылки у людей не меняются: тот же домен, тот же сертификат на боте.

Без DUCKDNS_TOKEN или без запасных точек бот только предупреждает админа.
"""
import asyncio
import logging
import socket
import time

import httpx

log = logging.getLogger("sub_guard")

CONFIRM_CHECKS = 2   # столько проверок подряд «недоступен», прежде чем переключать


class SubGuard:
    def __init__(self, host: str, port: int, *, own_ip: str = "", relays=(), duckdns_token: str = "",
                 rf_check=None, notify=None, resolve=None, duckdns_update=None):
        self.host = host
        self.port = port
        self.own_ip = own_ip
        self.relays = [r for r in relays if r]
        self.token = duckdns_token
        self.rf_check = rf_check            # async ({имя: "ip:port"}) -> {имя: RFResult}
        self.notify = notify                # async (text, **kw)
        self.resolve = resolve or self._resolve
        self.duckdns_update = duckdns_update or self._duckdns_update
        self.blocked_streak = 0
        self.last = {}                      # для /audit: ip, status, ts, switched_to

    @property
    def duckdns_domain(self) -> str:
        return self.host[:-len(".duckdns.org")] if self.host.endswith(".duckdns.org") else ""

    @property
    def can_switch(self) -> bool:
        return bool(self.token and self.duckdns_domain and (self.relays or self.own_ip))

    async def _resolve(self, host: str) -> str:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=socket.AF_INET)
        return infos[0][4][0] if infos else ""

    async def _duckdns_update(self, ip: str) -> bool:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get("https://www.duckdns.org/update",
                               params={"domains": self.duckdns_domain, "token": self.token, "ip": ip})
        return r.text.strip() == "OK"

    def candidates(self) -> list:
        out = []
        for ip in [self.own_ip] + self.relays:
            if ip and ip not in out:
                out.append(ip)
        return out

    async def check(self) -> str:
        """Одна проверка. Возвращает: ok / blocked / switched / stuck / unknown."""
        try:
            current = await self.resolve(self.host)
        except OSError as e:
            log.warning("Не удалось узнать IP %s: %s", self.host, e)
            return "unknown"
        res = (await self.rf_check({"current": f"{current}:{self.port}"})).get("current")
        status = res.status if res is not None else "unknown"
        switched = self.last.get("switched_to")
        self.last = {"ip": current, "status": status, "ts": time.time()}
        if switched:
            self.last["switched_to"] = switched
        if status != "blocked":
            self.blocked_streak = 0
            return "ok" if status in ("ok", "partial") else "unknown"

        self.blocked_streak += 1
        if self.blocked_streak < CONFIRM_CHECKS:
            return "unknown"   # перепроверим в следующий раз: у check-host бывают сбои

        if not self.can_switch:
            await self.notify(
                "Из России не открывается адрес подписки: приложения не могут её обновить (сами VPN-серверы "
                "работают). Чтобы бот переключал адрес сам, поставьте запасную точку входа на VPN-сервер "
                "(`deploy/relay.sh`) и задайте `SUB_RELAYS` и `DUCKDNS_TOKEN` в .env.",
                title="Адрес подписки заблокирован в РФ", level="error", key="sub_blocked", cooldown=6 * 3600,
                fields=[("Адрес", f"{self.host} → {current}")])
            return "stuck"

        others = [ip for ip in self.candidates() if ip != current]
        results = await self.rf_check({ip: f"{ip}:{self.port}" for ip in others}) if others else {}
        best = None
        for wanted in ("ok", "partial"):
            best = next((ip for ip in others if results.get(ip) and results[ip].status == wanted), None)
            if best:
                break
        if best is None:
            await self.notify(
                "Ни сервер бота, ни запасные точки входа не открываются из России. Поставьте `relay.sh` на "
                "другой сервер и добавьте его IP в `SUB_RELAYS`.",
                title="Адрес подписки: все точки заблокированы", level="error", key="sub_stuck",
                cooldown=6 * 3600,
                fields=[(ip, results[ip].label if results.get(ip) else "нет данных") for ip in others[:10]])
            return "stuck"

        try:
            ok = await self.duckdns_update(best)
        except httpx.HTTPError as e:
            ok = False
            log.error("DuckDNS: %s", e)
        if not ok:
            await self.notify("DuckDNS не принял обновление. Проверьте `DUCKDNS_TOKEN`.",
                              title="Адрес подписки не переключён", level="error", key="sub_ddns",
                              cooldown=3600)
            return "stuck"
        self.blocked_streak = 0
        self.last["switched_to"] = best
        where = "сервер бота" if best == self.own_ip else "запасную точку входа"
        await self.notify(
            f"IP {current} заблокирован в России, домен {self.host} переключён на {where} {best}. Ссылки у людей "
            f"прежние, приложения обновят подписку в течение нескольких минут.",
            title="Адрес подписки переключён", level="ok")
        log.warning("Домен %s переключён с %s на %s", self.host, current, best)
        return "switched"
