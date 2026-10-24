"""
Discord-бот для выдачи и управления VPN-подписками через 3x-ui.

Команды:
    /myvpn              - показать свою подписку, срок, трафик и QR-код
    /status_server      - [Админ/VPN/Trial] статус сервера и Xray (через Discord Webhook)
    /revoke @user       - [Админ] отозвать подписку пользователя (и снять VPN-роли)
    /sync               - [Админ] сверить роли участников с подписками прямо сейчас
    /reset_trial @user  - [Админ] разрешить пользователю пробный период ещё раз
    /vpn_stats          - [Админ] сколько сейчас подписок

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

Подключение к 3x-ui выполняется через headless-браузер (см. xui_client.py),
поэтому все вызовы XUIClient - асинхронные (await).
"""

import asyncio
import io
import logging
import os
import re
import time
from collections import Counter

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
XUI_USERNAME = _env("XUI_USERNAME") or os.environ["XUI_USERNAME"]
XUI_PASSWORD = os.environ["XUI_PASSWORD"].strip()
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

xui: XUIClient | None = None


class VPNBot(discord.Client):
    async def setup_hook(self):
        await _setup()

    async def close(self):
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

async def _create_sub(discord_id: str, kind: str) -> str:
    """Создаёт клиента в 3x-ui и запись в БД. Возвращает ссылку подписки."""
    days = TRIAL_DAYS if kind == "trial" else 0
    email = f"discord_{discord_id}"
    created = await xui.add_client(XUI_INBOUND_ID, email, days, ip_limit=DEVICE_LIMIT)
    db.create_subscription(
        discord_id, email, created["uuid"], created["subId"],
        XUI_INBOUND_ID, created["expiryMs"], kind=kind,
    )
    log.info("Выдана подписка %s пользователю %s", kind, discord_id)
    return sub_link(created["subId"])


async def _delete_sub(sub: dict):
    await xui.delete_client(sub["inbound_id"], sub["client_uuid"], email=sub["email"])
    db.delete_subscription(sub["discord_id"])
    log.info("Удалена подписка %s пользователя %s", sub.get("kind"), sub["discord_id"])


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
                return actions
            upgraded = sub is not None
            if sub:
                # Был trial: панель не даёт редактировать клиента запросом,
                # поэтому пересоздаём его уже бессрочным.
                await _delete_sub(sub)
            link = await _create_sub(discord_id, "paid")
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
                return actions
            if db.has_used_trial(discord_id):
                if trial_just_added and not quiet:
                    await send_dm(member, "Пробный период уже был использован и доступен только один раз. "
                                          "Оформите подписку на Boosty, чтобы получить постоянный доступ.")
                return actions
            link = await _create_sub(discord_id, "trial")
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

@tree.command(name="myvpn", description="Показать вашу подписку, ссылку и QR-код")
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
        await interaction.followup.send(
            "У вас пока нет подписки. Оформите доступ через Boosty.", ephemeral=True,
        )
        return

    kind = "Пробная" if sub["kind"] == "trial" else "Платная"
    expiry = "бессрочно (пока есть роль)" if sub["expiry_ms"] == 0 else f"<t:{sub['expiry_ms'] // 1000}:F>"

    traffic_line = ""
    traffic = await xui.get_client_traffic(sub["email"])
    if traffic:
        up_gb = (traffic.get("up") or 0) / 1024 ** 3
        down_gb = (traffic.get("down") or 0) / 1024 ** 3
        traffic_line = f"\nИспользовано: ↑{up_gb:.2f} ГБ / ↓{down_gb:.2f} ГБ"
        panel_exp = traffic.get("expiryTime")
        if isinstance(panel_exp, (int, float)) and sub["kind"] == "trial":
            if panel_exp < 0:
                days_left = int(-panel_exp // 86_400_000)
                expiry = f"начнётся с первого подключения ({days_left} {_plural_days(days_left)})"
            elif panel_exp > 0:
                expiry = f"<t:{int(panel_exp) // 1000}:F> (<t:{int(panel_exp) // 1000}:R>)"

    link = sub_link(sub["sub_id"])
    await interaction.followup.send(
        f"Подписка: {kind}\nСрок действия: {expiry}\nУстройств: до {DEVICE_LIMIT}"
        f"{traffic_line}\nСсылка: {link}",
        file=make_qr_file(link),
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


def _render_status_card(cpu_percent: float, mem, sent_speed: float, recv_speed: float,
                         xray_running: bool, requested_by: str) -> bytes:
    """Рисует PNG-карточку статуса сервера (Pillow) - вызывается через
    asyncio.to_thread, т.к. отрисовка блокирующая."""
    W, H = 1300, 560
    bg = (13, 14, 18)
    card_border = (40, 42, 50)
    text_dim = (150, 153, 163)
    text_bright = (255, 255, 255)
    accent = (87, 242, 135) if xray_running else (237, 66, 69)
    icon_color = (137, 143, 255)
    icon_box_fill = (26, 28, 38)
    bar_start, bar_end = (99, 102, 241), (79, 172, 254)

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([1, 1, W - 2, H - 2], radius=28, outline=card_border, width=2, fill=(15, 16, 20))

    title_font = _find_font(38, bold=True)
    subtitle_font = _find_font(20)
    label_font = _find_font(26, bold=True)
    value_font = _find_font(24, bold=True)
    status_font = _find_font(24, bold=True)
    small_font = _find_font(16)

    # Круглый аватар с буквой N
    avatar_cx, avatar_cy, avatar_r = 140, 130, 70
    draw.ellipse(
        [avatar_cx - avatar_r, avatar_cy - avatar_r, avatar_cx + avatar_r, avatar_cy + avatar_r],
        outline=(60, 63, 72), width=2, fill=(20, 21, 27),
    )
    n_font = _find_font(64, bold=True)
    n_w = draw.textlength("N", font=n_font)
    draw.text((avatar_cx - n_w / 2, avatar_cy - 42), "N", font=n_font, fill=(235, 236, 240))

    # Заголовок + зелёная точка "онлайн" + подзаголовок
    title_x, title_y = 250, 78
    draw.text((title_x, title_y), "NEXUS_VPN", font=title_font, fill=text_bright)
    title_w = draw.textlength("NEXUS_VPN", font=title_font)
    dot_x = title_x + title_w + 22
    dot_y = title_y + 20
    draw.ellipse([dot_x - 9, dot_y - 9, dot_x + 9, dot_y + 9], fill=(87, 242, 135))
    draw.text((title_x, title_y + 56), "Мониторинг в реальном времени", font=subtitle_font, fill=text_dim)

    # Три колонки метрик: CPU / Память / Трафик
    row_y = 250
    icon_size = 56
    col_x = [70, 480, 890]
    col_w = 360

    # CPU
    x = col_x[0]
    _draw_icon_box(draw, x, row_y, icon_size, "cpu", icon_color, icon_box_fill)
    draw.text((x + icon_size + 16, row_y + 12), "CPU", font=label_font, fill=text_bright)
    cpu_val = f"{cpu_percent:.1f}%"
    draw.text((x + col_w - draw.textlength(cpu_val, font=value_font), row_y + 14),
              cpu_val, font=value_font, fill=(122, 162, 255))
    _draw_gradient_bar(img, x, row_y + icon_size + 20, col_w, 14, cpu_percent, bar_start, bar_end)
    draw = ImageDraw.Draw(img)  # ImageDraw объект нужно пересоздать после paste в _draw_gradient_bar

    # Память
    x = col_x[1]
    _draw_icon_box(draw, x, row_y, icon_size, "memory", icon_color, icon_box_fill)
    draw.text((x + icon_size + 16, row_y + 12), "Память", font=label_font, fill=text_bright)
    mem_val = f"{mem.used / 1024**3:.1f} / {mem.total / 1024**3:.1f} ГБ"
    draw.text((x + col_w - draw.textlength(mem_val, font=value_font), row_y + 14),
              mem_val, font=value_font, fill=(122, 162, 255))
    _draw_gradient_bar(img, x, row_y + icon_size + 20, col_w, 14, mem.percent, bar_start, bar_end)
    draw = ImageDraw.Draw(img)

    # Трафик
    x = col_x[2]
    _draw_icon_box(draw, x, row_y, icon_size, "traffic", icon_color, icon_box_fill)
    draw.text((x + icon_size + 16, row_y + 12), "Трафик", font=label_font, fill=text_bright)
    up_text = f"↑ {_fmt_speed(sent_speed)}"
    down_text = f"↓ {_fmt_speed(recv_speed)}"
    draw.text((x, row_y + icon_size + 12), up_text, font=value_font, fill=(122, 162, 255))
    draw.text((x, row_y + icon_size + 44), down_text, font=value_font, fill=(122, 162, 255))

    # Разделитель
    divider_y = row_y + icon_size + 90
    draw.line([(60, divider_y), (W - 60, divider_y)], fill=(40, 42, 50), width=1)

    # Xray
    xray_y = divider_y + 30
    x = col_x[0]
    _draw_icon_box(draw, x, xray_y, icon_size, "pulse", icon_color, icon_box_fill)
    draw.text((x + icon_size + 16, xray_y + 16), "Xray", font=label_font, fill=text_bright)
    label_w = draw.textlength("Xray", font=label_font)
    sep_x = x + icon_size + 16 + label_w + 20
    draw.line([(sep_x, xray_y + 8), (sep_x, xray_y + icon_size - 8)], fill=(60, 62, 70), width=2)
    dot_x2 = sep_x + 24
    dot_y2 = xray_y + icon_size / 2
    draw.ellipse([dot_x2 - 8, dot_y2 - 8, dot_x2 + 8, dot_y2 + 8], fill=accent)
    status_text = "Работает" if xray_running else "Не запущен"
    draw.text((dot_x2 + 20, xray_y + 16), status_text, font=status_font, fill=accent)

    footer_y = xray_y + icon_size + 20
    draw.text((70, footer_y), f"Запрошено: {requested_by}", font=small_font, fill=(90, 92, 100))

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

    cpu_percent, mem, sent_speed, recv_speed, xray_running = await asyncio.to_thread(_sample_system_stats)

    png_bytes = await asyncio.to_thread(
        _render_status_card, cpu_percent, mem, sent_speed, recv_speed,
        xray_running, interaction.user.display_name,
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

    if not xray_running:
        await notify_admin("🔴 /status_server: Xray не запущен!", key="xray_down", cooldown=600)


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
        f"Платных подписок: {counts.get('paid', 0)}\nПробных подписок: {counts.get('trial', 0)}",
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

@tasks.loop(hours=1)
async def check_expired():
    now_ms = int(time.time() * 1000)

    for sub in db.list_expiring():
        if not sub["expiry_ms"] or sub["expiry_ms"] >= now_ms:
            continue

        discord_id = sub["discord_id"]
        async with _member_lock(discord_id):
            sub = db.get_subscription(discord_id)
            if not sub or not sub["expiry_ms"]:
                continue

            # Сверяемся с фактическим сроком в панели: при "Start After First
            # Use" он отсчитывается от первого подключения, а не от выдачи.
            traffic = await xui.get_client_traffic(sub["email"])
            panel_exp = traffic.get("expiryTime") if traffic else None
            if isinstance(panel_exp, (int, float)):
                if panel_exp < 0:
                    continue  # ещё ни разу не подключался - срок не начался
                if panel_exp > now_ms:
                    db.update_expiry(discord_id, int(panel_exp))
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
            await send_dm(target, "Срок вашей VPN-подписки истёк, доступ отключён. "
                                  "Оформите подписку на Boosty, чтобы продолжить пользоваться VPN.")


@check_expired.error
async def _check_expired_error(error):
    log.exception("Ошибка в check_expired", exc_info=error)


# ---------- запуск ----------

async def _setup():
    global xui
    log.info("Подключаюсь к панели 3x-ui (headless-браузер, может занять 10-20 секунд)...")
    try:
        xui = await XUIClient.create(
            XUI_BASE_URL, XUI_USERNAME, XUI_PASSWORD,
            verify_ssl=XUI_VERIFY_SSL, web_base_path=XUI_WEB_BASE_PATH,
            static_csrf_token=XUI_STATIC_CSRF_TOKEN,
        )
        log.info("Подключение к 3x-ui успешно (%s).", xui.login_debug_info)
    except XUIError as e:
        log.error("НЕ УДАЛОСЬ подключиться к 3x-ui: %s", e)
        log.error("Запустите `python test_connection.py` для подробной диагностики.")
        raise SystemExit(1)

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


@client.event
async def on_ready():
    global _started
    log.info("Бот запущен как %s", client.user)
    if _started:
        return  # on_ready повторяется после переподключений
    _started = True
    if not check_expired.is_running():
        check_expired.start()
    if SYNC_INTERVAL_MINUTES > 0 and not reconcile_loop.is_running():
        reconcile_loop.start()  # первый проход - сразу после запуска
    elif SYNC_INTERVAL_MINUTES <= 0:
        asyncio.create_task(reconcile())  # хотя бы один раз при старте


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
