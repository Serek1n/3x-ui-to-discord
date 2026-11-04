"""
Проверка подключения к 3x-ui без запуска Discord-бота.

Запуск:
    .venv/bin/python test_connection.py

Если в .env задан XUI_API_TOKEN - проверяет API основного сервера и всех
дополнительных (XUI2_*, XUI3_*...): список inbound'ов, статус сервера и
полный цикл «создать тестового клиента → прописать внешнюю подписку →
удалить». Так до переключения бота видно, что API реально работает.

Без токена - старый режим: логин через headless-браузер.
"""

import asyncio
import os
import re
import sys

from dotenv import load_dotenv

from xui_client import XUIClient, XUIError

load_dotenv()

TEST_EMAIL = "nexus_connection_test"


def _env(name, default=""):
    # Отрезаем "  # комментарий" после значения (systemd его не убирает).
    return re.sub(r"\s+#.*$", "", os.environ.get(name, default)).strip()


def _bool(name, default="true"):
    return _env(name, default).lower() in ("1", "true", "yes", "on")


def _panels():
    panels = [{
        "n": "", "name": _env("XUI_NAME", "Сервер 1"), "base_url": _env("XUI_BASE_URL"),
        "token": os.environ.get("XUI_API_TOKEN", "").strip(),
        "web_base_path": _env("XUI_WEB_BASE_PATH"), "verify_ssl": _bool("XUI_VERIFY_SSL"),
        "inbound_ids": _env("XUI_INBOUND_IDS", "all"), "sub_base": _env("XUI_SUB_BASE").rstrip("/"),
    }]
    for n in range(2, 6):
        if not _env(f"XUI{n}_BASE_URL"):
            continue
        panels.append({
            "n": str(n), "name": _env(f"XUI{n}_NAME", f"Сервер {n}"), "base_url": _env(f"XUI{n}_BASE_URL"),
            "token": os.environ.get(f"XUI{n}_API_TOKEN", "").strip(),
            "web_base_path": _env(f"XUI{n}_WEB_BASE_PATH"), "verify_ssl": _bool(f"XUI{n}_VERIFY_SSL"),
            "inbound_ids": _env(f"XUI{n}_INBOUND_IDS", "all"), "sub_base": _env(f"XUI{n}_SUB_BASE").rstrip("/"),
        })
    return panels


async def check_api() -> bool:
    from xui_api import XUIApi

    ok = True
    apis = []
    for p in _panels():
        prefix = f"XUI{p['n']}_"
        print(f"=== {p['name']} ({p['base_url']}, база: {p['web_base_path'] or '-'}) ===")
        if not p["token"]:
            print(f"ОШИБКА: не задан {prefix}API_TOKEN")
            ok = False
            continue
        try:
            api = await XUIApi.create(p["base_url"], p["token"], verify_ssl=p["verify_ssl"],
                                      web_base_path=p["web_base_path"], name=p["name"],
                                      inbound_ids=p["inbound_ids"])
            print(f"OK: токен принят, inbound'ы для клиентов: {await api.inbound_ids()}")
            st = await api.server_status()
            print(f"OK: статус сервера - Xray: {(st.get('xray') or {}).get('state')}, CPU: {st.get('cpu')}")
            apis.append((p, api))
        except XUIError as e:
            print(f"ОШИБКА: {e}")
            ok = False
        print()

    if not apis or not ok:
        for _, api in apis:
            await api.close()
        return False

    print("=== Тест записи: создать и удалить тестового клиента на каждом сервере ===")
    created = {}
    try:
        for p, api in apis:
            c = await api.add_client(0, TEST_EMAIL, 1, ip_limit=1, comment="проверка бота")
            created[p["name"]] = c
            print(f"OK [{p['name']}]: клиент создан, subId {c['subId']}")
        if len(apis) > 1:
            main_p, main_api = apis[0]
            links = {p["name"]: f"{p['sub_base']}/{created[p['name']]['subId']}" for p, _ in apis[1:]}
            await main_api.set_external_subscriptions(
                TEST_EMAIL, created[main_p["name"]]["subId"], links, [p["sub_base"] for p, _ in apis[1:]])
            has = await main_api.has_external_links(TEST_EMAIL, created[main_p["name"]]["subId"],
                                                    list(links.values()))
            print(f"{'OK' if has else 'ОШИБКА'}: внешние подписки прописаны в основной ({links})")
            ok = ok and has
    except XUIError as e:
        print(f"ОШИБКА: {e}")
        ok = False
    finally:
        for p, api in apis:
            try:
                await api.delete_client(0, "", email=TEST_EMAIL)
                print(f"OK [{p['name']}]: тестовый клиент удалён")
            except XUIError as e:
                print(f"ОШИБКА удаления на {p['name']}: {e} - удалите клиента {TEST_EMAIL} вручную")
                ok = False
            await api.close()
    return ok


async def check_browser() -> bool:
    base = _env("XUI_BASE_URL")
    user = _env("XUI_USERNAME")
    password = os.environ.get("XUI_PASSWORD", "").strip()
    print("=== Проверка через браузер (XUI_API_TOKEN не задан) ===")
    if not base or not user or not password:
        print("ОШИБКА: не заданы XUI_BASE_URL / XUI_USERNAME / XUI_PASSWORD в .env")
        return False
    try:
        print("-> Открываю headless-браузер и логинюсь в панель (10-20 секунд)...")
        xui = await XUIClient.create(
            base, user, password, verify_ssl=_bool("XUI_VERIFY_SSL"),
            web_base_path=_env("XUI_WEB_BASE_PATH"), static_csrf_token=_env("XUI_STATIC_CSRF_TOKEN"),
        )
        print(f"OK: логин прошёл успешно ({xui.login_debug_info}).")
    except XUIError as e:
        print(f"ОШИБКА логина: {e}")
        return False
    traffic = await xui.get_client_traffic("__connection_test__")
    print(f"OK: запрос к API выполнен (ответ: {traffic}).")
    await xui.close()
    return True


async def main():
    if os.environ.get("XUI_API_TOKEN", "").strip():
        ok = await check_api()
    else:
        ok = await check_browser()
    print()
    print("Всё в порядке." if ok else "Есть ошибки - см. выше.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
