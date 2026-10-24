"""
Проверка подключения к 3x-ui без запуска Discord-бота.

Запуск:
    python test_connection.py

Читает те же переменные из .env, что и bot.py, пытается залогиниться
через headless-браузер и выполнить тестовый запрос к API, печатает
подробности на каждом шаге.
"""

import asyncio
import os
import re
import sys

from dotenv import load_dotenv

from xui_client import XUIClient, XUIError

load_dotenv()


def _env(name, default=""):
    # Отрезаем "  # комментарий" после значения (systemd его не убирает).
    return re.sub(r"\s+#.*$", "", os.environ.get(name, default)).strip()


XUI_BASE_URL = _env("XUI_BASE_URL", "")
XUI_USERNAME = _env("XUI_USERNAME", "")
XUI_PASSWORD = os.environ.get("XUI_PASSWORD", "").strip()
XUI_VERIFY_SSL = _env("XUI_VERIFY_SSL", "true").lower() in ("1", "true", "yes")
XUI_WEB_BASE_PATH = _env("XUI_WEB_BASE_PATH", "")
XUI_STATIC_CSRF_TOKEN = _env("XUI_STATIC_CSRF_TOKEN", "")
XUI_INBOUND_ID = _env("XUI_INBOUND_ID", "1")


async def main():
    print("=== Проверка подключения к 3x-ui ===")
    print(f"XUI_BASE_URL       = {XUI_BASE_URL}")
    print(f"XUI_USERNAME        = {XUI_USERNAME}")
    print(f"XUI_VERIFY_SSL      = {XUI_VERIFY_SSL}")
    print(f"XUI_WEB_BASE_PATH   = {XUI_WEB_BASE_PATH!r}")
    print(f"XUI_STATIC_CSRF_TOKEN задан: {'да' if XUI_STATIC_CSRF_TOKEN else 'нет'}")
    print(f"XUI_INBOUND_ID      = {XUI_INBOUND_ID}")
    print()

    if not XUI_BASE_URL or not XUI_USERNAME or not XUI_PASSWORD:
        print("ОШИБКА: не заданы XUI_BASE_URL / XUI_USERNAME / XUI_PASSWORD в .env")
        sys.exit(1)

    xui = None
    try:
        print("-> Открываю headless-браузер и логинюсь в панель (10-20 секунд)...")
        xui = await XUIClient.create(
            XUI_BASE_URL, XUI_USERNAME, XUI_PASSWORD,
            verify_ssl=XUI_VERIFY_SSL, web_base_path=XUI_WEB_BASE_PATH,
            static_csrf_token=XUI_STATIC_CSRF_TOKEN,
        )
        print("OK: логин прошёл успешно.")
        print(f"    Детали логина: {xui.login_debug_info}")
    except XUIError as e:
        print(f"ОШИБКА логина: {e}")
        sys.exit(1)

    print()
    print("-> Проверяю доступ к API (запрос трафика несуществующего клиента - "
          "это нормально закончится 'не найден', главное, что запрос дойдёт)...")
    try:
        traffic = await xui.get_client_traffic("__connection_test__")
        print(f"OK: запрос выполнен (ответ: {traffic}).")
    except XUIError as e:
        print(f"ОШИБКА при запросе к API клиентов: {e}")
        await xui.close()
        sys.exit(1)

    await xui.close()
    print()
    print("Всё в порядке. Бота можно запускать: python bot.py")


if __name__ == "__main__":
    asyncio.run(main())
