"""Подписка от бота: одна постоянная ссылка, все серверы равноправны."""
import base64
import time
from urllib.parse import unquote
from harness import load, connect, reset, check, run, V, G, T, HOME, DAY
from fakes import Guild, Member

bot, db, main_p, milan, admin = load(extras=1, sub_on_panel=True, BOT_SUBSCRIPTION="true",
                                     NEXUS_LINK_BASE="https://bot.example:8444", XUI_NAME="🇫🇮 Финляндия",
                                     XUI2_NAME="🇳🇱 Нидерланды")
home = Guild(HOME, roles=[V, G, T])


async def setup():
    if bot.xui is None or bot.extra_panels[0].api is None:
        await connect(bot)
    reset(bot, db, main_p, milan)
    for p in bot.extra_panels:
        p.retired = False
    home._m.clear()
    bot.client.guilds = [home]
    admin.clear()


def token_of(did):
    return db.get_token(str(did), create=False)


async def configs(did):
    res = await bot.serve_subscription(token_of(did))
    check(isinstance(res, tuple), f"подписка не отдана: {res!r}")
    body, headers = res
    lines = base64.b64decode(body).decode().splitlines()
    return lines, headers


def names(lines):
    return sorted(unquote(line.rsplit("#", 1)[1]) for line in lines)


async def test_link_is_from_bot_and_has_all_servers():
    m = Member(1, V, guild=home)
    await bot.sync_member(m)
    tok = token_of(1)
    check(tok and m.dms and f"https://bot.example:8444/sub/{tok}" in m.dms[0], f"ссылка не от бота: {m.dms}")
    lines, headers = await configs(1)
    check(names(lines) == ["🇫🇮 Финляндия", "🇳🇱 Нидерланды"], f"конфиги: {names(lines)}")
    check(headers["subscription-userinfo"] == "upload=20; download=40; total=0; expire=0", headers)
    check(headers["profile-title"].startswith("base64:"), "нет названия подписки")


async def test_main_server_change_keeps_link_and_sends_nothing():
    """Основной сервер «сменили»: клиентов на нём нет - создаются тихо, ссылка та же."""
    m = Member(2, V, guild=home)
    await bot.sync_member(m)
    tok, dms = token_of(2), len(m.dms)
    main_p.clients.clear()                       # новая основная панель - пустая
    await bot.reconcile(force_check=True)
    check("discord_2" in main_p.clients, "клиент на новом основном сервере не создан")
    check(token_of(2) == tok and len(m.dms) == dms, "ссылка сменилась или человеку написали")
    check(db.get_subscription("2")["sub_id"] == main_p.clients["discord_2"]["subId"], "subId в базе не обновлён")
    check(not any("Основная панель не та" in t for t in admin), "ложная тревога о чужой панели")
    lines, _ = await configs(2)
    check(len(lines) == 2, "в подписке не все серверы")


async def test_new_server_added_to_everyone():
    m = Member(3, V, guild=home)
    await bot.sync_member(m)
    milan.clients.clear()                        # «новый» сервер без клиентов
    await bot.reconcile(force_check=True)
    check("discord_3" in milan.clients, "на новом сервере клиент не создан")
    bot.sub_aggregator._cache.clear()
    lines, _ = await configs(3)
    check("🇳🇱 Нидерланды" in names(lines), "новый сервер не попал в подписку")


async def test_server_down_keeps_its_configs():
    m = Member(4, V, guild=home)
    await bot.sync_member(m)
    await configs(4)
    bot.sub_aggregator._cache.clear()
    milan.down = True
    lines, _ = await configs(4)
    check(len(lines) == 2, "страна пропала из подписки из-за сбоя сервера")
    main_p.down = True
    bot.sub_aggregator._cache.clear()
    bot.sub_aggregator._last_good.clear()
    res = await bot.serve_subscription(token_of(4))
    check(res == "unavailable", f"при недоступности всех серверов: {res!r}")


async def test_reissue_new_token_old_dead():
    m = Member(5, V, guild=home)
    await bot.sync_member(m)
    old = token_of(5)
    link, failed = await bot.reissue_link("5")
    new = token_of(5)
    check(new != old and link.endswith(new) and not failed, "перевыпуск не сменил ссылку")
    check(await bot.serve_subscription(old) is None, "старая ссылка работает после перевыпуска")
    lines, _ = await configs(5)
    check(len(lines) == 2, "новая ссылка без серверов")


async def test_revoked_link_is_dead():
    m = Member(6, V, guild=home)
    await bot.sync_member(m)
    tok = token_of(6)
    m.set_roles()
    await bot.sync_member(m)
    check(await bot.serve_subscription(tok) is None, "ссылка работает после отзыва")
    m.set_roles(V)
    await bot.sync_member(m)
    check(token_of(6) == tok, "при возврате роли ссылка сменилась")


async def test_existing_subscribers_get_new_link_once():
    m = Member(7, V, guild=home)
    await bot.sync_member(m)
    db.mark_link_sent("7", False)                # как подписчик со старой ссылкой
    m.dms.clear()
    await bot.reconcile()
    await bot.reconcile()
    check(len(m.dms) == 1 and "Новая ссылка" in m.dms[0] and token_of(7) in m.dms[0],
          f"сообщений с новой ссылкой: {len(m.dms)}")


async def test_old_add_button_leads_to_bot_link():
    m = Member(8, V, guild=home)
    await bot.sync_member(m)
    sub = db.get_subscription("8")
    check(db.get_subscription_by_sub_id(sub["sub_id"])["discord_id"] == "8", "")
    check(bot.plain_user_url(sub) == f"https://bot.example:8444/sub/{token_of(8)}", bot.plain_user_url(sub))


async def test_retired_server():
    """Выводимый сервер: новым не создаётся и в подписку не попадает, но удаление идёт и на нём."""
    old = Member(9, V, guild=home)
    await bot.sync_member(old)
    bot.extra_panels[0].retired = True
    bot.sub_aggregator._cache.clear()
    new = Member(10, V, guild=home)
    await bot.sync_member(new)
    check("discord_10" not in milan.clients, "на выводимом сервере создан клиент")
    lines, _ = await configs(9)
    check(names(lines) == ["🇫🇮 Финляндия"], f"выводимый сервер в подписке: {names(lines)}")
    old.set_roles()
    await bot.sync_member(old)
    check("discord_9" not in milan.clients, "на выводимом сервере клиент не удалён")


async def test_trial_recreated_with_same_term():
    m = Member(11, T, guild=home)
    await bot.sync_member(m, trial_just_added=True)
    ends = int(time.time() * 1000) + 3 * DAY
    main_p.clients["discord_11"]["expiryTime"] = ends
    milan.clients.pop("discord_11")
    await bot.reconcile(force_check=True)
    check(milan.clients["discord_11"]["expiryTime"] == ends, "на досозданном сервере новый срок trial")


async def test_up_to_20_servers_configurable():
    import inspect
    src = inspect.getsource(bot)
    check("range(2, 21)" in src, "серверов меньше 20")


run([test_link_is_from_bot_and_has_all_servers, test_main_server_change_keeps_link_and_sends_nothing,
     test_new_server_added_to_everyone, test_server_down_keeps_its_configs, test_reissue_new_token_old_dead,
     test_revoked_link_is_dead, test_existing_subscribers_get_new_link_once, test_old_add_button_leads_to_bot_link,
     test_retired_server, test_trial_recreated_with_same_term, test_up_to_20_servers_configurable], setup=setup)
