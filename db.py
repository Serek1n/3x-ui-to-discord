"""
Хранилище подписок в SQLite.

Таблица subscriptions хранит связь между Discord-пользователем и клиентом
в 3x-ui, чтобы бот мог отвечать на /myvpn и отзывать доступ без лишних
запросов к панели.

Поле kind:
    'paid'  - выдана за роль VPN_ROLE_NAME (Boosty), бессрочная, пока есть роль;
    'trial' - пробный период по роли TRIAL_ROLE_NAME, со сроком.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

# По умолчанию база лежит рядом с кодом бота (как и раньше, т.к. systemd
# запускает бота из этой же папки). Можно переопределить через DB_PATH.
DB_PATH = (os.environ.get("DB_PATH", "").split(" #")[0].strip()
           or os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscriptions.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id    TEXT NOT NULL UNIQUE,
    email         TEXT NOT NULL UNIQUE,
    client_uuid   TEXT NOT NULL,
    sub_id        TEXT NOT NULL,
    inbound_id    INTEGER NOT NULL,
    expiry_ms     INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    auto_renew    INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL DEFAULT 'paid',
    reminders     INTEGER NOT NULL DEFAULT 0
);
"""

# Обращения в поддержку (/support): одна открытая ветка на человека.
TICKETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  TEXT NOT NULL,
    thread_id   TEXT NOT NULL UNIQUE,
    topic       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  INTEGER NOT NULL,
    closed_at   INTEGER
);
"""

# Битовые флаги напоминаний о конце пробного периода.
REMIND_2D = 1
REMIND_1D = 2

# Отдельная таблица, независимая от subscriptions - факт использования пробного
# периода остаётся навсегда, даже если саму подписку потом удалят/отзовут.
TRIALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    discord_id  TEXT PRIMARY KEY,
    used_at     INTEGER NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column(conn, ddl):
    try:
        conn.execute(ddl)
        return True
    except sqlite3.OperationalError:
        return False  # колонка уже есть


def init_db():
    with get_conn() as conn:
        # WAL - чтобы бэкап и бот не мешали друг другу.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)
        conn.execute(TRIALS_SCHEMA)
        conn.execute(TICKETS_SCHEMA)
        # Миграции для баз, созданных старыми версиями бота.
        _add_column(conn, "ALTER TABLE subscriptions ADD COLUMN auto_renew INTEGER NOT NULL DEFAULT 0")
        if _add_column(conn, "ALTER TABLE subscriptions ADD COLUMN kind TEXT NOT NULL DEFAULT 'paid'"):
            # Старые записи: со сроком - это trial, без срока - платные.
            conn.execute("UPDATE subscriptions SET kind = 'trial' WHERE expiry_ms > 0")
        _add_column(conn, "ALTER TABLE subscriptions ADD COLUMN reminders INTEGER NOT NULL DEFAULT 0")


def create_subscription(discord_id, email, client_uuid, sub_id, inbound_id, expiry_ms, kind="paid"):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO subscriptions
               (discord_id, email, client_uuid, sub_id, inbound_id, expiry_ms, created_at, active, auto_renew, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)""",
            (discord_id, email, client_uuid, sub_id, inbound_id, expiry_ms, int(time.time()), kind),
        )


def get_subscription(discord_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return dict(row) if row else None


def list_subscriptions():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM subscriptions").fetchall()]


def count_subscriptions():
    with get_conn() as conn:
        rows = conn.execute("SELECT kind, COUNT(*) AS n FROM subscriptions GROUP BY kind").fetchall()
        return {r["kind"]: r["n"] for r in rows}


def update_expiry(discord_id, new_expiry_ms):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET expiry_ms = ? WHERE discord_id = ?",
            (new_expiry_ms, discord_id),
        )


def update_client_ids(discord_id, client_uuid, sub_id):
    """После перевыпуска ссылки: новые uuid и subId, остальное как было."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET client_uuid = ?, sub_id = ? WHERE discord_id = ?",
            (client_uuid, sub_id, discord_id),
        )


def add_reminder_flag(discord_id, flag):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET reminders = reminders | ? WHERE discord_id = ?",
            (flag, discord_id),
        )


def list_by_kind(kind=None):
    with get_conn() as conn:
        if kind:
            rows = conn.execute("SELECT * FROM subscriptions WHERE kind = ?", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM subscriptions").fetchall()
        return [dict(r) for r in rows]


def delete_subscription(discord_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM subscriptions WHERE discord_id = ?", (discord_id,))


def list_expiring():
    """Подписки со сроком (пробные) - кандидаты на напоминания и удаление.
    auto_renew=1 исключены - их продлевает сама панель."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE expiry_ms > 0 AND auto_renew = 0 AND active = 1"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- пробный период ----------

def has_used_trial(discord_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trials WHERE discord_id = ?", (discord_id,)
        ).fetchone()
        return row is not None


def mark_trial_used(discord_id):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO trials (discord_id, used_at) VALUES (?, ?)",
            (discord_id, int(time.time())),
        )


def reset_trial(discord_id):
    """Разрешить пользователю получить пробный период ещё раз."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM trials WHERE discord_id = ?", (discord_id,))
        return cur.rowcount > 0


# ---------- обращения в поддержку ----------

def get_open_ticket(discord_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE discord_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (discord_id,),
        ).fetchone()
        return dict(row) if row else None


def get_ticket_by_thread(thread_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tickets WHERE thread_id = ?", (str(thread_id),)).fetchone()
        return dict(row) if row else None


def create_ticket(discord_id, thread_id, topic):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tickets (discord_id, thread_id, topic, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (discord_id, str(thread_id), topic, int(time.time())),
        )


def close_ticket(thread_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE thread_id = ?",
            (int(time.time()), str(thread_id)),
        )


def count_open_tickets():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'").fetchone()[0]
