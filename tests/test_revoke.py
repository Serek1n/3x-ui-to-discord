"""Отзыв доступа: отсрочка, лимит массовых удалений, подтверждение админом."""
import time
from harness import load, connect, reset, check, run, V, G, T, HOME
from fakes import Guild, Member

bot, db, main_p, milan, admin = load(extras=0, REVOKE_GRACE_MINUTES=15, REVOKE_LIMIT_PER_HOUR=0)
home = Guild(HOME, roles=[V, G, T])


async def setup():
    if bot.xui is None:
        await connect(bot, extras=False)
    reset(bot, db, main_p)
    home._m.clear()
    bot.client.guilds = [home]
    admin.clear()


def expire_grace():
    """Перематываем отсрочку: все запланированные удаления наступили."""
    for row in db.list_pending_revokes():
        db.set_revoke(row["discord_id"], time.time() - 1, row["revoke_reason"])


async def test_role_flap_keeps_link():
    """DEL-02b: снятие и быстрый возврат роли не меняет ссылку."""
    m = Member(1, V, guild=home)
    await bot.sync_member(m)
    sub_id = db.get_subscription("1")["sub_id"]
    before = m.copy()
    m.set_roles()
    await bot.on_member_update(before, m)
    check(db.get_subscription("1")["revoke_at"], "удаление не запланировано")
    check("discord_1" in main_p.clients, "клиент удалён сразу, без отсрочки")
    before = m.copy()
    m.set_roles(V)
    await bot.on_member_update(before, m)
    sub = db.get_subscription("1")
    check(sub["sub_id"] == sub_id and not sub["revoke_at"], "после возврата роли ссылка сменилась или отзыв не отменён")
    expire_grace()
    await bot.process_due_revokes()
    check("discord_1" in main_p.clients, "клиент удалён, хотя роль вернули")


async def test_grace_then_delete():
    m = Member(2, V, guild=home)
    await bot.sync_member(m)
    m.set_roles()
    await bot.sync_member(m)
    await bot.process_due_revokes()
    check("discord_2" in main_p.clients, "удалён до конца отсрочки")
    expire_grace()
    stats = await bot.process_due_revokes()
    check(stats.get("revoked") == 1 and "discord_2" not in main_p.clients, "после отсрочки клиент не удалён")
    check(db.get_subscription("2") is None, "запись осталась после удаления")
    check(any("Роль доступа" in (t or "") for t in m.dms), "человеку не сообщили об отключении")


async def test_mass_removal_is_held():
    """DEL-01/DEL-02: 5 из 7 потеряли роль - удаляется не больше лимита, остальные ждут админа."""
    members = [Member(i, V, guild=home) for i in range(100, 107)]
    for m in members:
        await bot.sync_member(m)
    for m in members[:5]:
        before = m.copy()
        m.set_roles()
        await bot.on_member_update(before, m)
    expire_grace()
    stats = await bot.reconcile()
    left = len(db.list_subscriptions())
    check(left >= 5, f"удалено слишком много подписок: осталось {left} из 7")
    check(stats.get("held", 0) >= 1 and bot._revoke_hold, "массовое удаление не приостановлено")
    check(any("/sync confirm:True" in t for t in admin), "админам не сообщили, как подтвердить")
    # Админ проверил и подтвердил
    bot._revoke_hold = False
    bot._revoke_approved_until = time.time() + 600
    await bot.reconcile()
    check(len(db.list_subscriptions()) == 2, f"после подтверждения осталось {len(db.list_subscriptions())}, ждали 2")


async def test_held_then_roles_restored():
    members = [Member(i, V, guild=home) for i in range(200, 210)]
    for m in members:
        await bot.sync_member(m)
    for m in members:
        m.set_roles()
    await bot.reconcile()
    expire_grace()
    await bot.reconcile()
    check(len(db.list_subscriptions()) >= 7, "при снятии ролей у всех удалено больше лимита")
    for m in members:
        m.set_roles(V)        # роли сняли по ошибке и вернули
    await bot.reconcile()
    check(len(db.list_subscriptions()) == 10, "часть подписок потеряна, хотя роли вернули")
    pending = db.list_pending_revokes()
    check(not pending, f"после возврата ролей остались запланированные удаления: {len(pending)}")
    check(not bot._revoke_hold, "пауза удалений не снялась, хотя ждать нечего")


async def test_member_left_is_deleted_after_grace():
    m = Member(3, V, guild=home)
    await bot.sync_member(m)
    home.remove(m)
    await bot.on_member_remove(m)
    check("discord_3" in main_p.clients, "ушедший удалён без отсрочки")
    expire_grace()
    await bot.process_due_revokes()
    check("discord_3" not in main_p.clients and db.get_subscription("3") is None, "ушедший не удалён после отсрочки")


async def test_rejoin_during_grace_keeps_access():
    m = Member(4, V, guild=home)
    await bot.sync_member(m)
    sub_id = db.get_subscription("4")["sub_id"]
    home.remove(m)
    await bot.on_member_remove(m)
    home.add(m)               # вернулся с ролью
    expire_grace()
    await bot.process_due_revokes()
    sub = db.get_subscription("4")
    check(sub is not None and sub["sub_id"] == sub_id and not sub["revoke_at"], "вернувшийся участник потерял доступ")


async def test_trial_role_removed_is_revoked():
    m = Member(5, T, guild=home)
    await bot.sync_member(m, trial_just_added=True)
    m.set_roles()
    await bot.sync_member(m)
    expire_grace()
    await bot.process_due_revokes()
    check(db.get_subscription("5") is None, "trial без роли не отозван")


async def test_sync_reports_scheduled():
    m = Member(6, V, guild=home)
    await bot.sync_member(m)
    m.set_roles()
    stats = await bot.reconcile()
    check(stats.get("scheduled") == 1, f"сверка не показала отложенное удаление: {dict(stats)}")
    check("отсрочки" in bot._format_stats(stats), "в отчёте сверки нет строки об отсрочке")


run([test_role_flap_keeps_link, test_grace_then_delete, test_mass_removal_is_held, test_held_then_roles_restored,
     test_member_left_is_deleted_after_grace, test_rejoin_during_grace_keeps_access,
     test_trial_role_removed_is_revoked, test_sync_reports_scheduled], setup=setup)
