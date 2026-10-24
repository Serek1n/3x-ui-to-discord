#!/usr/bin/env bash
# Установка Discord-бота (3x-ui интеграция) как systemd-сервиса на Ubuntu.
# Запускать из директории с ботом (там, где bot.py, .env, requirements.txt):
#   sudo bash install_ubuntu.sh

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash install_ubuntu.sh"
  exit 1
fi

APP_DIR="$(pwd)"
SERVICE_USER="${SUDO_USER:-$(whoami)}"
SERVICE_NAME="xui-discord-bot"

echo "==> Директория проекта: $APP_DIR"
echo "==> Сервис будет запускаться от пользователя: $SERVICE_USER"

if [ ! -f "$APP_DIR/requirements.txt" ]; then
  echo "ОШИБКА: requirements.txt не найден в $APP_DIR."
  echo "Убедитесь, что все файлы бота скопированы в эту директорию перед запуском."
  exit 1
fi

echo "==> Устанавливаю python3-venv (если ещё не установлен)"
apt-get update -qq
apt-get install -y python3-venv python3-pip

echo "==> Создаю виртуальное окружение .venv"
python3 -m venv "$APP_DIR/.venv"

echo "==> Устанавливаю зависимости"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Устанавливаю браузер Chromium для Playwright (нужен для обхода JS-защиты панели)"
"$APP_DIR/.venv/bin/playwright" install --with-deps chromium

if [ ! -f "$APP_DIR/.env" ]; then
  echo "!! Файл .env не найден в $APP_DIR. Создайте его из .env.example перед запуском."
fi

echo "==> Проверяю подключение к 3x-ui (без запуска Discord-бота)"
"$APP_DIR/.venv/bin/python" "$APP_DIR/test_connection.py" || {
  echo ""
  echo "Проверка подключения не прошла - см. вывод выше. Сервис всё равно будет создан,"
  echo "но перед стартом стоит поправить .env и повторить: python test_connection.py"
}

echo "==> Создаю systemd unit /etc/systemd/system/${SERVICE_NAME}.service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Discord bot for 3x-ui VPN subscriptions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python -u ${APP_DIR}/bot.py
Restart=always
RestartSec=5
TimeoutStopSec=20
StandardOutput=append:${APP_DIR}/bot.log
StandardError=append:${APP_DIR}/bot.log

[Install]
WantedBy=multi-user.target
EOF

echo "==> Перечитываю конфигурацию systemd"
systemctl daemon-reload

echo "==> Включаю автозапуск и запускаю сервис"
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2
systemctl --no-pager status "${SERVICE_NAME}" || true

echo ""
echo "Готово. Полезные команды:"
echo "  sudo systemctl status ${SERVICE_NAME}      - статус"
echo "  sudo systemctl restart ${SERVICE_NAME}     - перезапуск (например, после правки .env)"
echo "  sudo journalctl -u ${SERVICE_NAME} -f       - логи в реальном времени"
echo "  tail -f ${APP_DIR}/bot.log                 - логи бота (файл)"
echo "  ${APP_DIR}/.venv/bin/python ${APP_DIR}/test_connection.py  - проверка связи с 3x-ui отдельно"
