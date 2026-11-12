"""Подготовка окружения: временная база, заглушки панелей, импорт бота.
Каждый файл тестов запускается отдельным процессом (бот читает настройки
при импорте), см. run_all.py."""
import asyncio
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from fakes import FakePanel, FakeClient  # noqa: E402

V, G, T, A = "VPN Boosty", "VPN", "VPN Trial", "VPN Admin"
HOME = 1
DAY = 86_400_000


def load(api=True, extras=1, **env):
    """Импортирует бота с тестовыми настройками. Возвращает (bot, db, main, milan, admin_msgs)."""
    main_p, milan = FakePanel("tok1", "/secretpath"), FakePanel("tok2", "/m")
    tmp = tempfile.mkdtemp()
    e = dict(DISCORD_TOKEN="x", DB_PATH=os.path.join(tmp, "t.db"), TRIAL_DAYS="10", DEVICE_LIMIT="3",
             VPN_ROLE_NAME=V, VPN_GIFT_ROLES=G, TRIAL_ROLE_NAME=T, ADMIN_ROLE_NAME=A, GUILD_ID=str(HOME),
             XUI_BASE_URL=main_p.url, XUI_WEB_BASE_PATH="secretpath", XUI_SUB_BASE="https://h.example:2096/sub",
             XUI_NAME="Хельсинки", XUI_VERIFY_SSL="false", REVOKE_GRACE_MINUTES="0",
             REVOKE_LIMIT_PER_HOUR="1000", LOG_FILE=os.path.join(tmp, "bot.log"),
             NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    if api:
        e["XUI_API_TOKEN"] = "tok1"
    else:
        e.update(XUI_USERNAME="u", XUI_PASSWORD="p")
    if extras:
        e.update(XUI2_BASE_URL=milan.url, XUI2_API_TOKEN="tok2", XUI2_WEB_BASE_PATH="m",
                 XUI2_SUB_BASE="https://m.example:2096/s", XUI2_NAME="Милан")
    e.update({k: str(v) for k, v in env.items()})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ.update(e)
    sys.path.insert(0, REPO)
    os.chdir(tmp)
    import bot
    import db
    import xui_api
    xui_api.XUIApi.RETRIES = int(os.environ.get("TEST_XUI_RETRIES", "1"))  # без пауз на повторах
    admin = []

    async def fake_notify(text, key=None, cooldown=0):
        admin.append(text)
    bot.notify_admin = fake_notify
    bot.client = FakeClient()
    return bot, db, main_p, milan, admin


async def connect(bot, extras=True):
    from xui_api import XUIApi
    bot.xui = await XUIApi.create(bot.XUI_BASE_URL, bot.XUI_API_TOKEN, verify_ssl=False,
                                  web_base_path=bot.XUI_WEB_BASE_PATH, name="Хельсинки")
    bot.primary.api = bot.xui
    if extras:
        for ex in bot.extra_panels:
            ex.api = await XUIApi.create(ex.env["base_url"], ex.env["token"], verify_ssl=False,
                                         web_base_path=ex.env["web_base_path"], name=ex.name)


def check(cond, text):
    if not cond:
        raise AssertionError(text)


def reset(bot, db, *panels):
    """Чистое состояние между тестами одного файла."""
    for sub in db.list_subscriptions():
        db.delete_subscription(sub["discord_id"])
    for p in panels:
        p.clients.clear()
        p.links.clear()
        p.down = False
        p.fail_add = p.fail_traffic = 0
    bot._checked.clear()
    bot._revoke_log.clear()
    bot._revoke_hold = False
    bot._revoke_approved_until = 0.0


def run(tests, setup=None):
    """Запускает async-тесты по очереди в одном цикле событий.
    setup() вызывается перед каждым тестом. Код выхода 1, если хоть один упал."""
    async def main():
        failed = 0
        for t in tests:
            try:
                if setup is not None:
                    await setup()
                await t()
                print(f"OK    {t.__name__}")
            except Exception:
                failed += 1
                print(f"СБОЙ  {t.__name__}")
                traceback.print_exc()
        return failed
    sys.exit(1 if asyncio.run(main()) else 0)
