"""Переходник «Добавить в Nexus»: только адреса из базы, экранирование,
заголовки, ограничение перебора subId."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
import aiohttp  # noqa: E402
from harness import check, run  # noqa: E402
from link_server import LinkServer, SUB_ID_RE  # noqa: E402

VALID = {"abcdef0123456789": "https://h.example:2096/sub/abcdef0123456789"}
PORT = 18611
BASE = f"http://127.0.0.1:{PORT}"
srv = None


async def setup():
    global srv
    if srv is not None:
        await srv.stop()
    srv = LinkServer(lambda s: VALID.get(s), host="0.0.0.0", port=PORT,
                     download_url='https://x.example/"><script>alert(1)</script>')
    await srv.start()


async def test_localhost_without_cert():
    bound = [s.getsockname()[0] for site in srv._runner.sites for s in site._server.sockets]
    check(bound == ["127.0.0.1"], f"без сертификата слушает {bound}")


async def test_page_is_safe():
    async with aiohttp.ClientSession(trust_env=False) as http:
        r = await http.get(BASE + "/add/abcdef0123456789", allow_redirects=False)
        body = await r.text()
    check(r.status == 200 and "Location" not in r.headers, "страница делает редирект")
    check("<script>alert(1)" not in body and "&lt;script&gt;" in body, "значения не экранируются")
    check("nexus://import?url=https%3A%2F%2Fh.example%3A2096%2Fsub%2Fabcdef0123456789" in body, "неверный deeplink")
    csp = r.headers["Content-Security-Policy"]
    check("default-src 'none'" in csp and r.headers["X-Frame-Options"] == "DENY", "нет заголовков безопасности")


async def test_bad_paths_404():
    bad = ["/add/unknown0000000000", "/add/..%2f..%2f.env", "/add/abc", "/add/" + "a" * 65,
           "/fonts/..%2f..%2f.env", "/fonts/../../bot.py", "/.env", "/subscriptions.db", "/bot.log"]
    async with aiohttp.ClientSession(trust_env=False) as http:
        for p in bad:
            r = await http.get(BASE + p, allow_redirects=False)
            t = await r.text()
            check(r.status == 404 and "DISCORD_TOKEN" not in t and "import " not in t, f"{p}: {r.status}")


async def test_bruteforce_is_limited():
    """LNK-02: перебор subId с одного IP упирается в ограничение."""
    async with aiohttp.ClientSession(trust_env=False) as http:
        codes = []
        for i in range(40):
            r = await http.get(BASE + f"/add/{i:016d}")
            codes.append(r.status)
        r = await http.get(BASE + "/add/abcdef0123456789")
    check(codes.count(404) <= 10 and 429 in codes, f"перебор не ограничен: {codes.count(404)} ответов 404")
    check(r.status == 429, "после перебора страница всё ещё отдаётся этому IP")


async def test_normal_use_not_limited():
    async with aiohttp.ClientSession(trust_env=False) as http:
        for _ in range(5):
            r = await http.get(BASE + "/add/abcdef0123456789")
            check(r.status == 200, "обычное открытие страницы ограничено")


async def test_sub_id_pattern():
    check(not SUB_ID_RE.match("a/b") and not SUB_ID_RE.match("a.b") and SUB_ID_RE.match("abcd_-12"), "")




run([test_localhost_without_cert, test_page_is_safe, test_bad_paths_404, test_bruteforce_is_limited,
     test_normal_use_not_limited, test_sub_id_pattern], setup=setup)
