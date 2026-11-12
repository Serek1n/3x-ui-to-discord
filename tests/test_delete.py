"""Удаление и самовосстановление при сбоях панелей (два сервера)."""
import time
from harness import load, connect, reset, check, run, V, G, T, HOME, DAY
from fakes import Guild, Member

bot, db, main_p, milan, admin = load(extras=1)
home = Guild(HOME, roles=[V, G, T])


async def setup():
    if bot.xui is None:
        await connect(bot)
    for p in bot.extra_panels:
        if p.api is None:
            await connect(bot)
    reset(bot, db, main_p, milan)
    home._m.clear()
    bot.client.guilds = [home]


async def test_grant_creates_on_both_and_links():
    m = Member(10, V, guild=home)
    await bot.sync_member(m)
    sub = db.get_subscription("10")
    check("discord_10" in main_p.clients and "discord_10" in milan.clients, "клиент создан не на всех серверах")
    url = f"https://m.example:2096/s/{milan.clients['discord_10']['subId']}"
    check(url in main_p.linked("discord_10"), "подписка Милана не прописана в основную")
    check(sub["sub_id"] == main_p.clients["discord_10"]["subId"], "subId в базе не совпадает с панелью")


async def test_extra_down_main_still_deleted_and_retried():
    """DEL-03: недоступный доп. сервер не мешает удалить клиента на основном,
    запись остаётся, удаление доводится после восстановления."""
    m = Member(1, V, guild=home)
    await bot.sync_member(m)
    milan.down = True
    m.set_roles()
    try:
        await bot.sync_member(m)
    except bot.XUIError:
        pass
    check("discord_1" not in main_p.clients, "на основном сервере клиент не удалён")
    check(db.get_subscription("1") is not None, "запись удалена, хотя на Милане клиент остался")
    milan.down = False
    await bot.process_due_revokes()
    check("discord_1" not in milan.clients and db.get_subscription("1") is None, "удаление не доведено до конца")


async def test_extra_not_connected_keeps_record():
    """DEL-04: доп. сервер не подключён - запись не удаляется, клиент на нём не теряется."""
    m = Member(2, V, guild=home)
    await bot.sync_member(m)
    saved = bot.extra_panels[0].api
    bot.extra_panels[0].api = None
    m.set_roles()
    try:
        await bot.sync_member(m)
    except bot.XUIError:
        pass
    check(db.get_subscription("2") is not None, "запись удалена, пока доп. сервер не подключён")
    bot.extra_panels[0].api = saved
    await bot.process_due_revokes()
    check("discord_2" not in milan.clients and db.get_subscription("2") is None, "после подключения не удалено")


async def test_panel_error_is_not_absence():
    """DEL-05: ответ success:false - это ошибка, а не «клиента нет»."""
    m = Member(3, V, guild=home)
    await bot.sync_member(m)
    m.set_roles()
    main_p.fail_traffic = 1
    try:
        await bot.sync_member(m)
    except bot.XUIError:
        pass
    check(db.get_subscription("3") is not None, "запись удалена по ошибке панели")
    await bot.process_due_revokes()
    check("discord_3" not in main_p.clients and db.get_subscription("3") is None, "повтор удаления не сработал")


async def test_trial_not_started_survives_panel_error():
    """TRL-01: сбой панели не сжигает неначатый пробный период."""
    m = Member(4, T, guild=home)
    await bot.sync_member(m, trial_just_added=True)
    check(main_p.clients["discord_4"]["expiryTime"] == -10 * DAY, "trial создан не «с первого подключения»")
    db.update_expiry("4", int(time.time() * 1000) - 1000)
    main_p.fail_traffic = 1
    milan.fail_traffic = 1
    await bot.check_expired.coro()
    check(db.get_subscription("4") is not None, "неначатый trial удалён из-за сбоя панели")
    await bot.check_expired.coro()
    check(db.get_subscription("4") is not None, "неначатый trial удалён без сбоя")


async def test_trial_started_expires():
    m = Member(5, T, guild=home)
    await bot.sync_member(m, trial_just_added=True)
    past = int(time.time() * 1000) - 1000
    main_p.clients["discord_5"]["expiryTime"] = past
    milan.clients["discord_5"]["expiryTime"] = past
    await bot.check_expired.coro()
    check(db.get_subscription("5") is None and "discord_5" not in main_p.clients, "истёкший trial не удалён")
    check(db.has_used_trial("5"), "trial не отмечен использованным")


async def test_trial_reminder_once():
    m = Member(6, T, guild=home)
    await bot.sync_member(m, trial_just_added=True)
    soon = int(time.time() * 1000) + DAY // 2
    main_p.clients["discord_6"]["expiryTime"] = soon
    await bot.check_expired.coro()
    await bot.check_expired.coro()
    n = sum(1 for t in m.dms if t and "закончится" in t)
    check(n == 1, f"напоминание отправлено {n} раз")


async def test_missing_paid_client_restored():
    """REL-02: клиент пропал из панели при живой роли - сверка его восстанавливает."""
    m = Member(7, V, guild=home)
    await bot.sync_member(m)
    main_p.clients.pop("discord_7")
    await bot.reconcile(force_check=True)
    check("discord_7" in main_p.clients, "пропавший клиент не восстановлен")
    check(db.get_subscription("7")["sub_id"] == main_p.clients["discord_7"]["subId"], "в базе старый subId")
    check(any("восстановлена" in (t or "") for t in m.dms), "человеку не отправлена новая ссылка")


async def test_extra_missing_is_recreated():
    """REL-03: клиент не создался на доп. сервере при выдаче - сверка досоздаёт."""
    milan.down = True
    m = Member(8, V, guild=home)
    await bot.sync_member(m)
    milan.down = False
    check("discord_8" not in milan.clients, "")
    await bot.reconcile()
    check("discord_8" in milan.clients, "клиент на доп. сервере не досоздан")
    url = f"https://m.example:2096/s/{milan.clients['discord_8']['subId']}"
    check(url in main_p.linked("discord_8"), "подписка доп. сервера не прописана")


async def test_manual_links_kept():
    m = Member(9, V, guild=home)
    await bot.sync_member(m)
    main_p.links["discord_9"].append({"kind": "subscription", "value": "https://other.example/x",
                                      "remark": "ручная", "enable": True})
    milan.clients.pop("discord_9")
    await bot.reconcile(force_check=True)
    check("https://other.example/x" in main_p.linked("discord_9"), "ручная внешняя ссылка стёрта")


async def test_orphans_found():
    main_p.clients["discord_999"] = {"email": "discord_999", "subId": "x", "uuid": "u", "expiryTime": 0}
    orphans = await bot.find_orphans()
    check(orphans.get("Хельсинки") == ["discord_999"], f"клиент без записи не найден: {orphans}")


async def test_primary_down_reconcile_skips():
    m = Member(11, V, guild=home)
    await bot.sync_member(m)
    m.set_roles()
    saved = bot.xui
    bot.xui = None
    try:
        stats = await bot.reconcile()
        check(stats.get("skipped"), "сверка без панели не пропущена")
    finally:
        bot.xui = saved
    check(db.get_subscription("11") is not None, "")


run([test_grant_creates_on_both_and_links, test_extra_down_main_still_deleted_and_retried,
     test_extra_not_connected_keeps_record, test_panel_error_is_not_absence,
     test_trial_not_started_survives_panel_error, test_trial_started_expires, test_trial_reminder_once,
     test_missing_paid_client_restored, test_extra_missing_is_recreated, test_manual_links_kept,
     test_orphans_found, test_primary_down_reconcile_skips], setup=setup)
