"""
Discord-бот для выдачи и управления VPN-подписками через 3x-ui.

Команды:
    /myvpn              - показать свою подписку, срок, трафик и QR-код
    /status_server      - [Админ/VPN/Trial] статус сервера и Xray (через Discord Webhook)
    /revoke @user       - [Админ] отозвать подписку пользователя (и снять VPN-роли)
    /sync               - [Админ] сверить роли участников с подписками прямо сейчас
    /reset_trial @user  - [Админ] разрешить пользователю пробный период ещё раз
    /vpn_stats          - [Админ] сколько сейчас подписок
    /support [тема]     - открыть обращение (приватная ветка с админами)
    /announce           - [Админ] рассылка подписчикам в ЛС
    /ips @user          - [Админ] IP-адреса подключений пользователя (режим API)

Доступ управляется ролями (Boosty сам выдаёт/снимает роль подписчикам):
    роль VPN_ROLE_NAME   -> бессрочная платная подписка, пока роль есть;
    роль TRIAL_ROLE_NAME -> пробный период на TRIAL_DAYS дней, один раз на человека;
    ни одной роли        -> клиент удаляется из 3x-ui.

Логика "какой доступ должен быть у участника" собрана в одной функции
sync_member(). Она вызывается при изменении ролей, при входе в /myvpn и
периодически для всех участников (reconcile_loop) - так бот догоняет
изменения ролей, пропущенные, пока он был выключен или перезапускался.

Фоновая задача check_expired раз в час удаляет истёкшие пробные подписки.
Перед удалением бот сверяется с фактическим сроком в панели: при включённом
"Start After First Use" срок начинается с первого подключения, а не с выдачи.

Подключение к 3x-ui: если задан XUI_API_TOKEN - через официальный API
(xui_api.py, быстро и без браузера), иначе через headless-браузер
(xui_client.py). Дополнительные серверы (XUI2_*, XUI3_*...) работают только
через API: бот сам создаёт на них клиента и прописывает его подписку как
внешнюю в подписке основного сервера - у пользователя одна ссылка на все
локации.
"""

import asyncio
import io
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

import psutil
import qrcode
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import tasks

load_dotenv()

import db  # noqa: E402  (после load_dotenv, чтобы учитывался DB_PATH из .env)
from xui_client import XUIClient, XUIError  # noqa: E402
from xui_api import XUIApi, recent_ips  # noqa: E402
import rf_check  # noqa: E402
from link_server import LinkServer  # noqa: E402

log = logging.getLogger("nexus")


def _env(name: str, default: str = "") -> str:
    """Значение из окружения без комментария в конце строки. systemd
    (EnvironmentFile) не отрезает "  # комментарий" после значения, и без
    этого, например, TEST_GUILD_ID превращается в "123...  # id сервера"."""
    value = os.environ.get(name, default)
    return re.sub(r"\s+#.*$", "", value).strip()


def _env_int(name: str, default: str) -> int:
    value = _env(name, default)
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"Ошибка в .env: {name}={value!r} - должно быть целое число")


def _env_bool(name: str, default: str) -> bool:
    return _env(name, default).lower() in ("1", "true", "yes", "on")


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"].strip()
XUI_BASE_URL = _env("XUI_BASE_URL", "http://127.0.0.1:2053")
# Токен API панели (Настройки → Безопасность → API Token). Задан - бот
# работает через API; пусто - через браузер по логину и паролю.
XUI_API_TOKEN = os.environ.get("XUI_API_TOKEN", "").strip()
XUI_USERNAME = _env("XUI_USERNAME")
XUI_PASSWORD = os.environ.get("XUI_PASSWORD", "").strip()
if not XUI_API_TOKEN and not (XUI_USERNAME and XUI_PASSWORD):
    raise SystemExit("Ошибка в .env: задайте XUI_API_TOKEN или XUI_USERNAME + XUI_PASSWORD")
# Какие inbound'ы получают клиента в режиме API: all или список id через запятую.
XUI_INBOUND_IDS = _env("XUI_INBOUND_IDS", "all")
XUI_NAME = _env("XUI_NAME", "Сервер 1")
XUI_INBOUND_ID = _env_int("XUI_INBOUND_ID", "1")
XUI_SUB_BASE = _env("XUI_SUB_BASE", "http://127.0.0.1:2096/sub").rstrip("/")
XUI_VERIFY_SSL = _env_bool("XUI_VERIFY_SSL", "true")
XUI_WEB_BASE_PATH = _env("XUI_WEB_BASE_PATH", "")
XUI_STATIC_CSRF_TOKEN = _env("XUI_STATIC_CSRF_TOKEN", "")
ADMIN_ROLE_NAME = _env("ADMIN_ROLE_NAME", "VPN Admin")
HAPP_PROVIDER_ID = _env("HAPP_PROVIDER_ID", "")
TRIAL_DAYS = _env_int("TRIAL_DAYS", "10")
DEVICE_LIMIT = _env_int("DEVICE_LIMIT", "3")
TEST_GUILD_ID = _env("TEST_GUILD_ID", "")

# Одна универсальная роль вместо тарифных: пока она есть - VPN работает.
VPN_ROLE_NAME = _env("VPN_ROLE_NAME", "VPN Access")
# Роль разового пробного доступа.
TRIAL_ROLE_NAME = _env("TRIAL_ROLE_NAME", "VPN Trial")

# URL вебхука канала, куда /status_server публикует карточку статуса.
STATUS_WEBHOOK_URL = _env("STATUS_WEBHOOK_URL", "")
# Необязательный вебхук закрытого канала для админов: туда бот пишет об
# ошибках выдачи/удаления, чтобы о проблеме с оплатившим клиентом узнали сразу.
ADMIN_LOG_WEBHOOK_URL = _env("ADMIN_LOG_WEBHOOK_URL", "")
# Как часто сверять роли всех участников с подписками (минуты, 0 = выключить).
SYNC_INTERVAL_MINUTES = _env_int("SYNC_INTERVAL_MINUTES", "30")
# Снимать роль VPN Trial после окончания пробного периода (нужно право
# "Управление ролями", и роль бота должна быть выше роли VPN Trial).
REMOVE_TRIAL_ROLE_ON_EXPIRY = _env_bool("REMOVE_TRIAL_ROLE_ON_EXPIRY", "true")
# Защита от спама /status_server (секунд на одного пользователя).
STATUS_COOLDOWN_SEC = _env_int("STATUS_COOLDOWN_SEC", "30")
# Ссылка на страницу Boosty - в напоминаниях о конце пробного периода.
BOOSTY_URL = _env("BOOSTY_URL", "")
# Канал, в котором /support создаёт приватные ветки обращений.
SUPPORT_CHANNEL_ID = _env("SUPPORT_CHANNEL_ID", "")
# Проверять доступность серверов из РФ в /status_server (через check-host.net).
RF_CHECK = _env_bool("RF_CHECK", "true")
# Контроль «шаринга»: оповещать админов, если с одной подписки за сутки
# подключались с большего числа разных IP, чем этот порог (только режим API).
SHARING_CHECK = _env_bool("SHARING_CHECK", "true")
SHARING_IP_THRESHOLD = _env_int("SHARING_IP_THRESHOLD", str(DEVICE_LIMIT + 2))
# Не чаще одного перевыпуска ссылки за это время (секунд) на человека.
REISSUE_COOLDOWN_SEC = _env_int("REISSUE_COOLDOWN_SEC", "600")
# Кнопка «Добавить в Nexus»: публичный https-адрес переходника (домен и порт),
# например https://vpn.example.com:8444. Пусто - кнопки нет.
NEXUS_LINK_BASE = _env("NEXUS_LINK_BASE", "").rstrip("/")
NEXUS_LINK_HOST = _env("NEXUS_LINK_HOST", "0.0.0.0")
NEXUS_LINK_PORT = _env_int("NEXUS_LINK_PORT", "8444")
# Сертификат и ключ (те же файлы, что у панели 3x-ui). Без них переходник
# слушает только 127.0.0.1 - для варианта за nginx с https.
NEXUS_LINK_CERT = _env("NEXUS_LINK_CERT", "")
NEXUS_LINK_KEY = _env("NEXUS_LINK_KEY", "")
# Где скачать Nexus - кнопка на странице переходника (необязательно).
NEXUS_DOWNLOAD_URL = _env("NEXUS_DOWNLOAD_URL", "")
# Лог с автоматической ротацией: bot.log до LOG_MAX_MB, LOG_BACKUPS старых копий.
LOG_FILE = _env("LOG_FILE", "bot.log")
LOG_MAX_MB = _env_int("LOG_MAX_MB", "5")
LOG_BACKUPS = _env_int("LOG_BACKUPS", "3")


def _plural_days(n: int) -> str:
    """Возвращает 'день'/'дня'/'дней' в правильной форме для числа n."""
    n_abs = abs(n) % 100
    last = n_abs % 10
    if 11 <= n_abs <= 14:
        return "дней"
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


db.init_db()

xui = None  # основной сервер: XUIApi (по токену) или XUIClient (браузер)


@dataclass
class Panel:
    """Сервер 3x-ui: название, адрес подписок и адрес для проверки из РФ."""
    name: str
    sub_base: str
    check_target: str = ""
    api: XUIApi | None = None
    env: dict = field(default_factory=dict)

    @property
    def client(self):
        return self.api


def _host_port(url: str) -> str:
    """host:port для проверки из РФ по адресу подписки (порт по схеме, если не указан)."""
    m = re.match(r"^(https?)://([^/:]+)(?::(\d+))?", url or "")
    if not m:
        return ""
    port = m.group(3) or ("443" if m.group(1) == "https" else "80")
    return f"{m.group(2)}:{port}"


primary = Panel(
    name=XUI_NAME, sub_base=XUI_SUB_BASE,
    check_target=_env("XUI_CHECK_TARGET", "") or _host_port(XUI_SUB_BASE),
)

# Дополнительные серверы: XUI2_*, XUI3_*, ... (только через API-токен).
extra_panels: list[Panel] = []
for _n in range(2, 6):
    _base = _env(f"XUI{_n}_BASE_URL")
    if not _base:
        continue
    _sub = _env(f"XUI{_n}_SUB_BASE").rstrip("/")
    extra_panels.append(Panel(
        name=_env(f"XUI{_n}_NAME", f"Сервер {_n}"),
        sub_base=_sub,
        check_target=_env(f"XUI{_n}_CHECK_TARGET") or _host_port(_sub),
        env={
            "base_url": _base,
            "token": os.environ.get(f"XUI{_n}_API_TOKEN", "").strip(),
            "web_base_path": _env(f"XUI{_n}_WEB_BASE_PATH"),
            "verify_ssl": _env_bool(f"XUI{_n}_VERIFY_SSL", "true"),
            "inbound_ids": _env(f"XUI{_n}_INBOUND_IDS", "all"),
            "sub_base_ok": bool(_sub),
        },
    ))


def api_mode() -> bool:
    return isinstance(xui, XUIApi)


def active_extras() -> list:
    """Подключённые дополнительные серверы (нужен API на основном)."""
    return [p for p in extra_panels if p.api is not None] if api_mode() else []


link_server: LinkServer | None = None


class VPNBot(discord.Client):
    async def setup_hook(self):
        await _setup()

    async def close(self):
        if link_server is not None:
            await link_server.stop()
        if xui is not None:
            await xui.close()
        await super().close()


intents = discord.Intents.default()
intents.members = True  # обязателен для on_member_update (реакция на роли от Boosty)
client = VPNBot(intents=intents)
tree = app_commands.CommandTree(client)


# ---------- вспомогательное ----------

def sub_link(sub_id: str) -> str:
    base = f"{XUI_SUB_BASE}/{sub_id}"
    if HAPP_PROVIDER_ID:
        # Happ: TCP-пинг (без отключения туннеля) + привязка к happ-proxy.com.
        base += f"#?providerid={HAPP_PROVIDER_ID}&ping-type=tcp"
    return base


def plain_sub_url(sub_id: str) -> str:
    """Ссылка подписки без параметров Happ - для Nexus."""
    return f"{XUI_SUB_BASE}/{sub_id}"


def _sub_id_from_link(link: str) -> str:
    return link.split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def nexus_button(sub_id: str) -> discord.ui.Button | None:
    """Кнопка-ссылка «Добавить в Nexus» (через https-переходник)."""
    if not NEXUS_LINK_BASE or not sub_id or link_server is None:
        return None
    return discord.ui.Button(style=discord.ButtonStyle.link, label="Добавить в Nexus", emoji="⚡",
                             url=f"{NEXUS_LINK_BASE}/add/{sub_id}")


def nexus_view(link: str | None) -> discord.ui.View | None:
    button = nexus_button(_sub_id_from_link(link)) if link else None
    if button is None:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(button)
    return view


def make_qr_file(link: str, filename: str = "subscription_qr.png") -> discord.File:
    """Генерирует QR-код ссылки подписки как discord.File."""
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(link)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)


def _role_names(member) -> set:
    return {r.name for r in getattr(member, "roles", [])}


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return ADMIN_ROLE_NAME in _role_names(interaction.user)


def can_view_status(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return bool(_role_names(interaction.user) & {ADMIN_ROLE_NAME, VPN_ROLE_NAME, TRIAL_ROLE_NAME})


def find_member(user_id: int):
    """Ищет участника на серверах бота (нужно, когда команда пришла из ЛС).
    Предпочитает тот сервер, где у человека есть VPN-роль."""
    fallback = None
    for g in client.guilds:
        m = g.get_member(user_id)
        if m is None:
            continue
        if _role_names(m) & {VPN_ROLE_NAME, TRIAL_ROLE_NAME}:
            return m
        fallback = fallback or m
    return fallback


async def send_dm(user, text: str, link: str | None = None):
    """Пишет пользователю в ЛС (с QR-кодом, если передана ссылка). Закрытые
    ЛС не считаются ошибкой."""
    try:
        if link:
            view = nexus_view(link)
            if view is not None:
                await user.send(text, file=make_qr_file(link), view=view)
            else:
                await user.send(text, file=make_qr_file(link))
        else:
            await user.send(text)
    except (discord.Forbidden, discord.HTTPException):
        log.info("Не удалось написать в ЛС пользователю %s", getattr(user, "id", user))


_last_alerts: dict = {}


async def notify_admin(text: str, key: str | None = None, cooldown: int = 6 * 3600):
    """Сообщение в админ-канал через ADMIN_LOG_WEBHOOK_URL. Одинаковые
    оповещения (по key) не чаще, чем раз в cooldown секунд."""
    log.warning("ADMIN: %s", text)
    if not ADMIN_LOG_WEBHOOK_URL:
        return
    if key:
        now = time.time()
        if now - _last_alerts.get(key, 0) < cooldown:
            return
        _last_alerts[key] = now
    try:
        webhook = discord.Webhook.from_url(ADMIN_LOG_WEBHOOK_URL, client=client)
        await webhook.send(text[:1900], username="NEXUS_VPN Logs")
    except Exception as e:
        log.error("Не удалось отправить оповещение админам: %s", e)


_member_locks: dict = {}


def _member_lock(discord_id: str) -> asyncio.Lock:
    """Одна операция над подпиской конкретного человека за раз (событие роли,
    сверка и /myvpn не должны одновременно создавать/удалять одного клиента)."""
    lock = _member_locks.get(discord_id)
    if lock is None:
        lock = _member_locks[discord_id] = asyncio.Lock()
    return lock


# ---------- выдача / удаление ----------

def _email(discord_id: str) -> str:
    return f"discord_{discord_id}"


async def _link_extras(email: str, sub_id: str, extra_subs: dict, preserve: list | None = None):
    """Прописывает подписки доп. серверов как внешние у основного клиента."""
    if not api_mode() or not (extra_subs or preserve):
        return
    await xui.set_external_subscriptions(
        email, sub_id, extra_subs, [p.sub_base for p in active_extras()], preserve=preserve,
    )


async def _create_on_extras(email: str, expiry_time: int, comment: str) -> dict:
    """Создаёт клиента на всех доп. серверах. Возвращает {название: url подписки}.
    Ошибка на одном сервере не мешает выдаче - сверка доделает позже."""
    subs = {}
    for panel in active_extras():
        try:
            created = await panel.api.add_client(
                0, email, 0, ip_limit=DEVICE_LIMIT, expiry_time=expiry_time, comment=comment,
            )
            subs[panel.name] = f"{panel.sub_base}/{created['subId']}"
        except XUIError as e:
            await notify_admin(f"⚠️ Не удалось создать клиента {email} на сервере «{panel.name}»: {e}",
                               key=f"extra:{panel.name}:{email}")
    return subs


async def _create_sub(discord_id: str, kind: str, *, member=None) -> str:
    """Создаёт клиента на всех серверах и запись в БД. Возвращает ссылку подписки."""
    email = _email(discord_id)
    comment = f"discord: {member}" if member is not None else ""
    if api_mode():
        expiry_time = -TRIAL_DAYS * 86_400_000 if kind == "trial" else 0
        created = await xui.add_client(0, email, 0, ip_limit=DEVICE_LIMIT,
                                       expiry_time=expiry_time, comment=comment)
        extra_subs = await _create_on_extras(email, expiry_time, comment)
        try:
            await _link_extras(email, created["subId"], extra_subs)
        except XUIError as e:
            await notify_admin(f"⚠️ Не удалось прописать доп. серверы в подписку {email}: {e}",
                               key=f"links:{email}")
    else:
        days = TRIAL_DAYS if kind == "trial" else 0
        created = await xui.add_client(XUI_INBOUND_ID, email, days, ip_limit=DEVICE_LIMIT)
    db.create_subscription(
        discord_id, email, created["uuid"], created["subId"],
        XUI_INBOUND_ID, created["expiryMs"], kind=kind,
    )
    _extras_checked.add(discord_id)
    log.info("Выдана подписка %s пользователю %s", kind, discord_id)
    return sub_link(created["subId"])


async def _delete_sub(sub: dict):
    """Удаляет клиента со всех серверов (сначала дополнительные: если там
    ошибка, запись в БД остаётся и удаление повторится при сверке)."""
    for panel in active_extras():
        await panel.api.delete_client(0, "", email=sub["email"])
    await xui.delete_client(sub["inbound_id"], sub["client_uuid"], email=sub["email"])
    db.delete_subscription(sub["discord_id"])
    _extras_checked.discard(sub["discord_id"])
    log.info("Удалена подписка %s пользователя %s", sub.get("kind"), sub["discord_id"])


async def _panel_expiry_state(sub: dict):
    """('started', ms) | ('not_started', None) | ('unknown', None)"""
    panels = [xui] + [p.api for p in active_extras()]
    seen_negative = False
    started = []
    for client_api in panels:
        traffic = await client_api.get_client_traffic(sub["email"])
        exp = traffic.get("expiryTime") if traffic else None
        if isinstance(exp, (int, float)):
            if exp > 0:
                started.append(int(exp))
            elif exp < 0:
                seen_negative = True
    if started:
        return "started", min(started)
    if seen_negative:
        return "not_started", None
    return "unknown", None


_extras_checked: set = set()


async def ensure_extras(sub: dict):
    """Догоняет доп. серверы для уже существующей подписки: создаёт клиента,
    если его там нет, и прописывает внешнюю подписку. Раз за запуск бота."""
    did = sub["discord_id"]
    if did in _extras_checked or not active_extras():
        return
    email = sub["email"]
    expiry_time = 0
    if sub["kind"] == "trial":
        state, ms = await _panel_expiry_state(sub)
        expiry_time = ms if state == "started" else -TRIAL_DAYS * 86_400_000
    wanted = {}
    for panel in active_extras():
        found = await panel.api.find_client(email)
        if not found:
            created = await panel.api.add_client(0, email, 0, ip_limit=DEVICE_LIMIT,
                                                 expiry_time=expiry_time)
            found = {"subId": created["subId"]}
            log.info("Клиент %s создан на сервере «%s» при сверке", email, panel.name)
        wanted[panel.name] = f"{panel.sub_base}/{found.get('subId')}"
    if not await xui.has_external_links(email, sub["sub_id"], list(wanted.values())):
        await _link_extras(email, sub["sub_id"], wanted)
    _extras_checked.add(did)


async def reissue_link(discord_id: str) -> str:
    """Новая ссылка и новые ключи: старые конфиги перестают работать на всех
    серверах. Тип подписки и уже идущий срок сохраняются."""
    sub = db.get_subscription(discord_id)
    if not sub:
        raise XUIError("подписки нет")
    if not api_mode():
        raise XUIError("перевыпуск доступен только при работе бота через API-токен")
    email = sub["email"]
    expiry_time = 0
    if sub["kind"] == "trial":
        state, ms = await _panel_expiry_state(sub)
        if state == "started":
            if ms <= int(time.time() * 1000):
                raise XUIError("пробный период уже закончился")
            expiry_time = ms
        else:
            expiry_time = -TRIAL_DAYS * 86_400_000
    # Внешние ссылки, добавленные админом вручную, удаляются вместе со старым
    # клиентом - запоминаем их и возвращаем новому.
    old_links = await xui.get_external_links(email, sub["sub_id"])
    # add_client сам удаляет старого клиента с тем же email и создаёт нового.
    created = await xui.add_client(0, email, 0, ip_limit=DEVICE_LIMIT, expiry_time=expiry_time)
    extra_subs = await _create_on_extras(email, expiry_time, "")
    await _link_extras(email, created["subId"], extra_subs, preserve=old_links)
    db.update_client_ids(discord_id, created["uuid"], created["subId"])
    _extras_checked.add(discord_id)
    log.info("Перевыпущена ссылка пользователя %s", discord_id)
    return sub_link(created["subId"])


async def _ensure_extras_quiet(sub: dict):
    try:
        await ensure_extras(sub)
    except XUIError as e:
        await notify_admin(f"⚠️ Доп. серверы для {sub['email']}: {e}", key=f"ensure:{sub['discord_id']}")


async def sync_member(member, *, trial_just_added: bool = False, quiet: bool = False) -> list:
    """Приводит подписку участника в соответствие с его ролями.

    trial_just_added - роль Trial только что выдали (для пояснений в ЛС);
    quiet            - не слать пояснительные сообщения без изменений
                       (используется периодической сверкой).
    Возвращает список выполненных действий: 'granted', 'upgraded', 'trial', 'revoked'.
    Бросает XUIError, если панель не дала выполнить операцию.
    """
    discord_id = str(member.id)
    actions = []
    async with _member_lock(discord_id):
        names = _role_names(member)
        has_vpn = VPN_ROLE_NAME in names
        has_trial = TRIAL_ROLE_NAME in names
        sub = db.get_subscription(discord_id)

        # 1. Платная роль - главный приоритет.
        if has_vpn:
            if sub and sub["kind"] == "paid":
                if trial_just_added and not quiet:
                    await send_dm(member, "У вас уже есть платная подписка, пробный период не нужен. "
                                          "Ссылка - в `/myvpn`.")
                await _ensure_extras_quiet(sub)
                return actions
            upgraded = sub is not None
            if sub:
                # Был trial: панель не даёт редактировать клиента запросом,
                # поэтому пересоздаём его уже бессрочным.
                await _delete_sub(sub)
            link = await _create_sub(discord_id, "paid", member=member)
            actions.append("upgraded" if upgraded else "granted")
            text = (
                ("Оплата на Boosty получена, спасибо! ✅ Пробный доступ заменён на постоянный.\n"
                 "Старая ссылка больше не работает - добавьте новую:\n" if upgraded else
                 "Оплата на Boosty получена, спасибо! ✅\n")
                + f"VPN активен, пока у вас есть роль «{VPN_ROLE_NAME}». "
                  f"Можно подключить до {DEVICE_LIMIT} устройств.\nСсылка: {link}"
            )
            await send_dm(member, text, link)
            return actions

        # 2. Платной роли нет, а платная подписка осталась - отзываем.
        if sub and sub["kind"] == "paid":
            await _delete_sub(sub)
            sub = None
            actions.append("revoked")
            await send_dm(member, f"Роль «{VPN_ROLE_NAME}» снята, доступ к VPN отозван.\n"
                                  f"Если это ошибка или вы продолжаете оплачивать подписку - "
                                  f"напишите администратору.")

        # 3. Пробный период.
        if has_trial:
            if sub:  # trial уже идёт
                await _ensure_extras_quiet(sub)
                return actions
            if db.has_used_trial(discord_id):
                if trial_just_added and not quiet:
                    await send_dm(member, "Пробный период уже был использован и доступен только один раз. "
                                          "Оформите подписку на Boosty, чтобы получить постоянный доступ.")
                return actions
            link = await _create_sub(discord_id, "trial", member=member)
            # Помечаем использованным только после успешной выдачи.
            db.mark_trial_used(discord_id)
            actions.append("trial")
            await send_dm(
                member,
                f"Пробный доступ на {TRIAL_DAYS} {_plural_days(TRIAL_DAYS)} активирован!\n"
                f"Срок пойдёт с первого подключения. Устройств: до {DEVICE_LIMIT}.\n"
                f"Ссылка: {link}\n"
                f"Повторно бесплатно получить его будет нельзя - после окончания "
                f"оформите подписку на Boosty.",
                link,
            )
            return actions

        # 4. Ролей нет, а trial-подписка осталась (роль Trial сняли вручную).
        if sub:
            await _delete_sub(sub)
            actions.append("revoked")
            await send_dm(member, "Роль пробного периода снята, доступ к VPN отозван.")
        return actions


async def _safe_sync(member, **kwargs):
    """sync_member с обработкой ошибок: человеку - понятное сообщение,
    админам - подробности."""
    try:
        return await sync_member(member, **kwargs)
    except XUIError as e:
        await notify_admin(
            f"⚠️ Не удалось синхронизировать VPN для {member} ({member.id}): {e}",
            key=f"sync:{member.id}",
        )
        if not kwargs.get("quiet"):
            await send_dm(member, "Не получилось обновить ваш VPN-доступ из-за ошибки панели. "
                                  "Бот повторит попытку автоматически, администратор уже уведомлён.")
    except Exception as e:
        log.exception("Ошибка sync_member для %s", member.id)
        await notify_admin(f"⚠️ Ошибка при обработке {member} ({member.id}): {e!r}",
                           key=f"sync:{member.id}")
    return []


# ---------- команды ----------

HAPP_SITE = "https://www.happ.su/main/ru"
HAPP_APPSTORE = "https://apps.apple.com/app/id6504287215"

_GUIDE_STEPS = (
    "2. Откройте `/myvpn` и нажмите на ссылку подписки, чтобы скопировать её.\n"
    "3. В Happ нажмите **«+»** → **«Вставить из буфера»** (или отсканируйте QR-код).\n"
    "4. Выберите сервер и нажмите кнопку подключения, разрешите создание VPN.\n"
)

GUIDES = {
    "android": ("Android", f"1. Установите **Happ** из Google Play или с сайта: {HAPP_SITE}\n" + _GUIDE_STEPS),
    "ios": ("iPhone / iPad", f"1. Установите **Happ** из App Store: {HAPP_APPSTORE}\n" + _GUIDE_STEPS),
    "windows": ("Windows", f"1. Скачайте **Happ** для Windows с сайта: {HAPP_SITE} и установите.\n" + _GUIDE_STEPS),
    "macos": ("macOS", f"1. Установите **Happ** из App Store: {HAPP_APPSTORE}\n" + _GUIDE_STEPS),
}


def guide_text(key: str) -> str:
    title, body = GUIDES[key]
    return (f"**Подключение на {title}**\n{body}"
            f"\nМожно подключить до {DEVICE_LIMIT} устройств. "
            f"Не получается? Напишите в `/support`.")


class GuideSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="📖 Инструкция по подключению",
            options=[discord.SelectOption(label=title, value=key) for key, (title, _) in GUIDES.items()],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(guide_text(self.values[0]), ephemeral=True,
                                                suppress_embeds=True)


_reissue_times: dict = {}


class ConfirmReissueView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id

    @discord.ui.button(label="Да, перевыпустить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Это не ваша подписка.", ephemeral=True)
            return
        now = time.time()
        last = _reissue_times.get(self.owner_id, 0)
        if now - last < REISSUE_COOLDOWN_SEC:
            wait = int((REISSUE_COOLDOWN_SEC - (now - last)) // 60) + 1
            await interaction.response.edit_message(
                content=f"Ссылку недавно перевыпускали. Повторить можно через {wait} мин.", view=None)
            return
        await interaction.response.edit_message(content="Перевыпускаю ссылку…", view=None)
        discord_id = str(self.owner_id)
        try:
            async with _member_lock(discord_id):
                link = await reissue_link(discord_id)
        except XUIError as e:
            await interaction.followup.send(f"Не получилось перевыпустить ссылку: {e}", ephemeral=True)
            return
        _reissue_times[self.owner_id] = now
        text = ("Ссылка перевыпущена ✅ Старая и все добавленные по ней конфиги больше не работают.\n"
                f"Добавьте новую подписку в приложение заново:\n{link}")
        view = nexus_view(link)
        if view is not None:
            await interaction.followup.send(text, file=make_qr_file(link), view=view, ephemeral=True)
        else:
            await interaction.followup.send(text, file=make_qr_file(link), ephemeral=True)
        await send_dm(interaction.user, text, link)
        await notify_admin(f"🔁 {interaction.user} ({discord_id}) перевыпустил ссылку подписки")

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Перевыпуск отменён.", view=None)


class MyVPNView(discord.ui.View):
    def __init__(self, owner_id: int, can_reissue: bool, sub_id: str = ""):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.add_item(GuideSelect())
        button = nexus_button(sub_id)
        if button is not None:
            button.row = 1
            self.add_item(button)
        if not can_reissue:
            self.reissue.disabled = True

    @discord.ui.button(label="Перевыпустить ссылку", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def reissue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Перевыпустить ссылку? Старая ссылка и все устройства, подключённые по ней, "
            "перестанут работать - подписку нужно будет добавить в приложение заново. "
            "Делайте это, если ссылка попала к посторонним.",
            view=ConfirmReissueView(self.owner_id), ephemeral=True,
        )


async def _traffic_total(email: str):
    """Суммарный трафик по всем серверам: (up, down) в байтах или None."""
    up = down = 0
    found = False
    for client_api in [xui] + [p.api for p in active_extras()]:
        t = await client_api.get_client_traffic(email)
        if t:
            found = True
            up += t.get("up") or 0
            down += t.get("down") or 0
    return (up, down) if found else None


@tree.command(name="myvpn", description="Показать вашу подписку, ссылку, QR-код и инструкцию")
async def myvpn(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    sub = db.get_subscription(discord_id)
    if not sub:
        # Самовосстановление: роль есть, а подписки нет (например, бот был
        # выключен в момент выдачи роли) - выдаём прямо сейчас.
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        member = member or find_member(interaction.user.id)
        if member and _role_names(member) & {VPN_ROLE_NAME, TRIAL_ROLE_NAME}:
            await _safe_sync(member, quiet=True)
            sub = db.get_subscription(discord_id)

    if not sub:
        text = "У вас пока нет подписки. Оформите доступ через Boosty."
        if BOOSTY_URL:
            text += f"\n{BOOSTY_URL}"
        await interaction.followup.send(text, ephemeral=True, suppress_embeds=True)
        return

    kind = "Пробная" if sub["kind"] == "trial" else "Платная"
    expiry = "бессрочно (пока есть роль)"
    if sub["kind"] == "trial":
        state, ms = await _panel_expiry_state(sub)
        if state == "started":
            expiry = f"<t:{ms // 1000}:F> (<t:{ms // 1000}:R>)"
        elif state == "not_started":
            expiry = f"начнётся с первого подключения ({TRIAL_DAYS} {_plural_days(TRIAL_DAYS)})"
        else:
            expiry = f"<t:{sub['expiry_ms'] // 1000}:F>"

    lines = [f"Подписка: {kind}", f"Срок действия: {expiry}", f"Устройств: до {DEVICE_LIMIT}"]
    servers = [primary.name] + [p.name for p in active_extras()]
    if len(servers) > 1:
        lines.append("Серверы: " + ", ".join(servers))
    traffic = await _traffic_total(sub["email"])
    if traffic:
        lines.append(f"Использовано: ↑{traffic[0] / 1024 ** 3:.2f} ГБ / ↓{traffic[1] / 1024 ** 3:.2f} ГБ")

    link = sub_link(sub["sub_id"])
    lines.append(f"Ссылка: {link}")
    await interaction.followup.send(
        "\n".join(lines),
        file=make_qr_file(link),
        view=MyVPNView(interaction.user.id, can_reissue=api_mode(), sub_id=sub["sub_id"]),
        ephemeral=True,
    )


def _fmt_speed(bytes_per_sec: float) -> str:
    mbps = bytes_per_sec * 8 / 1_000_000
    return f"{mbps:.2f} Мбит/с"


def _find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_gradient_bar(img: Image.Image, x: int, y: int, w: int, h: int,
                        percent: float, color_start: tuple, color_end: tuple):
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(42, 44, 51))

    fill_w = int(w * max(0, min(percent, 100)) / 100)
    if fill_w <= 0:
        return
    fill_w = max(fill_w, h)
    fill_w = min(fill_w, w)

    grad = Image.new("RGB", (fill_w, h))
    grad_draw = ImageDraw.Draw(grad)
    for i in range(fill_w):
        t = i / max(fill_w - 1, 1)
        r = int(color_start[0] + (color_end[0] - color_start[0]) * t)
        g = int(color_start[1] + (color_end[1] - color_start[1]) * t)
        b = int(color_start[2] + (color_end[2] - color_start[2]) * t)
        grad_draw.line([(i, 0), (i, h)], fill=(r, g, b))

    mask = Image.new("L", (fill_w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, fill_w, h], radius=h // 2, fill=255)
    img.paste(grad, (x, y), mask)


def _draw_icon_box(draw: ImageDraw.ImageDraw, x: int, y: int, size: int,
                    kind: str, icon_color: tuple, box_fill: tuple):
    draw.rounded_rectangle([x, y, x + size, y + size], radius=size // 4, fill=box_fill)
    cx, cy = x + size / 2, y + size / 2
    s = size * 0.28  # половина размера самой иконки внутри квадрата

    if kind == "cpu":
        draw.rounded_rectangle([cx - s, cy - s, cx + s, cy + s], radius=3, outline=icon_color, width=2)
        draw.rectangle([cx - s * 0.4, cy - s * 0.4, cx + s * 0.4, cy + s * 0.4], outline=icon_color, width=2)
        for dx in (-s, s):
            draw.line([(cx + dx, cy - s * 0.4), (cx + dx * 1.3, cy - s * 0.4)], fill=icon_color, width=2)
            draw.line([(cx + dx, cy + s * 0.4), (cx + dx * 1.3, cy + s * 0.4)], fill=icon_color, width=2)
        for dy in (-s, s):
            draw.line([(cx - s * 0.4, cy + dy), (cx - s * 0.4, cy + dy * 1.3)], fill=icon_color, width=2)
            draw.line([(cx + s * 0.4, cy + dy), (cx + s * 0.4, cy + dy * 1.3)], fill=icon_color, width=2)

    elif kind == "memory":
        draw.rounded_rectangle([cx - s * 1.1, cy - s * 0.7, cx + s * 1.1, cy + s * 0.7], radius=3,
                                outline=icon_color, width=2)
        for i in range(-1, 2):
            lx = cx + i * s * 0.6
            draw.line([(lx, cy - s * 0.7), (lx, cy + s * 0.7)], fill=icon_color, width=2)

    elif kind == "traffic":
        ax = cx - s * 0.5
        draw.line([(ax, cy + s), (ax, cy - s)], fill=icon_color, width=2)
        draw.polygon([(ax, cy - s * 1.3), (ax - s * 0.5, cy - s * 0.5), (ax + s * 0.5, cy - s * 0.5)],
                     fill=icon_color)
        bx = cx + s * 0.5
        draw.line([(bx, cy - s), (bx, cy + s)], fill=icon_color, width=2)
        draw.polygon([(bx, cy + s * 1.3), (bx - s * 0.5, cy + s * 0.5), (bx + s * 0.5, cy + s * 0.5)],
                     fill=icon_color)

    elif kind == "pulse":
        pts = [
            (cx - s * 1.3, cy), (cx - s * 0.6, cy),
            (cx - s * 0.3, cy - s * 1.1), (cx, cy + s * 1.1),
            (cx + s * 0.3, cy), (cx + s * 1.3, cy),
        ]
        draw.line(pts, fill=icon_color, width=2, joint="curve")


GREEN, YELLOW, RED, GRAY = (87, 242, 135), (250, 204, 21), (237, 66, 69), (130, 133, 145)
MSK = timezone(timedelta(hours=3))


def _paste_round_avatar(img: Image.Image, data: bytes, cx: int, cy: int, r: int) -> bool:
    """Вклеивает картинку в круг радиуса r со сглаженным краем. False - не вышло."""
    try:
        av = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return False
    # Квадрат по центру с небольшим приближением (у логотипа широкие поля).
    side = int(min(av.size) * 0.86)
    left, top = (av.width - side) // 2, (av.height - side) // 2
    av = av.crop((left, top, left + side, top + side)).resize((2 * r, 2 * r), Image.LANCZOS)
    scale = 4  # маска в 4 раза больше и уменьшается - край круга без «лесенки»
    mask = Image.new("L", (2 * r * scale, 2 * r * scale), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * r * scale - 1, 2 * r * scale - 1], fill=255)
    mask = mask.resize((2 * r, 2 * r), Image.LANCZOS)
    img.paste(av, (cx - r, cy - r), mask)
    return True


def _mix(color: tuple, bg: tuple, k: float) -> tuple:
    return tuple(int(bg[i] + (color[i] - bg[i]) * k) for i in range(3))


def _draw_status_icon(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, kind: str, color: tuple):
    """Круглый значок: ok - галочка, bad - крестик, warn - «!», unknown - «?»."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    ink = (14, 15, 20)
    w = max(3, int(r / 3.2))
    if kind == "ok":
        draw.line([(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.1, cy + r * 0.38), (cx + r * 0.5, cy - r * 0.35)],
                  fill=ink, width=w, joint="curve")
    elif kind == "bad":
        k = r * 0.38
        draw.line([(cx - k, cy - k), (cx + k, cy + k)], fill=ink, width=w)
        draw.line([(cx - k, cy + k), (cx + k, cy - k)], fill=ink, width=w)
    elif kind == "warn":
        draw.line([(cx, cy - r * 0.5), (cx, cy + r * 0.12)], fill=ink, width=w)
        draw.ellipse([cx - w / 2, cy + r * 0.32, cx + w / 2, cy + r * 0.32 + w], fill=ink)
    else:
        f = _find_font(int(r * 1.3), bold=True)
        tw = draw.textlength("?", font=f)
        draw.text((cx - tw / 2, cy - r * 0.78), "?", font=f, fill=ink)


def _draw_pill(draw, x: int, cy: int, text: str, kind: str, color: tuple, font, bg: tuple) -> int:
    """Плашка «значок + слово». Возвращает ширину."""
    h = 46
    tw = draw.textlength(text, font=font)
    w = int(tw + 70)
    draw.rounded_rectangle([x, cy - h // 2, x + w, cy + h // 2], radius=h // 2,
                           fill=_mix(color, bg, 0.16), outline=_mix(color, bg, 0.55), width=2)
    _draw_status_icon(draw, x + 24, cy, 13, kind, color)
    draw.text((x + 46, cy - 14), text, font=font, fill=color)
    return w


def _load_level(cpu: float | None, mem_pct: float | None):
    """Загрузка простыми словами по самому загруженному из процессора и памяти."""
    vals = [v for v in (cpu, mem_pct) if isinstance(v, (int, float))]
    if not vals:
        return None, "Не знаю", GRAY
    v = max(vals)
    if v < 60:
        return v, "Низкая", GREEN
    if v < 85:
        return v, "Средняя", YELLOW
    return v, "Высокая", RED


def card_summary(servers: list):
    """Итог для шапки карточки: (вид, заголовок, подсказка)."""
    broken = [sv["name"] for sv in servers if sv.get("xray") is False]
    blocked = [sv["name"] for sv in servers
               if sv.get("rf") is not None and sv["rf"].status in ("blocked", "partial")]
    fine = [sv["name"] for sv in servers
            if sv.get("xray") and (sv.get("rf") is None or sv["rf"].status == "ok")]
    q = lambda names: ", ".join(f"«{n}»" for n in names)
    if broken and not fine:
        return "bad", "VPN не работает", "Мы уже знаем о проблеме и чиним. Попробуйте чуть позже."
    if broken or blocked:
        parts = []
        if broken:
            parts.append(f"{q(broken)} сломался, мы уже чиним.")
        only_blocked = [n for n in blocked if n not in broken]
        if only_blocked:
            parts.append(f"{q(only_blocked)} сейчас не открывается из России.")
        if fine:
            parts.append(f"Подключайтесь к {q(fine)}: там всё хорошо.")
        return "warn", "Работает, но не везде", " ".join(parts)
    if any(sv.get("xray") is None for sv in servers):
        return "warn", "Почти всё работает", "Про некоторые серверы пока нет данных."
    return "ok", "Всё работает", "Можно спокойно подключаться к любому серверу."


def _render_status_card(servers: list, requested_by: str, avatar: bytes | None = None,
                         checked_at: datetime | None = None) -> bytes:
    """PNG-карточка статуса простыми словами (Pillow, вызывается через to_thread).
    servers: [{"name", "xray": True|False|None, "rf": RFResult|None,
               "cpu": %, "mem": %, "down": байт/с, "up": байт/с}]"""
    servers = servers or [{"name": "Сервер", "xray": None, "rf": None}]
    W = 1300
    row_h, row_gap = 84, 14
    rows_y = 302
    H = rows_y + len(servers) * (row_h + row_gap) + 70
    bg = (15, 16, 20)
    panel = (24, 25, 32)
    text_dim = (150, 153, 163)
    white = (255, 255, 255)
    blue = (122, 162, 255)

    img = Image.new("RGB", (W, H), (13, 14, 18))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([1, 1, W - 2, H - 2], radius=28, outline=(40, 42, 50), width=2, fill=bg)

    title_font = _find_font(40, bold=True)
    sub_font = _find_font(21)
    head_font = _find_font(19, bold=True)
    name_font = _find_font(28, bold=True)
    pill_font = _find_font(22, bold=True)
    big_font = _find_font(30, bold=True)
    hint_font = _find_font(23)
    small_font = _find_font(17)

    # Аватарка и название
    acx, acy, ar = 118, 112, 62
    if avatar and _paste_round_avatar(img, avatar, acx, acy, ar):
        draw = ImageDraw.Draw(img)
        draw.ellipse([acx - ar, acy - ar, acx + ar, acy + ar], outline=(60, 63, 72), width=2)
    else:
        draw.ellipse([acx - ar, acy - ar, acx + ar, acy + ar], outline=(60, 63, 72), width=2, fill=(20, 21, 27))
        n_font = _find_font(58, bold=True)
        draw.text((acx - draw.textlength("N", font=n_font) / 2, acy - 38), "N", font=n_font, fill=(235, 236, 240))
    draw.text((206, 62), "NEXUS VPN", font=title_font, fill=white)
    draw.text((208, 118), "Как дела у серверов", font=sub_font, fill=text_dim)

    # Главный итог - большой плашкой справа
    kind, title, hint = card_summary(servers)
    color = {"ok": GREEN, "warn": YELLOW, "bad": RED}[kind]
    tw = draw.textlength(title, font=big_font)
    pw, ph = int(tw + 96), 72
    px, py = W - 60 - pw, 76
    draw.rounded_rectangle([px, py, px + pw, py + ph], radius=ph // 2,
                           fill=_mix(color, bg, 0.16), outline=_mix(color, bg, 0.6), width=2)
    _draw_status_icon(draw, px + 38, py + ph / 2, 19, kind, color)
    draw.text((px + 70, py + ph / 2 - 19), title, font=big_font, fill=color)

    # Подсказка, что делать (до двух строк)
    lines, cur = [], ""
    for word in hint.split():
        test = f"{cur} {word}".strip()
        if draw.textlength(test, font=hint_font) <= W - 140:
            cur = test
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    if len(lines) > 2:
        lines = lines[:2]
        lines[1] = lines[1][:-1] + "…"
    for n, line in enumerate(lines):
        draw.text((70, 184 + n * 32), line, font=hint_font, fill=(214, 216, 226))

    # Заголовки колонок
    cols = {"name": 70, "vpn": 330, "rf": 580, "load": 870, "speed": 1080}
    hy = 262
    for key, label in (("name", "СЕРВЕР"), ("vpn", "VPN"), ("rf", "ИЗ РОССИИ"),
                       ("load", "ЗАГРУЗКА"), ("speed", "СКОРОСТЬ")):
        draw.text((cols[key], hy), label, font=head_font, fill=(110, 113, 125))

    for i, sv in enumerate(servers):
        top = rows_y + i * (row_h + row_gap)
        cy = top + row_h // 2
        draw.rounded_rectangle([50, top, W - 50, top + row_h], radius=18, fill=panel)

        name = str(sv.get("name") or "Сервер")
        while draw.textlength(name, font=name_font) > cols["vpn"] - cols["name"] - 20 and len(name) > 4:
            name = name[:-2] + "…"
        draw.text((cols["name"], cy - 18), name, font=name_font, fill=white)

        xray = sv.get("xray")
        if xray:
            _draw_pill(draw, cols["vpn"], cy, "Работает", "ok", GREEN, pill_font, panel)
        elif xray is False:
            _draw_pill(draw, cols["vpn"], cy, "Не работает", "bad", RED, pill_font, panel)
        else:
            _draw_pill(draw, cols["vpn"], cy, "Нет связи", "unknown", GRAY, pill_font, panel)

        rf = sv.get("rf")
        status = rf.status if rf is not None else "none"
        text, k, c = {
            "ok": ("Открывается", "ok", GREEN),
            "partial": ("Не у всех", "warn", YELLOW),
            "blocked": ("Не открывается", "bad", RED),
            "unknown": ("Не проверено", "unknown", GRAY),
            "none": ("Не проверено", "unknown", GRAY),
        }[status]
        _draw_pill(draw, cols["rf"], cy, text, k, c, pill_font, panel)

        value, word, lc = _load_level(sv.get("cpu"), sv.get("mem"))
        draw.text((cols["load"], cy - 24), word, font=pill_font, fill=lc)
        bar_w = 160
        draw.rounded_rectangle([cols["load"], cy + 10, cols["load"] + bar_w, cy + 20], radius=5, fill=(42, 44, 51))
        if value is not None:
            fill_w = max(10, int(bar_w * min(value, 100) / 100))
            draw.rounded_rectangle([cols["load"], cy + 10, cols["load"] + fill_w, cy + 20], radius=5, fill=lc)

        down, up = sv.get("down"), sv.get("up")
        if isinstance(down, (int, float)) and isinstance(up, (int, float)):
            draw.text((cols["speed"], cy - 26), f"↓ {down * 8 / 1e6:.1f} Мбит/с", font=small_font, fill=blue)
            draw.text((cols["speed"], cy + 4), f"↑ {up * 8 / 1e6:.1f} Мбит/с", font=small_font, fill=blue)
        else:
            draw.text((cols["speed"], cy - 10), "—", font=pill_font, fill=GRAY)

    when = (checked_at or datetime.now(MSK)).astimezone(MSK).strftime("%H:%M")
    draw.text((70, H - 48), f"Проверено в {when} по Москве · запросил {requested_by}",
              font=small_font, fill=(95, 98, 108))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _is_xray_process(name: str, cmdline: list) -> bool:
    if name.lower().startswith("xray"):
        return True
    if cmdline:
        return os.path.basename(cmdline[0]).lower().startswith("xray")
    return False


def _sample_system_stats():
    """Блокирующий сбор метрик (~1 секунда) - вызывается через asyncio.to_thread.
    CPU и сеть меряются за одну и ту же секунду."""
    psutil.cpu_percent(interval=None)  # первый вызов только запускает замер
    net_before = psutil.net_io_counters()
    time.sleep(1)
    cpu_percent = psutil.cpu_percent(interval=None)
    net_after = psutil.net_io_counters()
    mem = psutil.virtual_memory()
    sent_speed = net_after.bytes_sent - net_before.bytes_sent
    recv_speed = net_after.bytes_recv - net_before.bytes_recv

    xray_running = False
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if _is_xray_process(proc.info.get("name") or "", proc.info.get("cmdline") or []):
                xray_running = True
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return cpu_percent, mem, sent_speed, recv_speed, xray_running


_status_cooldowns: dict = {}
_avatar_cache: dict = {}
AVATAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "avatar.png")


async def _bot_avatar() -> bytes | None:
    """Текущая аватарка бота из Discord (кэш на час), иначе assets/avatar.png."""
    cached = _avatar_cache.get("data")
    if cached and time.time() - _avatar_cache.get("ts", 0) < 3600:
        return cached
    data = None
    try:
        if client.user is not None and client.user.avatar is not None:
            data = await client.user.display_avatar.replace(size=256, static_format="png").read()
    except Exception as e:
        log.warning("Не удалось скачать аватарку бота: %s", e)
    if not data:
        try:
            with open(AVATAR_FILE, "rb") as f:
                data = f.read()
        except OSError:
            data = None
    if data:
        _avatar_cache.update(data=data, ts=time.time())
    return data


async def _collect_servers() -> list:
    """Строки серверов для карточки: Xray доп. серверов через API и
    доступность всех серверов из РФ."""
    servers = [{"name": primary.name, "xray": None, "rf": None, "target": primary.check_target}]
    for panel in active_extras():
        row = {"name": panel.name, "xray": None, "rf": None, "target": panel.check_target}
        try:
            st = await panel.api.server_status()
            row["xray"] = str((st.get("xray") or {}).get("state", "")).lower() == "running"
            mem = st.get("mem") or {}
            if mem.get("total"):
                row["mem"] = 100 * (mem.get("current") or 0) / mem["total"]
            if st.get("cpu") is not None:
                row["cpu"] = float(st["cpu"])
            net = st.get("netIO") or {}
            if net.get("down") is not None and net.get("up") is not None:
                row["down"], row["up"] = float(net["down"]), float(net["up"])
        except XUIError as e:
            log.warning("Статус сервера «%s»: %s", panel.name, e)
        servers.append(row)
    if RF_CHECK:
        targets = {sv["name"]: sv["target"] for sv in servers if sv["target"]}
        results = await rf_check.check_targets(targets) if targets else {}
        for sv in servers:
            sv["rf"] = results.get(sv["name"])
    return servers


@tree.command(name="status_server", description="Статус сервера и VPN (публикуется в канал статуса)")
async def status_server_cmd(interaction: discord.Interaction):
    if not can_view_status(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return

    if not STATUS_WEBHOOK_URL:
        await interaction.response.send_message(
            "STATUS_WEBHOOK_URL не настроен на сервере - обратитесь к администратору.",
            ephemeral=True,
        )
        return

    now = time.time()
    last = _status_cooldowns.get(interaction.user.id, 0)
    if not is_admin(interaction) and now - last < STATUS_COOLDOWN_SEC:
        wait = int(STATUS_COOLDOWN_SEC - (now - last)) + 1
        await interaction.response.send_message(f"Подождите {wait} сек. перед повторным запросом.",
                                                ephemeral=True)
        return
    _status_cooldowns[interaction.user.id] = now

    await interaction.response.defer(ephemeral=True)

    (cpu_percent, mem, sent_speed, recv_speed, xray_running), servers = await asyncio.gather(
        asyncio.to_thread(_sample_system_stats), _collect_servers(),
    )
    # Основной сервер - тот, где работает бот: его цифры меряем на месте.
    servers[0].update(xray=xray_running, cpu=cpu_percent, mem=mem.percent,
                      down=recv_speed, up=sent_speed)

    png_bytes = await asyncio.to_thread(
        _render_status_card, servers, interaction.user.display_name, await _bot_avatar(),
    )

    try:
        webhook = discord.Webhook.from_url(STATUS_WEBHOOK_URL, client=client)
        await webhook.send(
            file=discord.File(io.BytesIO(png_bytes), filename="status.png"),
            username="NEXUS_VPN Status",
        )
        await interaction.followup.send("Статус отправлен ✅", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Не удалось отправить через вебхук: `{e}`", ephemeral=True)

    for sv in servers:
        if sv["xray"] is False:
            await notify_admin(f"🔴 /status_server: Xray на сервере «{sv['name']}» не запущен!",
                               key=f"xray_down:{sv['name']}", cooldown=600)
        rf = sv.get("rf")
        if rf is not None and rf.status == "blocked":
            await notify_admin(f"🚫 /status_server: сервер «{sv['name']}» недоступен из РФ "
                               f"(0/{rf.total} узлов check-host.net)",
                               key=f"rf_blocked:{sv['name']}", cooldown=3600)


@tree.command(name="revoke", description="[Админ] Отозвать подписку пользователя")
@app_commands.describe(
    member="Пользователь, чью подписку нужно отозвать",
    remove_roles="Снять роли VPN/Trial (иначе при следующей сверке доступ выдастся снова)",
)
async def revoke(interaction: discord.Interaction, member: discord.Member, remove_roles: bool = True):
    if not is_admin(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    discord_id = str(member.id)
    async with _member_lock(discord_id):
        sub = db.get_subscription(discord_id)
        if sub:
            try:
                await _delete_sub(sub)
            except XUIError as e:
                await interaction.followup.send(f"Ошибка при удалении в 3x-ui: `{e}`", ephemeral=True)
                return

        roles_note = ""
        if remove_roles:
            to_remove = [r for r in member.roles if r.name in (VPN_ROLE_NAME, TRIAL_ROLE_NAME)]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove, reason=f"/revoke от {interaction.user}")
                    roles_note = "\nРоли сняты: " + ", ".join(r.name for r in to_remove)
                except discord.HTTPException as e:
                    roles_note = (f"\n⚠️ Не удалось снять роли ({e}). Пока роль у пользователя есть, "
                                  f"доступ выдастся снова при следующей сверке.")

    if not sub:
        await interaction.followup.send(f"У {member.mention} не было подписки.{roles_note}", ephemeral=True)
        return
    await interaction.followup.send(f"Подписка пользователя {member.mention} отозвана.{roles_note}",
                                    ephemeral=True)


@tree.command(name="sync", description="[Админ] Сверить роли участников с VPN-подписками")
async def sync_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    stats = await reconcile()
    await interaction.followup.send(_format_stats(stats), ephemeral=True)


@tree.command(name="reset_trial", description="[Админ] Разрешить пользователю пробный период ещё раз")
@app_commands.describe(member="Пользователь")
async def reset_trial_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return
    existed = db.reset_trial(str(member.id))
    note = ("Теперь можно снова выдать роль «" + TRIAL_ROLE_NAME + "» (если она уже стоит - снимите и выдайте заново).")
    await interaction.response.send_message(
        (f"Пробный период для {member.mention} сброшен. " if existed else
         f"{member.mention} ещё не использовал пробный период. ") + note,
        ephemeral=True,
    )


@tree.command(name="vpn_stats", description="[Админ] Количество подписок")
async def vpn_stats_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return
    counts = db.count_subscriptions()
    await interaction.response.send_message(
        f"Платных подписок: {counts.get('paid', 0)}\nПробных подписок: {counts.get('trial', 0)}\n"
        f"Открытых обращений: {db.count_open_tickets()}\n"
        f"Режим панели: {'API' if api_mode() else 'браузер'}; серверы: "
        + ", ".join([primary.name] + [p.name for p in active_extras()]),
        ephemeral=True,
    )


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    log.error("Ошибка команды /%s: %r", interaction.command.name if interaction.command else "?",
              original, exc_info=original)
    await notify_admin(f"⚠️ Ошибка в команде /{interaction.command.name if interaction.command else '?'} "
                       f"от {interaction.user}: {original!r}")
    text = "Произошла ошибка, администратор уведомлён. Попробуйте позже."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


# ---------- поддержка ----------

TICKET_CLOSE_ID = "nexus:ticket_close"


class TicketCloseView(discord.ui.View):
    """Кнопка в первом сообщении обращения. Постоянная (timeout=None и
    custom_id) - работает и после перезапуска бота."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Закрыть обращение", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id=TICKET_CLOSE_ID)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        ticket = db.get_ticket_by_thread(getattr(thread, "id", 0))
        if ticket is None or not isinstance(thread, discord.Thread):
            await interaction.response.send_message("Это не ветка обращения.", ephemeral=True)
            return
        if str(interaction.user.id) != ticket["discord_id"] and not is_admin(interaction):
            await interaction.response.send_message("Закрыть обращение может автор или администратор.",
                                                    ephemeral=True)
            return
        db.close_ticket(thread.id)
        await interaction.response.send_message(f"Обращение закрыто ({interaction.user.mention}). "
                                                f"Если вопрос вернётся - откройте новое через `/support`.")
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException as e:
            log.warning("Не удалось архивировать ветку %s: %s", thread.id, e)


async def _support_channel():
    if not SUPPORT_CHANNEL_ID:
        return None
    channel = client.get_channel(int(SUPPORT_CHANNEL_ID))
    if channel is None:
        try:
            channel = await client.fetch_channel(int(SUPPORT_CHANNEL_ID))
        except discord.HTTPException:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


def _sub_summary(discord_id: str) -> str:
    sub = db.get_subscription(discord_id)
    if not sub:
        used = " (пробный уже использован)" if db.has_used_trial(discord_id) else ""
        return f"Подписки нет{used}."
    kind = "платная" if sub["kind"] == "paid" else "пробная"
    return f"Подписка: {kind}, клиент `{sub['email']}`."


@tree.command(name="support", description="Написать в поддержку - откроется приватная ветка с админами")
@app_commands.describe(topic="Коротко: что случилось")
async def support_cmd(interaction: discord.Interaction, topic: str = ""):
    channel = await _support_channel()
    if channel is None:
        await interaction.response.send_message(
            "Поддержка через бота не настроена. Напишите администратору напрямую.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    existing = db.get_open_ticket(discord_id)
    if existing:
        thread = channel.guild.get_thread(int(existing["thread_id"]))
        if thread is None:
            try:
                thread = await client.fetch_channel(int(existing["thread_id"]))
            except discord.HTTPException:
                thread = None
        if isinstance(thread, discord.Thread) and not thread.archived:
            await interaction.followup.send(f"У вас уже есть открытое обращение: {thread.mention}",
                                            ephemeral=True)
            return
        db.close_ticket(existing["thread_id"])

    topic = (topic or "").strip()[:200]
    try:
        thread = await channel.create_thread(
            name=f"Поддержка · {interaction.user.display_name}"[:90],
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"/support от {interaction.user}",
        )
        await thread.add_user(interaction.user)
        admin_role = discord.utils.get(channel.guild.roles, name=ADMIN_ROLE_NAME)
        if admin_role is not None:
            for admin in admin_role.members[:10]:
                try:
                    await thread.add_user(admin)
                except discord.HTTPException:
                    pass
        await thread.send(
            f"{interaction.user.mention}{' ' + admin_role.mention if admin_role else ''}\n"
            f"**Обращение:** {topic or 'без темы'}\n{_sub_summary(discord_id)}\n\n"
            f"Опишите проблему: на каком устройстве и в каком приложении, что именно не работает. "
            f"Скриншоты помогут. Когда вопрос решён - нажмите кнопку ниже.",
            view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )
    except discord.HTTPException as e:
        log.error("Не удалось создать ветку поддержки: %s", e)
        await interaction.followup.send(
            "Не получилось открыть обращение (у бота нет прав на приватные ветки?). "
            "Напишите администратору напрямую.", ephemeral=True)
        await notify_admin(f"⚠️ /support: не удалось создать ветку для {interaction.user}: {e}")
        return

    db.create_ticket(discord_id, thread.id, topic)
    await interaction.followup.send(f"Обращение открыто: {thread.mention}", ephemeral=True)
    await notify_admin(f"🆘 Новое обращение от {interaction.user} ({discord_id}): {topic or 'без темы'}")


# ---------- рассылка ----------

AUDIENCES = {"all": "всем подписчикам", "paid": "платным подписчикам", "trial": "пробным подписчикам"}


class AnnounceConfirmView(discord.ui.View):
    def __init__(self, author_id: int, text: str, recipients: list):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.text = text
        self.recipients = recipients
        self.send.label = f"Отправить ({len(recipients)})"

    @discord.ui.button(label="Отправить", style=discord.ButtonStyle.primary)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Подтвердить может только автор рассылки.", ephemeral=True)
            return
        await interaction.response.edit_message(content=f"Отправляю {len(self.recipients)} пользователям…",
                                                view=None)
        sent = failed = 0
        message = f"📢 **NEXUS VPN**\n\n{self.text}"
        for discord_id in self.recipients:
            user = find_member(int(discord_id))
            if user is None:
                try:
                    user = await client.fetch_user(int(discord_id))
                except discord.HTTPException:
                    failed += 1
                    continue
            try:
                await user.send(message)
                sent += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(1)  # бережём лимиты Discord
        result = f"Рассылка завершена: доставлено {sent}, не доставлено {failed} (закрытые ЛС и т.п.)."
        try:
            await interaction.followup.send(result, ephemeral=True)
        except discord.HTTPException:
            pass
        await notify_admin(f"📢 Рассылка от {interaction.user}: {result}")

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Рассылка отменена.", view=None)


class AnnounceModal(discord.ui.Modal, title="Рассылка подписчикам"):
    text = discord.ui.TextInput(label="Текст сообщения", style=discord.TextStyle.paragraph,
                                max_length=1800, placeholder="Например: плановые работы сегодня с 3:00 до 3:30 МСК")

    def __init__(self, audience: str):
        super().__init__()
        self.audience = audience

    async def on_submit(self, interaction: discord.Interaction):
        kind = None if self.audience == "all" else self.audience
        recipients = [sub["discord_id"] for sub in db.list_by_kind(kind)]
        if not recipients:
            await interaction.response.send_message("Получателей нет.", ephemeral=True)
            return
        preview = (f"Предпросмотр - получат **{len(recipients)}** ({AUDIENCES[self.audience]}):\n"
                   f"────────\n📢 **NEXUS VPN**\n\n{self.text.value}\n────────")
        await interaction.response.send_message(
            preview[:1990], view=AnnounceConfirmView(interaction.user.id, self.text.value, recipients),
            ephemeral=True,
        )


@tree.command(name="announce", description="[Админ] Рассылка подписчикам в личные сообщения")
@app_commands.describe(audience="Кому отправить")
@app_commands.choices(audience=[
    app_commands.Choice(name="Всем подписчикам", value="all"),
    app_commands.Choice(name="Только платным", value="paid"),
    app_commands.Choice(name="Только пробным", value="trial"),
])
async def announce_cmd(interaction: discord.Interaction, audience: app_commands.Choice[str] | None = None):
    if not is_admin(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return
    await interaction.response.send_modal(AnnounceModal(audience.value if audience else "all"))


# ---------- IP-адреса и контроль «шаринга» ----------

async def _client_ips(email: str) -> list:
    entries = []
    for client_api in [xui] + [p.api for p in active_extras()]:
        try:
            entries += await client_api.get_client_ips(email)
        except XUIError as e:
            log.warning("IP клиента %s: %s", email, e)
    return entries


@tree.command(name="ips", description="[Админ] IP-адреса, с которых подключался пользователь")
@app_commands.describe(member="Пользователь")
async def ips_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("Недостаточно прав.", ephemeral=True)
        return
    if not api_mode():
        await interaction.response.send_message("Доступно только при работе через API-токен (XUI_API_TOKEN).",
                                                ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    sub = db.get_subscription(str(member.id))
    if not sub:
        await interaction.followup.send("У пользователя нет подписки.", ephemeral=True)
        return
    entries = await _client_ips(sub["email"])
    recent = recent_ips(entries)
    if not entries:
        await interaction.followup.send(
            f"{member.mention}: записей об IP нет (в панели должен быть включён журнал IP / IP Limit).",
            ephemeral=True)
        return
    body = "\n".join(f"`{e}`" for e in entries[:40])
    await interaction.followup.send(
        f"{member.mention}: разных IP за сутки - **{len(recent)}** (лимит устройств {DEVICE_LIMIT}).\n{body}",
        ephemeral=True,
    )


@tasks.loop(hours=1)
async def check_sharing():
    """Если по одной подписке за сутки подключались с подозрительно большого
    числа разных IP - сообщаем админам (сами ничего не блокируем: смена
    Wi-Fi/мобильной сети тоже даёт новый IP)."""
    if not api_mode() or not SHARING_CHECK:
        return
    for sub in db.list_subscriptions():
        try:
            recent = recent_ips(await _client_ips(sub["email"]))
        except Exception as e:
            log.warning("Проверка шаринга %s: %s", sub["email"], e)
            continue
        if len(recent) > SHARING_IP_THRESHOLD:
            ips = ", ".join(sorted(recent)[:15])
            await notify_admin(
                f"👥 Возможная передача подписки: <@{sub['discord_id']}> (`{sub['email']}`) - "
                f"{len(recent)} разных IP за сутки (порог {SHARING_IP_THRESHOLD}, лимит устройств "
                f"{DEVICE_LIMIT}): {ips}. Подробнее: `/ips`.",
                key=f"sharing:{sub['discord_id']}", cooldown=24 * 3600,
            )
        await asyncio.sleep(0.2)


@check_sharing.error
async def _check_sharing_error(error):
    log.exception("Ошибка в check_sharing", exc_info=error)


# ---------- реакция на роли ----------

@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Boosty выдаёт роль при оплате и снимает при отмене/просрочке - бот
    замечает это и приводит VPN-доступ в соответствие."""
    before_names = _role_names(before)
    after_names = _role_names(after)
    watched = {VPN_ROLE_NAME, TRIAL_ROLE_NAME}
    if (before_names ^ after_names) & watched:
        trial_just_added = TRIAL_ROLE_NAME in after_names and TRIAL_ROLE_NAME not in before_names
        await _safe_sync(after, trial_just_added=trial_just_added)


@client.event
async def on_member_remove(member: discord.Member):
    """Человек вышел с сервера - роли пропали, а on_member_update не придёт."""
    if find_member(member.id):  # остался на другом сервере бота - решит сверка
        return
    discord_id = str(member.id)
    async with _member_lock(discord_id):
        sub = db.get_subscription(discord_id)
        if not sub:
            return
        try:
            await _delete_sub(sub)
            await send_dm(member, "Вы покинули сервер, поэтому доступ к VPN отозван.")
        except XUIError as e:
            await notify_admin(f"⚠️ {member} ({member.id}) вышел с сервера, но удалить VPN не удалось: {e}",
                               key=f"leave:{member.id}")


# ---------- сверка ролей и подписок ----------

_reconcile_lock = asyncio.Lock()


def _format_stats(stats: Counter) -> str:
    if stats.get("skipped"):
        return f"Сверка пропущена: {stats.get('reason', 'см. лог')}"
    parts = [
        f"Проверено участников: {stats.get('checked', 0)}",
        f"Выдано: {stats.get('granted', 0)}",
        f"Trial → платная: {stats.get('upgraded', 0)}",
        f"Выдано пробных: {stats.get('trial', 0)}",
        f"Отозвано: {stats.get('revoked', 0)}",
    ]
    if stats.get("errors"):
        parts.append(f"Ошибок: {stats['errors']} (подробности в админ-канале/логе)")
    if stats.get("guarded"):
        parts.append(f"⚠️ Не удалено из-за защиты от массового удаления: {stats['guarded']}")
    return "\n".join(parts)


async def reconcile() -> Counter:
    """Догоняет изменения ролей, которые бот пропустил (был выключен,
    перезапускался, упал браузер и т.п.)."""
    stats = Counter()
    if xui is None:
        stats.update(skipped=1)
        stats["reason"] = "нет подключения к панели"
        return stats

    async with _reconcile_lock:
        watched = {VPN_ROLE_NAME, TRIAL_ROLE_NAME}
        members: dict = {}
        all_chunked = True
        roles_exist = False
        for g in client.guilds:
            if not g.chunked:
                try:
                    await g.chunk()
                except Exception as e:
                    log.warning("Не удалось загрузить участников сервера %s: %s", g.id, e)
                    all_chunked = False
            if any(r.name in watched for r in g.roles):
                roles_exist = True
            for m in g.members:
                if m.bot:
                    continue
                key = str(m.id)
                prev = members.get(key)
                if prev is None or (not (_role_names(prev) & watched) and _role_names(m) & watched):
                    members[key] = m

        if not roles_exist:
            # Роль переименовали/удалили - не трогаем подписки, иначе удалили бы всех.
            await notify_admin(f"⚠️ Сверка пропущена: на сервере нет ролей «{VPN_ROLE_NAME}» / "
                               f"«{TRIAL_ROLE_NAME}». Проверьте VPN_ROLE_NAME/TRIAL_ROLE_NAME.",
                               key="no_roles")
            stats.update(skipped=1)
            stats["reason"] = "на сервере нет VPN-ролей"
            return stats

        subs = {s["discord_id"]: s for s in db.list_subscriptions()}

        # Кому нужно что-то сделать: у кого есть роль, и у кого есть подписка.
        to_check = {did for did, m in members.items() if _role_names(m) & watched}
        to_check |= {did for did in subs if did in members}
        absent = [s for did, s in subs.items() if did not in members] if all_chunked else []

        # Защита от массового удаления (например, Boosty-интеграция отвалилась
        # и разом сняла роли): если удалить надо больше половины подписок -
        # ничего не удаляем и сообщаем админам.
        planned_revokes = [did for did in to_check
                           if did in subs and not (_role_names(members[did]) & watched)]
        planned_total = len(planned_revokes) + len(absent)
        guard = planned_total > max(5, len(subs) // 2)
        if guard:
            stats["guarded"] = planned_total
            await notify_admin(
                f"⚠️ Сверка хотела удалить {planned_total} из {len(subs)} подписок - это подозрительно "
                f"много, удаление пропущено. Проверьте роли на сервере, затем запустите /sync.",
                key="mass_revoke", cooldown=3600,
            )
            to_check -= set(planned_revokes)
            absent = []

        for did in to_check:
            stats["checked"] += 1
            try:
                for action in await sync_member(members[did], quiet=True):
                    stats[action] += 1
            except Exception as e:
                stats["errors"] += 1
                await notify_admin(f"⚠️ Сверка: ошибка для {members[did]} ({did}): {e}", key=f"sync:{did}")

        for sub in absent:
            async with _member_lock(sub["discord_id"]):
                if db.get_subscription(sub["discord_id"]) is None:
                    continue
                try:
                    await _delete_sub(sub)
                    stats["revoked"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    await notify_admin(f"⚠️ Сверка: не удалось удалить подписку ушедшего {sub['discord_id']}: {e}",
                                       key=f"sync:{sub['discord_id']}")

    changed = sum(stats.get(k, 0) for k in ("granted", "upgraded", "trial", "revoked"))
    log.info("Сверка завершена: %s", dict(stats))
    if changed or stats.get("errors"):
        await notify_admin("🔄 Сверка ролей:\n" + _format_stats(stats))
    return stats


@tasks.loop(minutes=max(SYNC_INTERVAL_MINUTES, 1))
async def reconcile_loop():
    try:
        await reconcile()
    except Exception:
        log.exception("Ошибка периодической сверки")


# ---------- фоновая проверка истёкших пробных подписок ----------

def _boosty_hint() -> str:
    return f"\nОформить подписку: {BOOSTY_URL}" if BOOSTY_URL else "\nОформите подписку на Boosty."


async def _remind_trial(discord_id: str, exp_ms: int, days: int):
    user = find_member(int(discord_id))
    if user is None:
        try:
            user = await client.fetch_user(int(discord_id))
        except discord.HTTPException:
            return
    when = f"<t:{exp_ms // 1000}:R> (<t:{exp_ms // 1000}:f>)"
    await send_dm(user, f"⏳ Пробный доступ к NEXUS VPN закончится {when}.\n"
                        f"Чтобы VPN работал без перерыва, оформите подписку заранее - "
                        f"бот сразу пришлёт постоянную ссылку." + _boosty_hint())
    log.info("Напоминание о конце trial (%s дн.) отправлено %s", days, discord_id)


@tasks.loop(hours=1)
async def check_expired():
    """Пробные подписки: напоминания за 2 дня и за 1 день, удаление по окончании.
    Срок берётся с панелей: при «Start After First Use» он идёт с первого
    подключения (на любом из серверов)."""
    now_ms = int(time.time() * 1000)

    for sub in db.list_expiring():
        discord_id = sub["discord_id"]
        member = None
        async with _member_lock(discord_id):
            sub = db.get_subscription(discord_id)
            if not sub or not sub["expiry_ms"]:
                continue

            state, panel_ms = await _panel_expiry_state(sub)
            if state == "not_started":
                continue  # ещё ни разу не подключался - срок не начался
            if state == "started":
                if panel_ms != sub["expiry_ms"]:
                    db.update_expiry(discord_id, panel_ms)
                exp = panel_ms
            else:
                exp = sub["expiry_ms"]  # панели не ответили - по данным бота

            left = exp - now_ms
            if left > 0:
                flags = sub.get("reminders") or 0
                if state == "started" and left <= 86_400_000 and not flags & db.REMIND_1D:
                    db.add_reminder_flag(discord_id, db.REMIND_1D | db.REMIND_2D)
                    await _remind_trial(discord_id, exp, 1)
                elif state == "started" and left <= 2 * 86_400_000 and not flags & db.REMIND_2D:
                    db.add_reminder_flag(discord_id, db.REMIND_2D)
                    await _remind_trial(discord_id, exp, 2)
                continue

            try:
                await _delete_sub(sub)
            except XUIError as e:
                await notify_admin(f"⚠️ Не удалось удалить истёкшую подписку {discord_id}: {e}",
                                   key=f"expire:{discord_id}")
                continue

            member = find_member(int(discord_id))
            if REMOVE_TRIAL_ROLE_ON_EXPIRY and member is not None:
                role = discord.utils.get(member.roles, name=TRIAL_ROLE_NAME)
                if role is not None:
                    try:
                        await member.remove_roles(role, reason="Пробный период закончился")
                    except discord.HTTPException as e:
                        log.warning("Не удалось снять роль Trial у %s: %s", discord_id, e)

        target = member
        if target is None:
            try:
                target = await client.fetch_user(int(discord_id))
            except discord.HTTPException:
                target = None
        if target is not None:
            await send_dm(target, "Срок вашей VPN-подписки истёк, доступ отключён." + _boosty_hint())


@check_expired.error
async def _check_expired_error(error):
    log.exception("Ошибка в check_expired", exc_info=error)


# ---------- запуск ----------

async def _setup():
    global xui
    try:
        if XUI_API_TOKEN:
            log.info("Подключаюсь к панели 3x-ui «%s» через API-токен...", primary.name)
            xui = await XUIApi.create(
                XUI_BASE_URL, XUI_API_TOKEN, verify_ssl=XUI_VERIFY_SSL,
                web_base_path=XUI_WEB_BASE_PATH, name=primary.name, inbound_ids=XUI_INBOUND_IDS,
            )
        else:
            log.info("Подключаюсь к панели 3x-ui (headless-браузер, может занять 10-20 секунд)...")
            xui = await XUIClient.create(
                XUI_BASE_URL, XUI_USERNAME, XUI_PASSWORD,
                verify_ssl=XUI_VERIFY_SSL, web_base_path=XUI_WEB_BASE_PATH,
                static_csrf_token=XUI_STATIC_CSRF_TOKEN,
            )
        primary.api = xui
        log.info("Подключение к 3x-ui успешно (%s).", xui.login_debug_info)
    except XUIError as e:
        log.error("НЕ УДАЛОСЬ подключиться к 3x-ui: %s", e)
        log.error("Запустите `python test_connection.py` для подробной диагностики.")
        raise SystemExit(1)

    for panel in extra_panels:
        if not api_mode():
            log.warning("Сервер «%s» пропущен: доп. серверы работают, только если и основной "
                        "подключён через XUI_API_TOKEN.", panel.name)
            continue
        if not panel.env.get("token") or not panel.env.get("sub_base_ok"):
            log.warning("Сервер «%s» пропущен: нужны XUI_API_TOKEN и XUI_SUB_BASE этого сервера.", panel.name)
            continue
        try:
            panel.api = await XUIApi.create(
                panel.env["base_url"], panel.env["token"], verify_ssl=panel.env["verify_ssl"],
                web_base_path=panel.env["web_base_path"], name=panel.name,
                inbound_ids=panel.env["inbound_ids"],
            )
            log.info("Доп. сервер «%s» подключён (%s).", panel.name, panel.api.login_debug_info)
        except XUIError as e:
            # Не роняем бота: основной сервер работает, сверка догонит позже.
            log.error("Доп. сервер «%s» недоступен: %s", panel.name, e)

    client.add_view(TicketCloseView())  # кнопки «Закрыть обращение» после перезапуска

    if NEXUS_LINK_BASE:
        global link_server

        def _sub_url_for(sub_id: str):
            # Только действующая подписка: не отключена и срок не вышел
            # (expiry_ms = 0 - бессрочная).
            sub = db.get_subscription_by_sub_id(sub_id)
            if not sub or not sub.get("active"):
                return None
            exp = sub.get("expiry_ms") or 0
            if exp and exp <= int(time.time() * 1000):
                return None
            return plain_sub_url(sub_id)

        link_server = LinkServer(_sub_url_for, host=NEXUS_LINK_HOST, port=NEXUS_LINK_PORT,
                                 cert=NEXUS_LINK_CERT, key=NEXUS_LINK_KEY,
                                 download_url=NEXUS_DOWNLOAD_URL, logo_path=AVATAR_FILE)
        try:
            await link_server.start()
        except Exception as e:
            # Без переходника бот работает дальше, просто кнопка будет вести в никуда.
            log.error("Не удалось запустить переходник «Добавить в Nexus» на порту %s: %s",
                      NEXUS_LINK_PORT, e)
            await notify_admin(f"⚠️ Переходник «Добавить в Nexus» не запустился: {e}")
            link_server = None

    if TEST_GUILD_ID:
        # Команды регистрируются только на этот сервер (мгновенно). Порядок
        # важен: сначала копируем в гильдию и синхронизируем, потом чистим
        # глобальный список, чтобы не было дублей.
        guild = discord.Object(id=int(TEST_GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        tree.clear_commands(guild=None)
        await tree.sync()
        log.info("Команды синхронизированы только на сервер %s (мгновенно).", TEST_GUILD_ID)
    else:
        await tree.sync()
        log.info("Команды синхронизированы глобально (применение может занять до часа).")


_started = False
READY_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ready")


def _write_ready_marker():
    """Отметка «бот успешно запустился» - по ней deploy.sh понимает, что
    обновление прошло нормально."""
    try:
        with open(READY_MARKER, "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        log.warning("Не удалось записать %s: %s", READY_MARKER, e)


@client.event
async def on_ready():
    global _started
    log.info("Бот запущен как %s", client.user)
    if _started:
        return  # on_ready повторяется после переподключений
    _started = True
    _write_ready_marker()
    if not check_expired.is_running():
        check_expired.start()
    if not check_sharing.is_running():
        check_sharing.start()
    if SYNC_INTERVAL_MINUTES > 0 and not reconcile_loop.is_running():
        reconcile_loop.start()  # первый проход - сразу после запуска
    elif SYNC_INTERVAL_MINUTES <= 0:
        asyncio.create_task(reconcile())  # хотя бы один раз при старте


def _log_handler() -> logging.Handler:
    """bot.log с автоматической ротацией: файл не растёт бесконечно."""
    path = LOG_FILE if os.path.isabs(LOG_FILE) else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), LOG_FILE)
    return RotatingFileHandler(path, maxBytes=max(LOG_MAX_MB, 1) * 1024 * 1024,
                               backupCount=max(LOG_BACKUPS, 1), encoding="utf-8")


if __name__ == "__main__":
    client.run(
        DISCORD_TOKEN,
        log_handler=_log_handler(),
        log_formatter=logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
                                        "%Y-%m-%d %H:%M:%S"),
        root_logger=True,
    )
