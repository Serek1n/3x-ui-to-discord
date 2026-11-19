"""Оповещения админам: карточки, склейка однотипных, без повторов каждую минуту."""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import admin_log  # noqa: E402
from admin_log import AdminLog  # noqa: E402
from harness import load, connect, reset, check, run, V, G, T, HOME  # noqa: E402
from fakes import Guild, Member  # noqa: E402

admin_log.FLUSH_DELAY = 0.05
sent = []


class Capture(AdminLog):
    async def _post(self, embeds):
        sent.append(embeds)


def new_log():
    sent.clear()
    return Capture("https://example.invalid/webhook")


async def wait():
    await asyncio.sleep(0.15)


async def test_card_has_level_title_and_mention():
    lg = new_log()
    lg.post("⚠️ Не удалось что-то: подробности для discord_111111111111111111")
    await wait()
    e = sent[0][0]
    check(e.color.value == admin_log.LEVELS["warn"][0], "цвет не по уровню")
    check(e.title == "Не удалось что-то", f"заголовок: {e.title!r}")
    check("<@111111111111111111>" in e.description, "ID не превращён в упоминание")
    check(e.timestamp is not None, "нет времени")


async def test_similar_messages_are_merged():
    lg = new_log()
    ids = [str(10**17 + i) for i in range(5)]
    for i in ids:
        lg.post("Причина: не подключены серверы: Милан", title="Сверка с панелью", level="warn", who=[i])
    lg.post("Совсем другое", title="Другое")
    await wait()
    check(len(sent) == 1, f"сообщений {len(sent)}, ждали одно")
    embeds = sent[0]
    check(len(embeds) == 2, f"карточек {len(embeds)}, ждали 2")
    check(embeds[0].title == "Сверка с панелью ×5", f"заголовок {embeds[0].title!r}")
    check(all(f"<@{i}>" in embeds[0].description for i in ids), "не все участники перечислены")
    check(embeds[0].description.count("не подключены серверы") == 1, "текст повторён")


async def test_auto_ids_merged():
    lg = new_log()
    for i in range(3):
        lg.post(f"⚠️ Сверка с панелью для discord_{10**17 + i}: не подключены серверы: Милан")
    await wait()
    e = sent[0][0]
    check(len(sent[0]) == 1 and "×3" in e.title, f"старые тексты не склеены: {[x.title for x in sent[0]]}")
    check("discord_" not in e.description, "в склеенной карточке остались email")


async def test_many_cards_split_by_ten():
    lg = new_log()
    for i in range(23):
        lg.post(f"Текст {i}", title=f"Заголовок {i}")
    await wait()
    check([len(b) for b in sent] == [10, 10, 3], f"разбиение {[len(b) for b in sent]}")


async def test_cooldown_by_key():
    lg = new_log()
    for _ in range(5):
        lg.post("🔴 Повтор", key="same", cooldown=3600)
    await wait()
    check(sum(len(b) for b in sent) == 1, "повтор по ключу не подавлен")


async def test_fields_and_long_text():
    lg = new_log()
    lg.post("x" * 9000, title="Длинно", fields=[("Отозвано", 3), ("Адреса", "1.1.1.1", False)])
    await wait()
    e = sent[0][0]
    check(len(e.description) <= admin_log.MAX_DESC and len(e.fields) == 2 and not e.fields[1].inline,
          "поля или обрезка не работают")


async def test_underscores_not_italic():
    lg = new_log()
    lg.post("в XUI_BASE_URL указана не та панель. Укажите блок XUI_… и `XUI2_SUB_BASE`", title="Т",
            fields=[("Причина", "проверьте XUI_WEB_BASE_PATH")])
    await wait()
    e = sent[0][0]
    check("XUI\\_BASE\\_URL" in e.description and "`XUI2_SUB_BASE`" in e.description, e.description)
    check("XUI\\_WEB\\_BASE\\_PATH" in e.fields[0].value, e.fields[0].value)


async def test_guild_id_is_not_mention():
    lg = new_log()
    lg.post("«evil» (`123456789012345678`)", title="Чужой сервер")
    await wait()
    check("<@" not in sent[0][0].description, "ID сервера превращён в упоминание")


# --- доставка в личку ---

class DMUser:
    def __init__(self, id, closed=False, name="admin"):
        self.id, self.closed, self.name, self.global_name = id, closed, name, None
        self.got = []

    async def send(self, embeds=None, allowed_mentions=None):
        if self.closed:
            import discord
            raise discord.Forbidden.__new__(discord.Forbidden)
        self.got.append(embeds)


class App:
    def __init__(self, owner):
        self.owner, self.team = owner, None


class DMClient:
    def __init__(self, users, owner=None):
        self.users = {u.id: u for u in users}
        self.owner = owner

    def get_user(self, i):
        return self.users.get(i)

    async def fetch_user(self, i):
        return self.users[i]

    async def application_info(self):
        return App(self.owner)


class DMLog(AdminLog):
    hooked = []

    async def _post_webhook(self, embeds):
        DMLog.hooked.append(embeds)


async def test_dm_to_owner_by_default():
    owner = DMUser(1)
    lg = DMLog("", mode="dm")
    lg.client = DMClient([owner], owner=owner)
    lg.post("🔴 Сбой: подробности")
    await wait()
    check(len(owner.got) == 1 and owner.got[0][0].title == "Сбой", "владельцу бота в личку не пришло")


async def test_dm_to_listed_admins():
    a, b, owner = DMUser(10), DMUser(11), DMUser(1)
    lg = DMLog("", mode="dm", dm_ids=[10, 11])
    lg.client = DMClient([a, b, owner], owner=owner)
    lg.post("Событие", title="Т")
    await wait()
    check(len(a.got) == 1 and len(b.got) == 1 and not owner.got, "не тем администраторам")


async def test_closed_dm_falls_back_to_channel():
    DMLog.hooked.clear()
    owner = DMUser(1, closed=True)
    lg = DMLog("https://example.invalid/hook", mode="dm")
    lg.client = DMClient([owner], owner=owner)
    lg.post("Событие", title="Т")
    await wait()
    check(len(DMLog.hooked) == 1, "при закрытой личке не ушло в запасной канал")


async def test_dm_mode_does_not_duplicate_to_channel():
    DMLog.hooked.clear()
    owner = DMUser(1)
    lg = DMLog("https://example.invalid/hook", mode="dm")
    lg.client = DMClient([owner], owner=owner)
    lg.post("Событие", title="Т")
    await wait()
    check(len(owner.got) == 1 and not DMLog.hooked, "в режиме dm продублировано в канал")


async def test_names_instead_of_mentions():
    owner = DMUser(1)
    member = DMUser(123456789012345678, name="kirill")
    lg = DMLog("", mode="dm")
    lg.client = DMClient([owner, member], owner=owner)
    lg.post("Текст", title="Т", who=["123456789012345678", "223456789012345678"])
    lg.post("Текст", title="Т", who=["223456789012345678"])
    await wait()
    d = owner.got[0][0].description
    check("**kirill**" in d and "<@223456789012345678>" in d, f"имена: {d}")


# --- поведение бота ---

bot, db, main_p, milan, admin = load(extras=1)
home = Guild(HOME, roles=[V, G, T])


async def test_offline_extra_not_reported_per_member():
    await connect(bot)
    reset(bot, db, main_p, milan)
    bot.client.guilds = [home]
    for i in range(1, 6):
        await bot.sync_member(Member(i, V, guild=home))
    saved = bot.extra_panels[0].api
    bot.extra_panels[0].api = None
    admin.clear()
    await bot.reconcile(force_check=True)
    check(not any("не подключены серверы" in t for t in admin), f"по каждому участнику: {admin}")
    bot.extra_panels[0].api = saved


async def test_watchdog_reports_state_changes_only():
    saved = bot.extra_panels[0].api
    bot.extra_panels[0].api = None
    milan.down = True
    admin.clear()
    for _ in range(5):
        await bot.watchdog.coro()
    down = [t for t in admin if "не подключён" in t]
    check(len(down) == 1, f"об отключении сообщено {len(down)} раз")
    check("503" in down[0] or "недоступ" in down[0], f"нет причины: {down[0]}")
    milan.down = False
    await bot.watchdog.coro()
    await bot.watchdog.coro()
    up = [t for t in admin if "снова работает" in t]
    check(len(up) == 1, f"о восстановлении сообщено {len(up)} раз")
    if saved is not None:
        await saved.close()


run([test_dm_to_owner_by_default, test_dm_to_listed_admins, test_closed_dm_falls_back_to_channel,
     test_dm_mode_does_not_duplicate_to_channel, test_names_instead_of_mentions,
     test_card_has_level_title_and_mention, test_similar_messages_are_merged, test_auto_ids_merged,
     test_many_cards_split_by_ten, test_cooldown_by_key, test_fields_and_long_text, test_underscores_not_italic, test_guild_id_is_not_mention,
     test_offline_extra_not_reported_per_member, test_watchdog_reports_state_changes_only])
