"""Защита адреса подписки: при блокировке IP в РФ домен переключается на доступную точку."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from harness import check, run  # noqa: E402
from rf_check import RFResult  # noqa: E402
import sub_guard  # noqa: E402
from sub_guard import SubGuard  # noqa: E402

BOT, R1, R2 = "198.51.100.1", "203.0.113.10", "203.0.113.20"
OK, BLOCKED, PARTIAL = RFResult(5, 5), RFResult(0, 5), RFResult(2, 5)


class World:
    def __init__(self, dns=BOT, states=None, ddns_ok=True):
        self.dns = dns
        self.states = states or {}
        self.ddns_ok = ddns_ok
        self.updates = []
        self.notes = []

    async def rf(self, targets):
        return {name: self.states.get(t.split(":")[0], OK) for name, t in targets.items()}

    async def resolve(self, host):
        return self.dns

    async def ddns(self, ip):
        if self.ddns_ok:
            self.updates.append(ip)
            self.dns = ip
        return self.ddns_ok

    async def notify(self, text, **kw):
        self.notes.append((kw.get("title"), kw.get("level"), text))


def guard(w, relays=(R1, R2), token="tok", host="nexusbot.duckdns.org"):
    return SubGuard(host, 8444, own_ip=BOT, relays=relays, duckdns_token=token, rf_check=w.rf,
                    notify=w.notify, resolve=w.resolve, duckdns_update=w.ddns)


async def test_ok_does_nothing():
    w = World()
    g = guard(w)
    for _ in range(3):
        check(await g.check() == "ok", "")
    check(not w.updates and not w.notes, "лишние действия при доступном адресе")
    check(g.last["ip"] == BOT and g.last["status"] == "ok", g.last)


async def test_switch_after_two_blocked_checks():
    w = World(states={BOT: BLOCKED})
    g = guard(w)
    check(await g.check() == "unknown" and not w.updates, "переключил после одной проверки")
    check(await g.check() == "switched", "не переключил после второй")
    check(w.updates == [R1], f"переключил на {w.updates}")
    check(w.notes[-1][0] == "Адрес подписки переключён" and w.notes[-1][1] == "ok", w.notes)
    check(await g.check() == "ok", "после переключения новый адрес не проверен")


async def test_single_glitch_resets():
    w = World(states={BOT: BLOCKED})
    g = guard(w)
    await g.check()
    w.states = {}
    await g.check()
    w.states = {BOT: BLOCKED}
    check(await g.check() == "unknown" and not w.updates, "случайный сбой check-host вызвал переключение")


async def test_skips_blocked_relay_prefers_ok_over_partial():
    w = World(states={BOT: BLOCKED, R1: PARTIAL, R2: OK})
    g = guard(w)
    await g.check()
    await g.check()
    check(w.updates == [R2], f"выбран {w.updates}, ждали полностью доступный {R2}")


async def test_back_to_bot_when_relay_blocked():
    w = World(dns=R1, states={R1: BLOCKED})
    g = guard(w)
    await g.check()
    await g.check()
    check(w.updates == [BOT], f"не вернулся на сервер бота: {w.updates}")


async def test_all_blocked_alerts():
    w = World(states={BOT: BLOCKED, R1: BLOCKED, R2: BLOCKED})
    g = guard(w)
    await g.check()
    check(await g.check() == "stuck" and not w.updates, "")
    check(w.notes[-1][0] == "Адрес подписки: все точки заблокированы", w.notes)


async def test_without_token_only_alerts():
    w = World(states={BOT: BLOCKED})
    g = guard(w, token="")
    await g.check()
    check(await g.check() == "stuck" and not w.updates, "")
    check("SUB_RELAYS" in w.notes[-1][2], w.notes)


async def test_not_duckdns_only_alerts():
    w = World(states={BOT: BLOCKED})
    g = guard(w, host="sub.example.com")
    await g.check()
    check(await g.check() == "stuck" and not w.updates, "переключение для не-DuckDNS домена")


async def test_duckdns_refused():
    w = World(states={BOT: BLOCKED}, ddns_ok=False)
    g = guard(w)
    await g.check()
    check(await g.check() == "stuck", "")
    check(w.notes[-1][0] == "Адрес подписки не переключён", w.notes)


async def test_relays_not_rate_limited():
    import aiohttp
    from link_server import LinkServer

    async def sub(token):
        return None
    srv = LinkServer(lambda x: None, host="127.0.0.1", port=18612, sub_content=sub, trusted_ips={"127.0.0.1"})
    await srv.start()
    try:
        async with aiohttp.ClientSession(trust_env=False) as http:
            codes = [(await http.get(f"http://127.0.0.1:18612/sub/{i:024d}")).status for i in range(40)]
    finally:
        await srv.stop()
    check(429 not in codes, "запросы через запасную точку упёрлись в лимит")


async def test_relay_script():
    import subprocess
    path = os.path.join(os.path.dirname(HERE), "deploy", "relay.sh")
    check(subprocess.run(["bash", "-n", path]).returncode == 0, "синтаксис relay.sh")
    text = open(path, encoding="utf-8").read()
    check("socat TCP-LISTEN:$PORT" in text and "DynamicUser=yes" in text, "")


sub_guard.CONFIRM_CHECKS = 2
run([test_ok_does_nothing, test_switch_after_two_blocked_checks, test_single_glitch_resets,
     test_skips_blocked_relay_prefers_ok_over_partial, test_back_to_bot_when_relay_blocked, test_all_blocked_alerts,
     test_without_token_only_alerts, test_not_duckdns_only_alerts, test_duckdns_refused, test_relays_not_rate_limited,
     test_relay_script])
