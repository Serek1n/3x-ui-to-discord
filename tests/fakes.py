"""Заглушки для тестов: панель 3x-ui (HTTP API v3) и объекты Discord.
Реальные панели и Discord не используются."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


class FakePanel:
    """Минимальная копия API 3x-ui v3 на 127.0.0.1 со случайным портом.
    Поведение повторяет исходники 3x-ui: нет клиента -> success:true, obj:null;
    ошибка -> HTTP 200 и success:false."""

    def __init__(self, token="tok", base="/secretpath"):
        self.token, self.base = token, base
        self.clients, self.links, self.ips = {}, {}, {}
        self.calls = []
        self.fail_traffic = 0      # сколько раз ответить success:false на traffic
        self.fail_add = 0          # сколько раз ответить 500 на add
        self.down = False          # 503 на всё
        self.inbounds = [{"id": 1, "enable": True, "protocol": "vless", "port": 443}]
        panel = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, raw=None):
                body = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                path = urlparse(self.path).path
                panel.calls.append((method, path))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                if panel.down:
                    return self._send(503, None, raw=b"<html>503</html>")
                if self.headers.get("Authorization") != f"Bearer {panel.token}":
                    return self._send(401, {"success": False})
                p = path[len(panel.base + "/panel/api/"):]

                def ok(obj=None):
                    return self._send(200, {"success": True, "msg": "", "obj": obj})

                def fail(msg):
                    return self._send(200, {"success": False, "msg": msg, "obj": None})

                if method == "GET" and p == "inbounds/list":
                    return ok(panel.inbounds)
                if method == "GET" and p == "server/status":
                    return ok({"cpu": 1, "mem": {"current": 1, "total": 4}, "xray": {"state": "running"}})
                if method == "GET" and p == "clients/list":
                    return ok([{"email": e} for e in panel.clients])
                if method == "POST" and p == "clients/add":
                    if panel.fail_add > 0:
                        panel.fail_add -= 1
                        return self._send(500, None, raw=b"internal error")
                    c = dict(body["client"])
                    c["uuid"] = f"uuid-{time.time_ns()}"
                    c["inboundIds"] = body["inboundIds"]
                    if c["email"] in panel.clients:
                        return fail("exists")
                    panel.clients[c["email"]] = c
                    return ok()
                if method == "GET" and p.startswith("clients/traffic/"):
                    email = unquote(p[len("clients/traffic/"):])
                    if panel.fail_traffic > 0:
                        panel.fail_traffic -= 1
                        return fail("database is locked")
                    c = panel.clients.get(email)
                    if not c:
                        return ok(None)
                    return ok({"email": email, "up": 0, "down": 0, "expiryTime": c["expiryTime"],
                               "subId": c["subId"], "uuid": c["uuid"], "enable": True})
                if method == "POST" and p.startswith("clients/del/"):
                    email = unquote(p[len("clients/del/"):])
                    panel.clients.pop(email, None)
                    panel.links.pop(email, None)
                    return ok()
                if method == "GET" and p.startswith("clients/get/subId/"):
                    sid = unquote(p[len("clients/get/subId/"):])
                    return ok([{"client": c, "externalLinks": panel.links.get(e, [])}
                               for e, c in panel.clients.items() if c["subId"] == sid])
                if method == "POST" and p.endswith("/externalLinks"):
                    email = unquote(p[len("clients/"):-len("/externalLinks")])
                    panel.links[email] = body["externalLinks"]
                    return ok()
                if method == "POST" and p.startswith("clients/ips/"):
                    return ok(panel.ips.get(unquote(p[len("clients/ips/"):]), []))
                return self._send(404, {"success": False})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def linked(self, email):
        return {link["value"] for link in self.links.get(email, [])}


_role_ids = iter(range(10_000, 99_999))


class Role:
    def __init__(self, name, id=None):
        self.name = name
        self.id = id if id is not None else next(_role_ids)
        self.mention = f"@{name}"
        self.members = []


class Guild:
    def __init__(self, id, members=(), roles=()):
        self.id = id
        self.name = f"guild{id}"
        self.chunked = True
        self.unavailable = False
        self.roles = [r if isinstance(r, Role) else Role(r) for r in roles]
        self._m = {}
        for m in members:
            self.add(m)

    def role(self, name):
        return next(r for r in self.roles if r.name == name)

    def add(self, m):
        m.guild = self
        self._m[m.id] = m
        return m

    def remove(self, m):
        self._m.pop(m.id, None)

    @property
    def members(self):
        return list(self._m.values())

    def get_member(self, i):
        return self._m.get(i)


class Member:
    def __init__(self, id, *roles, guild=None):
        self.id = id
        self.guild = guild
        self.roles = [self._role(r) for r in roles]
        self.bot = False
        self.dms = []
        self.display_name = f"user{id}"
        self.mention = f"<@{id}>"
        if guild is not None:
            guild.add(self)

    def _role(self, r):
        if isinstance(r, Role):
            return r
        if self.guild is not None:
            for role in self.guild.roles:
                if role.name == r:
                    return role
        return Role(r)

    def set_roles(self, *roles):
        self.roles = [self._role(r) for r in roles]

    def copy(self):
        m = Member.__new__(Member)
        m.__dict__.update(self.__dict__)
        m.roles = list(self.roles)
        return m

    async def send(self, text=None, file=None, view=None):
        self.dms.append(text)

    async def remove_roles(self, *roles, reason=None):
        ids = {r.id for r in roles}
        self.roles = [r for r in self.roles if r.id not in ids]

    def __str__(self):
        return f"user{self.id}"


class FakeClient:
    def __init__(self):
        self.guilds = []
        self.user = None

    async def fetch_user(self, i):
        import discord
        raise discord.NotFound.__new__(discord.NotFound)


class Interaction:
    def __init__(self, user, guild):
        self.user = user
        self.guild = guild
