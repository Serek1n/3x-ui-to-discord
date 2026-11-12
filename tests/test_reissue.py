"""Перевыпуск ссылки при сбоях."""
import time
from harness import load, connect, reset, check, run, V, G, T, HOME, DAY
from fakes import Guild, Member

bot, db, main_p, milan, admin = load(extras=1)
home = Guild(HOME, roles=[V, G, T])


async def setup():
    if bot.xui is None:
        await connect(bot)
    reset(bot, db, main_p, milan)
    home._m.clear()
    bot.client.guilds = [home]


async def test_reissue_changes_keys_everywhere():
    m = Member(1, V, guild=home)
    await bot.sync_member(m)
    old_main, old_milan = main_p.clients["discord_1"]["uuid"], milan.clients["discord_1"]["uuid"]
    link, failed = await bot.reissue_link("1")
    check(not failed, f"не заменены ключи на: {failed}")
    check(main_p.clients["discord_1"]["uuid"] != old_main and milan.clients["discord_1"]["uuid"] != old_milan,
          "ключи не заменились")
    check(link.endswith(db.get_subscription("1")["sub_id"]), "ссылка не совпадает с базой")


async def test_reissue_survives_one_add_failure():
    """REL-01: сбой панели между удалением и созданием - повтор создаёт клиента."""
    m = Member(2, V, guild=home)
    await bot.sync_member(m)
    main_p.fail_add = 1
    link, _ = await bot.reissue_link("2")
    check("discord_2" in main_p.clients, "после сбоя клиент не создан")
    check(db.get_subscription("2")["sub_id"] == main_p.clients["discord_2"]["subId"], "база и панель расходятся")


async def test_reissue_total_failure_is_restored():
    m = Member(3, V, guild=home)
    await bot.sync_member(m)
    main_p.fail_add = 5
    try:
        await bot.reissue_link("3")
        check(False, "ошибка не передана")
    except bot.XUIError:
        pass
    main_p.fail_add = 0
    check("discord_3" not in main_p.clients, "")
    await bot.reconcile()
    check("discord_3" in main_p.clients, "сверка не восстановила клиента после неудачного перевыпуска")


async def test_reissue_extra_down_replaced_later():
    """SEC-05: доп. сервер был недоступен при перевыпуске - старые ключи там
    заменяются, как только он снова работает."""
    m = Member(4, V, guild=home)
    await bot.sync_member(m)
    old = milan.clients["discord_4"]["uuid"]
    milan.down = True
    _, failed = await bot.reissue_link("4")
    milan.down = False
    check(failed == ["Милан"], f"не сообщено о сервере с неизменёнными ключами: {failed}")
    await bot.reconcile()
    check(milan.clients["discord_4"]["uuid"] != old, "старые ключи на доп. сервере продолжают работать")
    url = f"https://m.example:2096/s/{milan.clients['discord_4']['subId']}"
    check(url in main_p.linked("discord_4"), "новая подписка доп. сервера не прописана")


async def test_trial_reissue_keeps_remaining_time():
    """TRL-02: перевыпуск trial не даёт новый полный срок, при сбое - отказ."""
    m = Member(5, T, guild=home)
    await bot.sync_member(m, trial_just_added=True)
    ends = int(time.time() * 1000) + DAY
    main_p.clients["discord_5"]["expiryTime"] = ends
    milan.clients["discord_5"]["expiryTime"] = ends
    main_p.fail_traffic = 1
    milan.fail_traffic = 1
    try:
        await bot.reissue_link("5")
        check(False, "перевыпуск при сбое панели прошёл")
    except bot.XUIError:
        pass
    await bot.reissue_link("5")
    check(main_p.clients["discord_5"]["expiryTime"] == ends, "срок trial изменился при перевыпуске")


async def test_manual_links_survive_reissue():
    m = Member(6, V, guild=home)
    await bot.sync_member(m)
    main_p.links["discord_6"].append({"kind": "subscription", "value": "https://other.example/y",
                                      "remark": "ручная", "enable": True})
    await bot.reissue_link("6")
    check("https://other.example/y" in main_p.linked("discord_6"), "ручная ссылка потеряна при перевыпуске")


run([test_reissue_changes_keys_everywhere, test_reissue_survives_one_add_failure,
     test_reissue_total_failure_is_restored, test_reissue_extra_down_replaced_later,
     test_trial_reissue_keeps_remaining_time, test_manual_links_survive_reissue], setup=setup)
