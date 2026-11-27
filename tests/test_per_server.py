"""Ссылка на каждый сервер (SUB_PER_SERVER): у человека постоянная ссылка на
каждом сервере, и в каждой - все страны."""
import base64
import re
from urllib.parse import unquote
from harness import load, connect, reset, check, run, V, G, T, HOME
from fakes import Guild, Member
import httpx

bot, db, main_p, milan, admin = load(extras=1, sub_on_panel=True, SUB_PER_SERVER="true",
                                     XUI_NAME="🇫🇮 Финляндия", XUI2_NAME="🇳🇱 Нидерланды")
home = Guild(HOME, roles=[V, G, T])
FI_SUB, NL_SUB = main_p.url + "/subsrv", milan.url + "/subsrv"


async def setup():
    if bot.xui is None or bot.extra_panels[0].api is None:
        await connect(bot)
    reset(bot, db, main_p, milan)
    bot._mirrors_ok.clear()
    home._m.clear()
    bot.client.guilds = [home]
    admin.clear()


def tok(did):
    return db.get_token(str(did), create=False)


async def fetch(url):
    async with httpx.AsyncClient(trust_env=False) as http:
        r = await http.get(url)
    if r.status_code != 200:
        return None
    return [unquote(l.rsplit("#", 1)[1]) + " @" + l.split("@")[1].split(":")[0]
            for l in base64.b64decode(r.text).decode().splitlines()]


async def test_grant_gives_link_per_server_each_with_all():
    m = Member(1, V, guild=home)
    await bot.sync_member(m)
    t = tok(1)
    check(main_p.clients["discord_1"]["subId"] == t and milan.clients["discord_1"]["subId"] == t,
          "subId на серверах не совпадает с токеном")
    dm = m.dms[0]
    check(f"{FI_SUB}/{t}" in dm and f"{NL_SUB}/{t}" in dm and "Запасные ссылки" in dm, dm)
    fi, nl = await fetch(f"{FI_SUB}/{t}"), await fetch(f"{NL_SUB}/{t}")
    check(sorted(x.split(" @")[1] for x in fi) == ["fi.example", "nl.example"], f"в ссылке Финляндии: {fi}")
    check(sorted(x.split(" @")[1] for x in nl) == ["fi.example", "nl.example"], f"в ссылке NL: {nl}")
    check("🇳🇱 Нидерланды @nl.example" in fi and "🇫🇮 Финляндия @fi.example" in nl, "имена серверов")


async def test_only_configs_no_subscription_loops():
    m = Member(2, V, guild=home)
    await bot.sync_member(m)
    for p in (main_p, milan):
        ext = p.links["discord_2"]
        check(ext and all(e["kind"] == "link" and e["namePrefix"] == "nexus-bot" for e in ext),
              f"внешние подписки вместо конфигов: {ext}")


async def test_manual_links_and_old_scheme_cleanup():
    m = Member(3, V, guild=home)
    await bot.sync_member(m)
    main_p.links["discord_3"] += [
        {"kind": "link", "value": "trojan://x@manual.example:443#Ручной", "remark": "Ручной", "enable": True},
        {"kind": "subscription", "value": NL_SUB + "/old", "remark": "старая схема", "enable": True}]
    await bot.reconcile(force_check=True)
    vals = [e["value"] for e in main_p.links["discord_3"]]
    check("trojan://x@manual.example:443#Ручной" in vals, "ручная ссылка стёрта")
    check(NL_SUB + "/old" not in vals, "внешняя подписка прежней схемы осталась")


async def test_migration_existing_clients():
    """Клиенты со старыми случайными subId: пересоздаются с токеном, ссылки - один раз."""
    bot.SUB_PER_SERVER = False
    m = Member(4, V, guild=home)
    await bot.sync_member(m)
    old = main_p.clients["discord_4"]["subId"]
    bot.SUB_PER_SERVER = True
    m.dms.clear()
    await bot.reconcile(force_check=True)
    await bot.reconcile()
    t = tok(4)
    check(old != t and main_p.clients["discord_4"]["subId"] == t and milan.clients["discord_4"]["subId"] == t,
          "клиенты не переведены на постоянные ссылки")
    check(len(m.dms) == 1 and f"{NL_SUB}/{t}" in m.dms[0], f"сообщений: {len(m.dms)}")
    check(len(await fetch(f"{NL_SUB}/{t}")) == 2, "в ссылке не все серверы")


async def test_reissue_changes_all_links():
    m = Member(5, V, guild=home)
    await bot.sync_member(m)
    old = tok(5)
    link, failed = await bot.reissue_link("5")
    new = tok(5)
    check(new != old and not failed and link.endswith(new), "перевыпуск не сменил ссылки")
    check(await fetch(f"{FI_SUB}/{old}") is None and await fetch(f"{NL_SUB}/{old}") is None,
          "старые ссылки работают после перевыпуска")
    nl = await fetch(f"{NL_SUB}/{new}")
    check(len(nl) == 2, f"новая ссылка без всех серверов: {nl}")
    fi_cfg = [e["value"] for e in milan.links["discord_5"]][0]
    check(main_p.clients["discord_5"]["uuid"] in fi_cfg, "в NL старый конфиг Финляндии")


async def test_offline_server_does_not_shrink():
    m = Member(6, V, guild=home)
    await bot.sync_member(m)
    before = list(main_p.links["discord_6"])
    saved = bot.extra_panels[0].api
    bot.extra_panels[0].api = None
    await bot.reconcile(force_check=True)
    bot.extra_panels[0].api = saved
    check(main_p.links["discord_6"] == before, "без доп. сервера его конфиги убраны из ссылок")


async def test_recreated_client_keeps_links():
    """Клиент пропал на сервере - пересоздаётся с тем же subId, ссылки прежние, сообщений нет."""
    m = Member(7, V, guild=home)
    await bot.sync_member(m)
    t, n = tok(7), len(m.dms)
    milan.clients.pop("discord_7")
    await bot.reconcile(force_check=True)
    check(milan.clients["discord_7"]["subId"] == t and len(m.dms) == n, "ссылка сменилась или ушло сообщение")
    fi_ext = [e["value"] for e in main_p.links["discord_7"]]
    check(milan.clients["discord_7"]["uuid"] in fi_ext[0], "в Финляндии старый конфиг NL")


async def test_revoke_removes_everywhere():
    m = Member(8, V, guild=home)
    await bot.sync_member(m)
    t = tok(8)
    m.set_roles()
    await bot.sync_member(m)
    check(await fetch(f"{FI_SUB}/{t}") is None and await fetch(f"{NL_SUB}/{t}") is None, "ссылки работают после отзыва")


async def test_with_bot_subscription_no_duplicates():
    bot.BOT_SUBSCRIPTION = True
    bot.NEXUS_LINK_BASE = "https://bot.example:8444"
    try:
        m = Member(9, V, guild=home)
        await bot.sync_member(m)
        res = await bot.serve_subscription(tok(9))
        lines = base64.b64decode(res[0]).decode().splitlines()
        check(len(lines) == 2, f"в подписке от бота дубли: {len(lines)}")
        links = bot.all_links(db.get_subscription("9"))
        check(links[0][1] == f"https://bot.example:8444/sub/{tok(9)}" and len(links) == 3, links)
    finally:
        bot.BOT_SUBSCRIPTION = False
        bot.NEXUS_LINK_BASE = ""


async def test_myvpn_shows_all_links():
    m = Member(10, V, guild=home)
    await bot.sync_member(m)
    text = "Ссылка: " + bot.user_link(db.get_subscription("10")) + bot.extra_links_text(
        bot.user_link(db.get_subscription("10")))
    check(re.search(r"Нидерланды: .*/subsrv/" + re.escape(tok(10)), text), text)


run([test_grant_gives_link_per_server_each_with_all, test_only_configs_no_subscription_loops,
     test_manual_links_and_old_scheme_cleanup, test_migration_existing_clients, test_reissue_changes_all_links,
     test_offline_server_does_not_shrink, test_recreated_client_keeps_links, test_revoke_removes_everywhere,
     test_with_bot_subscription_no_duplicates, test_myvpn_shows_all_links], setup=setup)
