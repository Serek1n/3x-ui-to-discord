"""Фоновые задачи, переподключение панелей, логи, права файлов, копия базы,
проверка из РФ, скрипты."""
import asyncio
import io
import logging
import os
import sqlite3
import stat
from harness import load, connect, reset, check, run, V, G, T, HOME, REPO
from fakes import Guild, Member

bot, db, main_p, milan, admin = load(extras=1)
home = Guild(HOME, roles=[V, G, T])


async def setup():
    if bot.xui is None or any(p.api is None for p in bot.extra_panels):
        await connect(bot)
    reset(bot, db, main_p, milan)
    home._m.clear()
    bot.client.guilds = [home]
    admin.clear()


async def test_check_expired_survives_errors():
    """REL-04: исключение внутри check_expired не останавливает задачу."""
    calls = {"n": 0}
    real = db.list_expiring

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real()
    db.list_expiring = flaky
    try:
        bot.check_expired.change_interval(seconds=0.1)
        bot.check_expired.start()
        await asyncio.sleep(0.8)
        check(bot.check_expired.is_running() and calls["n"] >= 3,
              f"check_expired остановился (итераций {calls['n']})")
    finally:
        bot.check_expired.cancel()
        db.list_expiring = real
        await asyncio.sleep(0.05)


async def test_one_bad_subscription_does_not_block_others():
    a = Member(1, T, guild=home)
    b = Member(2, T, guild=home)
    await bot.sync_member(a, trial_just_added=True)
    await bot.sync_member(b, trial_just_added=True)
    import time
    past = int(time.time() * 1000) - 1000
    for p in (main_p, milan):
        p.clients["discord_2"]["expiryTime"] = past
    real = bot._expire_one

    async def boom(did, now_ms):
        if did == "1":
            raise RuntimeError("сломанная запись")
        return await real(did, now_ms)
    bot._expire_one = boom
    try:
        await bot.check_expired.coro()
    finally:
        bot._expire_one = real
    check(db.get_subscription("2") is None, "ошибка на одной подписке помешала обработать другую")


async def test_watchdog_reconnects_primary():
    """DSC-01: бот работает без панели и подключается, когда она появляется."""
    saved = bot.xui
    bot.xui = None
    bot.primary.api = None
    main_p.down = True
    await bot.watchdog.coro()
    check(bot.xui is None and any("недоступна" in t for t in admin), "нет оповещения о недоступной панели")
    main_p.down = False
    await bot.watchdog.coro()
    check(bot.xui is not None and bot.primary.api is bot.xui, "панель не переподключилась")
    check(any("снова доступна" in t for t in admin), "нет оповещения о восстановлении")
    await saved.close()


async def test_watchdog_reconnects_extra():
    saved = bot.extra_panels[0].api
    bot.extra_panels[0].api = None
    await bot.watchdog.coro()
    check(bot.extra_panels[0].api is not None, "доп. сервер не переподключился")
    await saved.close()


async def test_watchdog_restarts_stopped_loops():
    calls = {"n": 0}
    from discord.ext import tasks

    @tasks.loop(seconds=0.05)
    async def fragile():
        calls["n"] += 1
    bot.BACKGROUND_LOOPS["fragile"] = fragile
    try:
        await bot.watchdog.coro()
        await asyncio.sleep(0.2)
        check(fragile.is_running() and calls["n"] >= 1, "остановленная задача не перезапущена")
    finally:
        fragile.cancel()
        bot.BACKGROUND_LOOPS.pop("fragile", None)


async def test_api_errors_without_secrets():
    """SEC-04: адреса запросов (с секретным путём и subId) не попадают в лог."""
    buf = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(buf)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    bot.quiet_library_logs()
    try:
        m = Member(3, V, guild=home)
        await bot.sync_member(m)
        sub_id = db.get_subscription("3")["sub_id"]
        await bot.xui.get_external_links("discord_3", sub_id)
    finally:
        root.removeHandler(handler)
    text = buf.getvalue()
    check("/secretpath/" not in text, "секретный путь панели в логе")
    check(sub_id not in text, "subId в логе")
    check("tok1" not in text, "API-токен в логе")


async def test_files_restricted():
    open(db.DB_PATH, "a").close()
    os.chmod(db.DB_PATH, 0o644)
    bot._harden_files()
    mode = stat.S_IMODE(os.stat(db.DB_PATH).st_mode)
    check(mode == 0o600, f"права на базу {oct(mode)}")


async def test_db_backup_copy():
    m = Member(4, V, guild=home)
    await bot.sync_member(m)
    out = db.DB_PATH + ".copy"
    db.backup_to(out)
    conn = sqlite3.connect(out)
    n = conn.execute("SELECT count(*) FROM subscriptions").fetchone()[0]
    conn.close()
    check(n == 1, "копия базы неполная")


async def test_rf_check_needs_enough_nodes():
    """MON-02: «Недоступен» только если ответили хотя бы 3 узла."""
    import rf_check
    check(rf_check.RFResult(0, 1).status == "unknown", "1 ответивший узел - уже «Недоступен»")
    check(rf_check.RFResult(0, 3).status == "blocked", "3 узла без ответа - не «Недоступен»")
    check(rf_check.RFResult(2, 3).status == "partial", "")


async def test_rf_target_uses_vpn_port():
    """MON-01: без XUIn_CHECK_TARGET проверяется порт VPN, а не порт подписок."""
    milan.inbounds = [{"id": 1, "enable": True, "protocol": "vless", "port": 8443}]
    panel = bot.extra_panels[0]
    panel.api._inbounds_ts = 0
    target = await bot._check_target(panel)
    check(target == "m.example:8443", f"цель проверки {target}")


async def test_audit_lists_state():
    main_p.clients["discord_555"] = {"email": "discord_555", "subId": "z", "uuid": "u", "expiryTime": 0}
    sent = {}

    class Resp:
        async def defer(self, **kw):
            pass

    class Followup:
        async def send(self, text, **kw):
            sent["text"] = text

    class I:
        user = Member(9, "VPN Admin", guild=Guild(HOME, roles=["VPN Admin"]))
        guild = user.guild
        response = Resp()
        followup = Followup()
    bot.client.guilds = [I.guild]
    await bot.audit_cmd.callback(I(), delete_orphans=False)
    check("discord_555" in sent.get("text", ""), "клиент без записи не показан в /audit")
    await bot.audit_cmd.callback(I(), delete_orphans=True)
    check("discord_555" not in main_p.clients, "клиент без записи не удалён по /audit")


async def test_scripts():
    text = open(os.path.join(REPO, "backup.sh"), encoding="utf-8").read()
    check('"${NAME}"_*.tar.gz' in text, "ротация бэкапов ищет не те файлы")
    dep = open(os.path.join(REPO, "deploy.sh"), encoding="utf-8").read()
    check("if ! install_deps" in dep, "сбой установки зависимостей не обрабатывается")
    unit = open(os.path.join(REPO, "deploy", "bot.service.template"), encoding="utf-8").read()
    for opt in ("NoNewPrivileges=yes", "UMask=0077", "PrivateTmp=yes"):
        check(opt in unit, f"в unit нет {opt}")
    wf = open(os.path.join(REPO, ".github", "workflows", "deploy.yml"), encoding="utf-8").read()
    check("ssh-action@7eaf76671a0d7eec5d98ee897acda4f968735a17" in wf, "действие деплоя не закреплено по SHA")
    check("fingerprint:" in wf, "нет проверки отпечатка сервера")


async def test_browser_errors_have_no_cookies():
    """SEC-07: тексты ошибок браузерного режима без cookie, заголовков и HTML."""
    src = open(os.path.join(REPO, "xui_client.py"), encoding="utf-8").read()
    for bad in ("cookie страницы", "Заголовки ответа: {", "Начало HTML", "Фрагмент страницы", "Строка: {html"):
        check(bad not in src, f"в тексте ошибки осталось: {bad}")


run([test_check_expired_survives_errors, test_one_bad_subscription_does_not_block_others,
     test_watchdog_reconnects_primary, test_watchdog_reconnects_extra, test_watchdog_restarts_stopped_loops,
     test_api_errors_without_secrets, test_files_restricted, test_db_backup_copy, test_rf_check_needs_enough_nodes,
     test_rf_target_uses_vpn_port, test_audit_lists_state, test_scripts, test_browser_errors_have_no_cookies],
    setup=setup)
