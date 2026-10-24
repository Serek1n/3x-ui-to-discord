#!/usr/bin/env bash
# Создаёт архив текущего состояния бота (код + база данных + .env) в /opt/backups.
# Запуск: sudo bash backup.sh
# Ежедневно в 4:00 автоматически (одна команда):
#   (sudo crontab -l 2>/dev/null; echo "0 4 * * * bash /opt/xui-discord-bot/backup.sh >/dev/null 2>&1") | sudo crontab -
set -e

APP_DIR="/opt/xui-discord-bot"
BACKUP_DIR="/opt/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

NAME="$(basename "$APP_DIR")"
mkdir -p "$TMP_DIR/$NAME"

# Консистентная копия базы, даже если бот в этот момент в неё пишет.
DB_ARGS=()
if [ -f "$APP_DIR/subscriptions.db" ]; then
  python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
    "$APP_DIR/subscriptions.db" "$TMP_DIR/$NAME/subscriptions.db"
  DB_ARGS=(-C "$TMP_DIR" "$NAME/subscriptions.db")
fi

ARCHIVE="$BACKUP_DIR/${NAME}_$TIMESTAMP.tar.gz"
TAR="$TMP_DIR/backup.tar"
# Пути в --exclude указываются относительно -C (без /opt в начале).
tar --exclude="$NAME/.venv" \
    --exclude="$NAME/__pycache__" \
    --exclude="$NAME/subscriptions.db*" \
    --exclude="$NAME/bot.log" \
    -cf "$TAR" -C "$(dirname "$APP_DIR")" "$NAME"
# Отдельно добавляем консистентную копию базы (её не задевают исключения выше).
if [ ${#DB_ARGS[@]} -gt 0 ]; then
  tar -rf "$TAR" "${DB_ARGS[@]}"
fi
gzip -c "$TAR" > "$ARCHIVE"
chmod 600 "$ARCHIVE"  # внутри .env с паролями

echo "Снапшот сохранён: $ARCHIVE"

# Оставляем только 10 последних снапшотов, чтобы не забить диск
ls -1t "$BACKUP_DIR"/xui-discord-bot_*.tar.gz | tail -n +11 | xargs -r rm --
