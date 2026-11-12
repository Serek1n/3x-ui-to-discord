"""Доступ только по ролям своего сервера, права админа, упоминания."""
from harness import load, connect, reset, check, run, V, G, T, A, HOME
from fakes import Guild, Member, Interaction

bot, db, main_p, milan, admin = load(extras=0)
home = Guild(HOME, roles=[V, G, T, A])
evil = Guild(666, roles=[V, A])


async def setup():
    if bot.xui is None:
        await connect(bot, extras=False)
    reset(bot, db, main_p)
    home._m.clear()
    evil._m.clear()
    bot.client.guilds = [home, evil]


async def test_foreign_guild_role_gives_nothing():
    """SEC-01: роль «VPN Boosty» на постороннем сервере не выдаёт VPN."""
    before = Member(77, guild=evil)
    after = before.copy()
    after.set_roles(V)
    await bot.on_member_update(before, after)
    check(db.get_subscription("77") is None, "подписка выдана по роли чужого сервера")
    check("discord_77" not in main_p.clients, "клиент создан по роли чужого сервера")


async def test_foreign_admin_is_not_admin():
    check(not bot.is_admin(Interaction(Member(78, A, guild=evil), evil)), "админ чужого сервера прошёл is_admin")
    check(bot.is_admin(Interaction(Member(79, A, guild=home), home)), "админ своего сервера не прошёл is_admin")


async def test_reconcile_ignores_foreign_members():
    Member(80, V, guild=evil)
    await bot.reconcile()
    check(db.get_subscription("80") is None, "сверка выдала доступ участнику чужого сервера")


async def test_status_requires_role():
    """SEC-02: /status_server только с VPN-ролью или админом."""
    check(not bot.can_view_status(Interaction(Member(50, guild=home), home)), "статус доступен без ролей")
    check(bot.can_view_status(Interaction(Member(51, T, guild=home), home)), "статус недоступен с Trial")
    check(not bot.can_view_status(Interaction(Member(52, V, guild=evil), evil)), "статус доступен с чужого сервера")


async def test_find_member_only_home():
    Member(53, guild=evil)
    m = Member(53, V, guild=home)
    check(bot.find_member(53) is m, "find_member вернул участника не со своего сервера")


async def test_paid_role_grants_on_home():
    before = Member(60, guild=home)
    after = before.copy()
    after.set_roles(V)
    await bot.on_member_update(before, after)
    sub = db.get_subscription("60")
    check(sub is not None and sub["kind"] == "paid", "платная роль на своём сервере не выдала VPN")
    check("discord_60" in main_p.clients, "клиент не создан в панели")


async def test_no_guild_id_and_many_guilds_means_no_access():
    saved = bot.GUILD_ID
    bot.GUILD_ID = None
    try:
        check(bot.home_guild_id() is None, "без GUILD_ID и с двумя серверами свой сервер определён наугад")
        check(bot.access(Member(61, V, guild=home)) == set(), "роли признаются без определённого сервера")
        bot.client.guilds = [home]
        check(bot.home_guild_id() == HOME, "единственный сервер не принят за свой")
    finally:
        bot.GUILD_ID = saved


async def test_role_ids_take_priority():
    """Если задан ID роли, одноимённая роль с другим ID не считается."""
    real = home.role(V)
    saved = bot._ROLE_RULES
    bot._ROLE_RULES = tuple((k, {real.id} if k == "boosty" else ids, names) for k, ids, names in saved)
    try:
        fake = Member(62, guild=home)
        from fakes import Role
        fake.roles = [Role(V)]       # то же название, другой ID
        check("boosty" not in bot.access(fake), "роль с чужим ID признана платной")
        check("boosty" in bot.access(Member(63, V, guild=home)), "роль с правильным ID не признана")
    finally:
        bot._ROLE_RULES = saved


async def test_support_mentions_exclude_everyone():
    """SEC-06: в ветке поддержки нельзя упомянуть @everyone через тему обращения."""
    import inspect
    src = inspect.getsource(bot.support_cmd.callback)
    check("everyone=False" in src, "в /support упоминания @everyone не запрещены")
    import discord
    d = discord.AllowedMentions.none().to_dict()
    check(not d.get("parse"), "AllowedMentions.none() что-то разрешает")


run([test_foreign_guild_role_gives_nothing, test_foreign_admin_is_not_admin, test_reconcile_ignores_foreign_members,
     test_status_requires_role, test_find_member_only_home, test_paid_role_grants_on_home,
     test_no_guild_id_and_many_guilds_means_no_access, test_role_ids_take_priority,
     test_support_mentions_exclude_everyone], setup=setup)
