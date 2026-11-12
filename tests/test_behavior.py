"""Правильное поведение: однократный trial, подарочные роли, гонки событий,
идемпотентность, защита сверки."""
import asyncio
import os
import subprocess
import sys
from harness import load, connect, reset, check, run, V, G, T, HOME, REPO
from fakes import Guild, Member

bot, db, main_p, milan, admin = load(extras=1)
home = Guild(HOME, roles=[V, G, T])


async def setup():
    if bot.xui is None:
        await connect(bot)
    reset(bot, db, main_p, milan)
    home._m.clear()
    bot.client.guilds = [home]


async def test_trial_only_once():
    u = Member(1, T, guild=home)
    await bot.sync_member(u, trial_just_added=True)
    check(db.get_subscription("1")["kind"] == "trial", "trial не выдан")
    u.set_roles()
    await bot.sync_member(u)
    u.set_roles(T)
    await bot.sync_member(u, trial_just_added=True)
    check(db.get_subscription("1") is None, "второй trial выдан после снятия и возврата роли")
    home.remove(u)
    await bot.on_member_remove(u)
    u2 = Member(1, T, guild=home)
    await bot.sync_member(u2, trial_just_added=True)
    check(db.get_subscription("1") is None, "второй trial выдан после перезахода на сервер")
    out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {REPO!r}); import db; "
                          "print(db.has_used_trial('1'))"], capture_output=True, text=True, env=os.environ).stdout
    check(out.strip() == "True", "отметка об использованном trial не пережила перезапуск")


async def test_gift_and_boosty_switch_keeps_link():
    g = Member(2, V, G, guild=home)
    await bot.sync_member(g)
    sid = db.get_subscription("2")["sub_id"]
    g.set_roles(G)
    await bot.sync_member(g)
    check(db.get_subscription("2")["sub_id"] == sid, "смена Boosty на подарочную роль поменяла ссылку")
    g.set_roles(V)
    await bot.sync_member(g)
    check(db.get_subscription("2")["sub_id"] == sid, "смена подарочной на Boosty поменяла ссылку")
    g.set_roles()
    await bot.sync_member(g)
    check(db.get_subscription("2") is None and "discord_2" not in main_p.clients
          and "discord_2" not in milan.clients, "после снятия последней роли клиент остался")


async def test_trial_to_paid_upgrade():
    u = Member(3, T, guild=home)
    await bot.sync_member(u, trial_just_added=True)
    u.set_roles(T, V)
    actions = await bot.sync_member(u)
    sub = db.get_subscription("3")
    check(actions == ["upgraded"] and sub["kind"] == "paid", f"trial не заменён платной: {actions}")
    check(main_p.clients["discord_3"]["expiryTime"] == 0, "платный клиент со сроком")


async def test_concurrent_events_create_once():
    c = Member(4, V, guild=home)
    n0 = sum(1 for m, p in main_p.calls if p.endswith("/clients/add"))
    blank = Member(4)
    blank.guild = home
    await asyncio.gather(*[bot.on_member_update(blank, c) for _ in range(5)], bot.reconcile(), bot.sync_member(c))
    adds = sum(1 for m, p in main_p.calls if p.endswith("/clients/add")) - n0
    check(adds == 1, f"клиент создан {adds} раз")
    check(len(c.dms) == 1, f"ссылка отправлена {len(c.dms)} раз")


async def test_lost_response_no_duplicate():
    d = Member(5, V, guild=home)
    real_find = bot.xui.find_client
    state = {"n": 0}

    async def flaky_find(email):
        state["n"] += 1
        if state["n"] == 2:
            raise bot.XUIError("timeout")      # после успешного POST add
        return await real_find(email)
    bot.xui.find_client = flaky_find
    try:
        await bot.sync_member(d)
    except bot.XUIError:
        pass
    finally:
        bot.xui.find_client = real_find
    await bot.sync_member(d)
    check(db.get_subscription("5")["sub_id"] == main_p.clients["discord_5"]["subId"], "база и панель расходятся")


async def test_crash_between_panel_and_db():
    d = Member(6, V, guild=home)
    await bot.sync_member(d)
    d.set_roles()
    real_del = db.delete_subscription

    def crash(_):
        raise KeyboardInterrupt
    db.delete_subscription = crash
    try:
        await bot.sync_member(d)
    except KeyboardInterrupt:
        pass
    finally:
        db.delete_subscription = real_del
    check(db.get_subscription("6") is not None, "")
    await bot.reconcile()
    check(db.get_subscription("6") is None, "сверка не дочистила запись после «падения»")


async def test_missing_roles_on_server_skips():
    ms = [Member(i, V, guild=home) for i in range(100, 110)]
    for m in ms:
        await bot.sync_member(m)
    bot.client.guilds = [Guild(HOME, ms, ["Other"])]
    st = await bot.reconcile()
    check(st.get("skipped") and len(db.list_subscriptions()) == 10, "роли пропали с сервера - а подписки удалены")
    bot.client.guilds = []
    st = await bot.reconcile()
    check(st.get("skipped") and len(db.list_subscriptions()) == 10, "сервер пропал - а подписки удалены")


async def test_unchunked_guild_keeps_absent():
    m = Member(7, V, guild=home)
    await bot.sync_member(m)
    home.remove(m)
    home.chunked = False

    async def chunk():
        raise RuntimeError("discord недоступен")
    home.chunk = chunk
    try:
        await bot.reconcile()
    finally:
        home.chunked = True
        del home.chunk
    check(db.get_subscription("7") is not None, "участник удалён, хотя список участников не загрузился")


async def test_outage_keeps_records():
    ms = [Member(i, V, guild=home) for i in range(200, 205)]
    for m in ms:
        await bot.sync_member(m)
    ms[0].set_roles()
    main_p.down = milan.down = True
    st = await bot.reconcile(force_check=True)
    main_p.down = milan.down = False
    check(len(db.list_subscriptions()) == 5 and st.get("errors", 0) >= 1, f"сбой панелей: {dict(st)}")


run([test_trial_only_once, test_gift_and_boosty_switch_keeps_link, test_trial_to_paid_upgrade,
     test_concurrent_events_create_once, test_lost_response_no_duplicate, test_crash_between_panel_and_db,
     test_missing_roles_on_server_skips, test_unchunked_guild_keeps_absent, test_outage_keeps_records], setup=setup)
