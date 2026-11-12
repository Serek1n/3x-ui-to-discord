#!/usr/bin/env bash
# Обновление бота на VPS одной командой:
#   бэкап → git pull → зависимости → systemd unit → перезапуск → проверка.
# Если после обновления бот не запустился за 90 секунд - автоматический откат
# на прошлую версию.
#
# Вручную:   sudo bash /opt/3x-ui-to-discord/deploy.sh
# Принудительно (даже без новых коммитов, например в первый раз):
#            sudo bash /opt/3x-ui-to-discord/deploy.sh --force
# Автоматически это делает GitHub Actions после каждого Push (см. README).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${SERVICE_NAME:-xui-discord-bot}"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
READY="$APP_DIR/.ready"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ "$EUID" -ne 0 ]; then
  echo "Запустите от root: sudo bash $0"
  exit 1
fi

cd "$APP_DIR"
export GIT_TERMINAL_PROMPT=0
git config --global --add safe.directory "$APP_DIR" >/dev/null 2>&1 || true

OLD=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "==> Бэкап перед обновлением"
bash "$APP_DIR/backup.sh" || echo "!! Бэкап не удался - продолжаю"

echo "==> git pull ($BRANCH)"
if ! git pull --ff-only origin "$BRANCH"; then
  echo "!! git pull не прошёл. Скорее всего, файлы на сервере меняли вручную:"
  git status --short
  echo "   Посмотрите изменения выше. Отменить их: git -C $APP_DIR checkout -- ."
  exit 1
fi
NEW=$(git rev-parse HEAD)

if [ "$OLD" = "$NEW" ] && [ "$FORCE" -eq 0 ]; then
  echo "==> Новых изменений нет ($NEW). Бот не перезапускался."
  exit 0
fi
echo "==> Версия: ${OLD:0:7} → ${NEW:0:7}"
git log --oneline "$OLD..$NEW" | head -20 || true

install_deps() {
  if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/.venv"
  fi
  "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
}

# Строки лога без адресов: в них может быть секретный путь панели или subId.
show_log() {
  tail -n 30 "$APP_DIR/bot.log" 2>/dev/null | sed -E 's#https?://[^ )"]+#<адрес скрыт>#g' || true
  journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null \
    | sed -E 's#https?://[^ )"]+#<адрес скрыт>#g' || true
}

rollback_code() {
  echo "==> Откат на ${OLD:0:7}"
  git reset --hard "$OLD"
  if ! git diff --quiet "$NEW" "$OLD" -- requirements.txt; then
    install_deps || echo "!! Не удалось восстановить зависимости прошлой версии"
  fi
}

if [ "$FORCE" -eq 1 ] || ! git diff --quiet "$OLD" "$NEW" -- requirements.txt; then
  echo "==> Устанавливаю зависимости"
  if ! install_deps; then
    echo "!! Зависимости не установились. Бот не перезапускался."
    if [ "$OLD" != "$NEW" ]; then
      rollback_code
    fi
    exit 1
  fi
fi

# Пользователь сервиса: как в текущем unit, иначе root.
SERVICE_USER=root
if [ -f "$UNIT_FILE" ]; then
  SERVICE_USER=$(sed -n 's/^User=//p' "$UNIT_FILE" | head -1)
  SERVICE_USER=${SERVICE_USER:-root}
fi

install_unit() {
  local tmp
  tmp=$(mktemp)
  sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$SERVICE_USER|g" \
      "$APP_DIR/deploy/bot.service.template" > "$tmp"
  if [ ! -f "$UNIT_FILE" ] || ! cmp -s "$tmp" "$UNIT_FILE"; then
    echo "==> Обновляю systemd unit $UNIT_FILE"
    install -m 644 "$tmp" "$UNIT_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
  fi
  rm -f "$tmp"
}
install_unit

restart_and_wait() {
  rm -f "$READY"
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"
  echo "==> Жду запуска бота (до 90 секунд)..."
  for _ in $(seq 1 30); do
    sleep 3
    if [ -f "$READY" ] && systemctl is-active --quiet "$SERVICE"; then
      return 0
    fi
  done
  return 1
}

if restart_and_wait; then
  echo "==> Готово: бот запущен на версии ${NEW:0:7}"
  exit 0
fi

echo "!! Бот не запустился на новой версии. Последние строки лога:"
show_log

if [ "$OLD" = "$NEW" ]; then
  echo "!! Откатываться некуда (версия не менялась). Проверьте .env и лог выше."
  exit 1
fi

rollback_code
install_unit
if restart_and_wait; then
  echo "==> Откат выполнен, бот работает на прошлой версии ${OLD:0:7}"
else
  echo "!! Бот не запустился и после отката - нужна ручная проверка"
fi
exit 1
