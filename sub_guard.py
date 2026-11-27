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
import ipaddress
import logging
import socket
import ssl
import time

import httpx

log = logging.getLogger("sub_guard")

CONFIRM_CHECKS = 2   # столько проверок подряд «недоступен», прежде чем переключать


class SubGuard:
    def __init__(self, host: str, port: int, *, own_ip: str = "", relays=(), duckdns_token: str = "",
                 rf_check=None, notify=None, resolve=None, duckdns_update=None, probe=None, trusted=None):
        self.host = host
        self.port = port
        self.own_ip = own_ip
        # IP или домены запасных точек (домен - адрес VPN-сервера из .env и т.п.)
        self.relays = [r for r in relays if r and r != host]
        self.token = duckdns_token
        self.rf_check = rf_check            # async ({имя: "ip:port"}) -> {имя: RFResult}
        self.notify = notify                # async (text, **kw)
        self.resolve = resolve or self._resolve
        self.duckdns_update = duckdns_update or self._duckdns_update
        self.probe = probe or self._probe
        # Множество IP запасных точек - то же, что у переходника (без лимита частоты).
        self.trusted = trusted if trusted is not None else set()
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

    async def _ip_of(self, name: str) -> str:
        try:
            ipaddress.IPv4Address(name)
            return name
        except ValueError:
            pass
        try:
            return await self.resolve(name)
        except OSError as e:
            log.warning("Не удалось узнать IP запасной точки %s: %s", name, e)
            return ""

    async def relay_ips(self) -> list:
        """IP запасных точек (домены разрешаются при каждой проверке)."""
        ips = []
        for name in self.relays:
            ip = await self._ip_of(name)
            if ip and ip not in ips:
                ips.append(ip)
        self.trusted.clear()
        self.trusted.update(ips)
        return ips

    async def candidates(self) -> list:
        out = []
        for ip in [self.own_ip] + await self.relay_ips():
            if ip and ip not in out:
                out.append(ip)
        return out

    async def _probe(self, ip: str) -> bool:
        """Открывается ли подписка через этот IP: TLS с именем домена, ответ /health = ok.
        Так бот не переключит домен на сервер, где точка входа не установлена."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                ip, self.port, ssl=ssl.create_default_context(), server_hostname=self.host), 10)
            writer.write(f"GET /health HTTP/1.1\r\nHost: {self.host}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), 10)
            writer.close()
            head, _, body = data.decode("latin-1").partition("\r\n\r\n")
            return head.startswith("HTTP/1.1 200") and body.strip().endswith("ok")
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            return False

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

        others = [ip for ip in await self.candidates() if ip != current]
        # Только те, через которые подписка действительно открывается (точка входа стоит).
        alive = await asyncio.gather(*(self.probe(ip) for ip in others))
        others = [ip for ip, ok in zip(others, alive) if ok]
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
