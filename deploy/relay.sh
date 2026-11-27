#!/usr/bin/env bash
# Запасная точка входа для подписки NEXUS VPN.
#
# Ставится на любой VPN-сервер (не на сервер с ботом). Принимает соединения
# на порт подписки и без расшифровки передаёт их серверу бота. Сертификат
# остаётся на боте, ссылки у людей не меняются. Если IP сервера бота
# заблокируют в РФ, бот переключит домен подписки на эту точку (DuckDNS).
#
# Установка (на VPN-сервере, от root):
#   curl -fsSL https://raw.githubusercontent.com/Serek1n/3x-ui-to-discord/main/deploy/relay.sh | bash -s -- АДРЕС_СЕРВЕРА_БОТА [ПОРТ]
# АДРЕС - IP сервера бота или его отдельный домен. Только НЕ домен подписки
# (NEXUS_LINK_BASE): при блокировке он будет указывать сюда же.
# Удаление:
#   bash relay.sh --remove

set -euo pipefail

SERVICE=nexus-sub-relay

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Запустите от root (sudo)." >&2
  exit 1
fi

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  echo "Точка входа удалена."
  exit 0
fi

BOT_IP="${1:-}"
PORT="${2:-8444}"

if ! [[ "$BOT_IP" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
  echo "Укажите IP или домен сервера с ботом (не домен подписки - он будет указывать сюда):" >&2
  echo "  bash relay.sh 203.0.113.10 [порт]" >&2
  exit 1
fi
if ! [[ "$BOT_IP" =~ ^[0-9.]+$ ]]; then
  echo "!! Адрес бота задан доменом ($BOT_IP). Это не должен быть домен подписки из NEXUS_LINK_BASE:"
  echo "!! при блокировке он будет указывать на эту точку, и запросы пойдут по кругу."
fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "Неверный порт: $PORT" >&2
  exit 1
fi

if ss -ltn "( sport = :$PORT )" | grep -q LISTEN && ! systemctl is-active --quiet "$SERVICE"; then
  echo "Порт $PORT на этом сервере уже занят другой программой:" >&2
  ss -ltnp "( sport = :$PORT )" >&2
  echo "Освободите его или укажите другой порт (тогда и в NEXUS_LINK_BASE бота нужен этот же порт)." >&2
  exit 1
fi

echo "==> Устанавливаю socat"
if ! command -v socat >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq socat
fi

echo "==> Создаю службу $SERVICE (порт $PORT -> $BOT_IP:$PORT)"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=NEXUS VPN: запасная точка входа подписки -> $BOT_IP:$PORT
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:$PORT,fork,reuseaddr,max-children=300 TCP:$BOT_IP:$PORT,connect-timeout=10
Restart=always
RestartSec=5
DynamicUser=yes
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl restart "$SERVICE"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Открываю порт $PORT в ufw"
  ufw allow "$PORT/tcp" >/dev/null
fi

sleep 1
if ! systemctl is-active --quiet "$SERVICE"; then
  echo "!! Служба не запустилась:" >&2
  journalctl -u "$SERVICE" -n 20 --no-pager >&2
  exit 1
fi

MY_IP="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
echo
echo "Готово. Точка входа работает: $MY_IP:$PORT -> $BOT_IP:$PORT"
echo "Добавьте этот IP в .env бота: SUB_RELAYS=$MY_IP (несколько - через запятую)"
