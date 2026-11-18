"""Оповещения администраторам бота.

Куда (ADMIN_LOG_MODE):
- dm (по умолчанию) - в личные сообщения: людям из ADMIN_DM_IDS, а если список
  пуст - владельцу бота (или участникам команды) из Discord Developer Portal.
  Если личка закрыта, сообщение уходит в канал ADMIN_LOG_WEBHOOK_URL (если задан).
- channel - только в канал по вебхуку ADMIN_LOG_WEBHOOK_URL.
- both - и туда, и туда.

- Каждое оповещение - карточка (embed) с цветной полосой по важности:
  красная - сбой, оранжевая - предупреждение, зелёная - восстановлено,
  синяя - информация, фиолетовая - обращение в поддержку.
- Сообщения копятся несколько секунд и уходят одним сообщением (до 10 карточек).
- Однотипные сообщения за это время склеиваются в одну карточку со списком
  участников: «Не удалось отозвать доступ ×5 - @a, @b, ...».
- Повтор одного и того же (по key) - не чаще cooldown.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import discord

log = logging.getLogger("admin_log")

LEVELS = {
    #          цвет      подпись
    "error":   (0xE5484D, "Сбой"),
    "warn":    (0xF5A524, "Внимание"),
    "ok":      (0x30A46C, "Восстановлено"),
    "info":    (0x3E63DD, "Событие"),
    "support": (0x8E4EC6, "Поддержка"),
}
# Уровень по эмодзи в начале текста - чтобы старые вызовы работали без правок.
EMOJI_LEVEL = {"🔴": "error", "🚨": "error", "🚫": "error", "🛑": "error", "⚠️": "warn", "⚠": "warn",
               "✅": "ok", "🆘": "support"}
_LEAD_EMOJI = re.compile(r"^\s*([\U0001F000-\U0001FAFF☀-➿⬀-⯿][️]?)\s*")
_ID = re.compile(r"(?:discord_|<@!?)(\d{15,21})>?")   # discord_<id> или <@id>

FLUSH_DELAY = 4.0      # ждём столько секунд после первого сообщения пачки
MAX_EMBEDS = 10        # лимит Discord на сообщение
MAX_DESC = 3800        # лимит описания карточки - 4096
MAX_TOTAL = 5800       # лимит всего текста карточек сообщения - 6000


@dataclass
class Item:
    level: str
    title: str
    text: str
    fields: list = field(default_factory=list)
    who: list = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def group_key(self):
        # Однотипные: одинаковые заголовок и текст, если убрать ID участников.
        if self.fields:
            return None
        return self.level, self.title, _ID.sub("#", self.text)




def _strip_ids(text: str) -> str:
    """Текст без ID участников - общий для склеенной карточки."""
    t = _ID.sub("", text)
    t = re.sub(r"\s+(для|у|пользователя|участника)(?=\s*[:.,)]|\s*$)", "", t)
    t = re.sub(r"\(\s*\)", "", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip(" :")


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AdminLog:
    def __init__(self, url: str = "", username: str = "NEXUS_VPN Logs", *, mode: str = "dm", dm_ids=()):
        self.url = url
        self.username = username
        self.mode = mode if mode in ("dm", "channel", "both") else "dm"
        self.dm_ids = [int(i) for i in dm_ids]
        self.client = None          # discord.Client - задаётся ботом
        self._queue: list = []
        self._task = None
        self._last: dict = {}
        self._recipients = None     # кэш: кому писать в личку
        self._dm_failed: set = set()

    @property
    def enabled(self) -> bool:
        return self.mode != "channel" or bool(self.url)

    def name(self, uid: str) -> str:
        """Имя участника вместо упоминания: в личке упоминания незнакомых
        клиенту Discord людей показываются как «@unknown-user»."""
        user = self.client.get_user(int(uid)) if self.client is not None else None
        if user is None:
            return f"<@{uid}>"
        return f"**{discord.utils.escape_markdown(user.global_name or user.name)}**"

    def _names(self, text: str) -> str:
        return _ID.sub(lambda m: self.name(m.group(1)), text)

    async def recipients(self) -> list:
        """Кому писать в личку: ADMIN_DM_IDS или владелец бота / команда из Developer Portal."""
        if self._recipients is not None:
            return self._recipients
        ids = list(self.dm_ids)
        if not ids and self.client is not None:
            app = await self.client.application_info()
            if app.team:
                ids = [m.id for m in app.team.members
                       if getattr(m, "membership_state", None) == discord.TeamMembershipState.accepted]
            elif app.owner:
                ids = [app.owner.id]
        users = []
        for uid in ids:
            try:
                users.append(self.client.get_user(uid) or await self.client.fetch_user(uid))
            except discord.HTTPException as e:
                log.error("Администратор %s для оповещений не найден: %s", uid, e)
        self._recipients = users
        if not users:
            log.error("Некому отправлять оповещения в личку: задайте ADMIN_DM_IDS в .env")
        return users

    def post(self, text: str, *, key=None, cooldown: int = 6 * 3600, title=None, level=None,
             fields=None, who=None) -> bool:
        """Ставит оповещение в очередь. Возвращает False, если оно подавлено cooldown."""
        if key:
            now = time.time()
            if now - self._last.get(key, 0) < cooldown:
                return False
            self._last[key] = now
        m = _LEAD_EMOJI.match(text)
        if m:
            level = level or EMOJI_LEVEL.get(m.group(1), "info")
            text = text[m.end():]
        level = level if level in LEVELS else "info"
        if title is None:
            title, text = self._split_title(text)
        ids = [str(w) for w in (who or [])] or list(dict.fromkeys(_ID.findall(text)))
        self._queue.append(Item(level, title, text.strip(), list(fields or []), ids))
        if not self.enabled:
            self._queue.clear()
            return True
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._flush_later())
            except RuntimeError:       # нет цикла событий (тесты, импорт)
                self._queue.clear()
        return True

    @staticmethod
    def _split_title(text: str):
        """«Заголовок: подробности» -> (заголовок, подробности), если заголовок короткий
        и без ID; иначе заголовок по важности не задаётся (пусто)."""
        head, sep, rest = text.partition(": ")
        if sep and 3 <= len(head) <= 60 and "\n" not in head and not _ID.search(head):
            return head.rstrip("."), rest
        first, sep, rest = text.partition("\n")
        if sep and len(first) <= 60 and not _ID.search(first):
            return first.rstrip(":."), rest
        return "", text

    async def _flush_later(self):
        await asyncio.sleep(FLUSH_DELAY)
        while self._queue:
            items, self._queue = self._queue, []
            try:
                await self._send(self._build(items))
            except Exception as e:
                log.error("Не удалось отправить оповещение админам: %s", e)

    def _build(self, items: list) -> list:
        groups: dict = {}
        order = []
        for it in items:
            k = it.group_key or id(it)
            if k not in groups:
                groups[k] = []
                order.append(k)
            groups[k].append(it)
        return [self._embed(groups[k]) for k in order]

    def _embed(self, group: list) -> discord.Embed:
        first = group[0]
        color, label = LEVELS[first.level]
        n = len(group)
        title = first.title or label
        if n > 1:
            title = f"{title} ×{n}"
        if n == 1:
            desc = self._names(first.text)
        else:
            who = list(dict.fromkeys(w for it in group for w in it.who))
            desc = _strip_ids(first.text)
            if who:
                shown = ", ".join(self.name(w) for w in who[:15])
                more = f" и ещё {len(who) - 15}" if len(who) > 15 else ""
                desc = (desc + "\n\n" if desc else "") + f"**Кого касается ({len(who)}):** {shown}{more}"
        emb = discord.Embed(title=_short(title, 250), description=_short(desc, MAX_DESC) or None, color=color,
                            timestamp=discord.utils.utcnow())
        for name, value, *inline in first.fields[:20]:
            emb.add_field(name=_short(str(name), 250), value=_short(str(value), 1000) or "—",
                          inline=bool(inline[0]) if inline else True)
        return emb

    async def _send(self, embeds: list):
        """Делит карточки на сообщения: не больше 10 и не больше ~6000 символов в одном."""
        batch, size = [], 0
        for e in embeds:
            n = len(e)
            if batch and (len(batch) >= MAX_EMBEDS or size + n > MAX_TOTAL):
                await self._post(batch)
                batch, size = [], 0
            batch.append(e)
            size += n
        if batch:
            await self._post(batch)

    async def _post(self, embeds: list):
        dm_ok = True
        if self.mode in ("dm", "both"):
            dm_ok = await self._post_dm(embeds)
        if self.url and (self.mode in ("channel", "both") or not dm_ok):
            await self._post_webhook(embeds)

    async def _post_dm(self, embeds: list) -> bool:
        """В личку каждому администратору. False - никому не дошло."""
        if self.client is None:
            return False
        delivered = False
        for user in await self.recipients():
            try:
                await user.send(embeds=embeds, allowed_mentions=discord.AllowedMentions.none())
                delivered = True
                self._dm_failed.discard(user.id)
            except discord.HTTPException as e:
                if user.id not in self._dm_failed:   # не засоряем лог каждой карточкой
                    self._dm_failed.add(user.id)
                    log.error("Не удалось написать администратору %s в личку (закрыты ЛС?): %s", user.id, e)
        return delivered

    async def _post_webhook(self, embeds: list):
        webhook = discord.Webhook.from_url(self.url, client=self.client)
        await webhook.send(embeds=embeds, username=self.username, allowed_mentions=discord.AllowedMentions.none())

    async def drain(self):
        """Отправить всё, что в очереди (при остановке бота)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._queue and self.enabled:
            items, self._queue = self._queue, []
            try:
                await self._send(self._build(items))
            except Exception as e:
                log.error("Не удалось отправить оповещение админам: %s", e)
