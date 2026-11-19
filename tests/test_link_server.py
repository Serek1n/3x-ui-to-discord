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


async def fake_sub(token):
    if token == "tokenOK00000000000000000":
        return "dmxlc3M6Ly94", {"Content-Type": "text/plain; charset=utf-8", "profile-update-interval": "12"}
    if token == "tokenDOWN000000000000000":
        return "unavailable"
    return None


async def setup():
    global srv
    if srv is not None:
        await srv.stop()
    srv = LinkServer(lambda s: VALID.get(s), host="0.0.0.0", port=PORT,
                     download_url='https://x.example/"><script>alert(1)</script>', sub_content=fake_sub)
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


async def test_bot_subscription_endpoint():
    async with aiohttp.ClientSession(trust_env=False) as http:
        r = await http.get(BASE + "/sub/tokenOK00000000000000000")
        body = await r.text()
        check(r.status == 200 and body == "dmxlc3M6Ly94" and r.headers["profile-update-interval"] == "12",
              f"подписка: {r.status} {body!r}")
        r = await http.get(BASE + "/sub/tokenDOWN000000000000000")
        check(r.status == 503, f"недоступная подписка: {r.status}")
        r = await http.get(BASE + "/sub/unknown00000000000000000")
        check(r.status == 404, f"чужой токен: {r.status}")
        r = await http.get(BASE + "/sub/..%2f.env")
        check(r.status == 404, "путь с выходом из папки")


async def test_sub_formats():
    import base64
    import json
    from bot_sub import decode_body, rename, merge_userinfo, parse_userinfo
    plain = "vless://a@h:1#x\ntrojan://b@h:2#y\n# comment\n"
    check(decode_body(plain) == ["vless://a@h:1#x", "trojan://b@h:2#y"], "простой текст")
    check(decode_body(base64.b64encode(plain.encode()).decode()) == ["vless://a@h:1#x", "trojan://b@h:2#y"], "base64")
    check(decode_body("<html>oops</html>") == [], "мусор принят за подписку")
    vm = "vmess://" + base64.b64encode(json.dumps({"add": "h", "ps": "old"}).encode()).decode()
    out = json.loads(base64.b64decode(rename(vm, "🇩🇪 Германия")[8:]).decode())
    check(out["ps"] == "🇩🇪 Германия" and out["add"] == "h", "vmess не переименован")
    check(rename("vless://a@h:1?x=1#old", "PL").endswith("#PL"), "vless не переименован")
    m = merge_userinfo([parse_userinfo("upload=1; download=2; total=0; expire=200"),
                        parse_userinfo("upload=3; download=4; total=10; expire=100"), {}])
    check(m == "upload=4; download=6; total=10; expire=100", m)


async def test_sub_id_pattern():
    check(not SUB_ID_RE.match("a/b") and not SUB_ID_RE.match("a.b") and SUB_ID_RE.match("abcd_-12"), "")




run([test_localhost_without_cert, test_page_is_safe, test_bad_paths_404, test_bruteforce_is_limited,
     test_normal_use_not_limited, test_bot_subscription_endpoint, test_sub_formats, test_sub_id_pattern],
    setup=setup)
