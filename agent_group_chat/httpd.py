import json
import re
import ssl
import socket
import threading
import time
from contextlib import ExitStack
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlsplit

from .common import Problem, integer, lan_ipv4, origin, string, token
from .relay import BROWSER_LIFETIME

ASSETS = files("agent_group_chat").joinpath("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/clipboard.js": ("clipboard.js", "text/javascript; charset=utf-8"),
         "/paper-weave.webp": ("paper-weave.webp", "image/webp"),
         "/codex.webp": ("codex.webp", "image/webp"),
         "/claude-code.webp": ("claude-code.webp", "image/webp"),
         "/deepseek-harness.webp": ("deepseek-harness.webp", "image/webp"),
         "/join.js": ("join.js", "text/javascript; charset=utf-8"),
         "/agent.md": ("agent.md", "text/markdown; charset=utf-8"),
         "/codex-listen.py": ("codex-listen.py", "text/plain; charset=utf-8"),
         "/project-memory.md": ("project-memory.md", "text/markdown; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8"),
         "/favicon.ico": (None, "image/x-icon")}


class DeliveryNotifications:
    def __init__(self, relay):
        self.relay = relay
        self.changes = threading.Condition()
        self.generation = 0
        self.listeners = threading.BoundedSemaphore(64)
        self.browsers = threading.BoundedSemaphore(64)

    def changed(self):
        with self.changes:
            self.generation += 1
            self.changes.notify_all()

    def wait_for_delivery(self, data, key):
        self.relay.project("access", {}, key)
        result = self._wait(data, self.listeners, lambda: self.relay.project("next", data, key),
                            lambda r: r["delivery"] or r["paused"])
        if result["delivery"]:
            self.changed()
        return result

    def browser_request(self, action, data, read):
        if action != "changes":
            return read()
        since = integer(data, "since", low=0)
        return self._wait(data, self.browsers, read, lambda r: r["revision"] != since)

    def _wait(self, data, slots, read, ready):
        timeout = min(25, integer(data, "timeout", 20, low=0))
        deadline = time.monotonic() + timeout
        if not slots.acquire(blocking=False):
            raise Problem("当前监听人数较多，请稍后重试", 429)
        try:
            while True:
                with self.changes:
                    observed = self.generation
                result = read()
                remaining = deadline - time.monotonic()
                if ready(result) or remaining <= 0:
                    return result
                with self.changes:
                    # Recheck periodically for writes from another serving process.
                    self.changes.wait_for(lambda: self.generation != observed, timeout=min(5, remaining))
        finally:
            slots.release()


class RelayHTTP(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, relay, tls=None, *, behind_proxy=False, deliveries=None):
        self.relay = relay
        self.secure = tls is not None or behind_proxy
        self.external_origin = relay.public_url if behind_proxy else None
        self.tls = tls
        self.deliveries = deliveries or DeliveryNotifications(relay)
        super().__init__(address, Handler)

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(15)
        if self.tls:
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr

    def finish_request(self, request, client_address):
        if self.tls:
            try:
                request.do_handshake()
            except (ssl.SSLError, TimeoutError, ConnectionResetError):
                return
        super().finish_request(request, client_address)


class DualStackRelayHTTP(RelayHTTP):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def lan_server(port, relay, context):
    if socket.has_dualstack_ipv6():
        return DualStackRelayHTTP(("::", port), relay, context)
    return RelayHTTP(("0.0.0.0", port), relay, context)


class Handler(BaseHTTPRequestHandler):
    server_version = "ProjectRelay"
    sys_version = ""

    def log_message(self, *_args):
        pass  # Requests may carry invitations. Do not log URLs, headers, or bodies.

    def send_payload(self, status, body=b"", mime="application/json; charset=utf-8", cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_payload(200, b'{"status":"ok"}')
        elif re.fullmatch(r"/room/[0-9a-f]{32}/?", path):
            self.send_payload(200, (ASSETS / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/join", "/join/"):
            html = "text/html" in self.headers.get("Accept", "")
            filename, mime = ("join.html", "text/html") if html else ("agent.md", "text/markdown")
            self.send_payload(200, (ASSETS / filename).read_bytes(), mime + "; charset=utf-8")
        elif path in FILES:
            filename, mime = FILES[path]
            self.send_payload(200 if filename else 204, (ASSETS / filename).read_bytes() if filename else b"", mime)
        else:
            self.send_payload(404, b'{"error":"Not found"}')

    def do_POST(self):
        try:
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise Problem("请求缺少长度", 411)
            if not 0 <= size <= 128 * 1024:
                raise Problem("请求内容过大", 413)
            body = self.rfile.read(size)
            source = self.headers.get("Origin")
            scheme = "https" if self.server.secure else "http"
            expected = self.server.external_origin or f"{scheme}://{self.headers.get('Host')}"
            if source and source != expected:
                raise Problem("此操作需要从当前服务页面发起", 403)
            if self.headers.get_content_type() != "application/json":
                raise Problem("请使用 JSON 请求", 415)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise Problem("请求需要 JSON 对象")
            parts = self.path.strip("/").split("/")
            member_route = (len(parts) == 4 and parts[:2] == ["api", "member"]
                            and re.fullmatch(r"[0-9a-f]{32}", parts[2]))
            if not member_route and (len(parts) != 3 or parts[0] != "api"):
                raise Problem("未知接口", 404)
            scope, action = parts[1], parts[-1]
            relay = self.server.relay
            header = self.headers.get("Authorization", "")
            key = header[7:] if header.startswith("Bearer ") else ""
            cookie = None
            if member_route:
                gid = parts[2]
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                session = jar["diesheng_member"].value if "diesheng_member" in jar else ""
                if action == "preview" and not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", session):
                    session = token()
                result = self.server.deliveries.browser_request(action, data, lambda: relay.web_member(action, gid, data, session))
                suffix = f"; HttpOnly; SameSite=Strict; Path=/api/member/{gid}/" + ("; Secure" if self.server.secure else "")
                if action in ("preview", "join"):
                    cookie = f"diesheng_member={session}; Max-Age={BROWSER_LIFETIME}{suffix}"
                elif action == "leave":
                    cookie = "diesheng_member=; Max-Age=0" + suffix
            elif scope == "owner":
                suffix = "; HttpOnly; SameSite=Strict; Path=/api/owner" + ("; Secure" if self.server.secure else "")
                if action == "login":
                    session = relay.login(string(data, "key", 150))
                    cookie = f"relay_owner={session}; Max-Age=28800{suffix}"
                    result = {}
                else:
                    jar = SimpleCookie(self.headers.get("Cookie", ""))
                    session = jar["relay_owner"].value if "relay_owner" in jar else ""
                    result = self.server.deliveries.browser_request(action, data, lambda: relay.owner(action, data, key or session))
                    if action == "logout":
                        cookie = "relay_owner=; Max-Age=0" + suffix
            elif scope == "channel":
                result = relay.channel(action, data, key)
            elif scope == "project":
                result = self.server.deliveries.wait_for_delivery(data, key) if action == "wait" else relay.project(action, data, key)
            else:
                raise Problem("未知接口", 404)
            if action in ("speak", "post", "receive", "accept", "fail", "requeue", "pause", "project_pause", "remove", "leave"):
                self.server.deliveries.changed()
            self.send_payload(200, json.dumps(result, ensure_ascii=False).encode(), cookie=cookie)
        except Problem as exc:
            self.send_payload(exc.status, json.dumps({"error": str(exc), "code": exc.code}, ensure_ascii=False).encode())
        except (ValueError, UnicodeError, CookieError, RecursionError):
            self.send_payload(400, b'{"error":"Invalid JSON or headers"}')
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            self.send_payload(HTTPStatus.INTERNAL_SERVER_ERROR, '{"error":"服务暂不可用，请重试"}'.encode())


def lan_context(public_url, cert, key):
    origin(public_url, resolve=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    def encrypted_key():
        raise Problem("请选择服务专用的无口令 PEM 私钥，并限制文件访问权限")

    context.load_cert_chain(cert, key, password=encrypted_key)
    return context


def shared_http_server(stack, relay, address, port):
    team = stack.enter_context(RelayHTTP((address, port), relay))
    port = team.server_port
    local = stack.enter_context(RelayHTTP(("127.0.0.1", port), relay, deliveries=team.deliveries))
    relay.public_url = f"http://{address}" + (f":{port}" if port != 80 else "")
    threading.Thread(target=local.serve_forever, daemon=True).start()
    stack.callback(local.shutdown)
    return team


def serve(relay, port, lan, cert, key, behind_proxy=False, *, lan_ip=None, local_only=False):
    with ExitStack() as stack:
        if behind_proxy:
            if not relay.public_url or cert or key:
                raise Problem("代理模式需要 --public-url，TLS 证书由代理配置")
            server = stack.enter_context(RelayHTTP(("0.0.0.0", port), relay, behind_proxy=True))
        elif relay.public_url or cert or key:
            if not (lan and relay.public_url and cert and key):
                raise Problem("直接提供 HTTPS 需要 --lan、--public-url、--cert 和 --key")
            server = stack.enter_context(lan_server(port, relay, lan_context(relay.public_url, cert, key)))
        else:
            address = None if local_only else lan_ipv4(lan_ip)
            if address:
                server = shared_http_server(stack, relay, address, port)
            elif lan:
                raise Problem("未发现内网 IPv4 地址，请连接局域网或用 --lan-ip 指定本机地址")
            else:
                server = stack.enter_context(RelayHTTP(("127.0.0.1", port), relay))
                print("当前仅本机可用。连接局域网后重启服务即可分享邀请。", flush=True)
        stop = threading.Event()

        def cleanup():
            while not stop.wait(300):
                relay.prune()

        worker = threading.Thread(target=cleanup, daemon=True)
        worker.start()
        print(f"叠声已启动　{relay.public_url or f'http://127.0.0.1:{server.server_port}'}", flush=True)
        try:
            server.serve_forever()
        finally:
            stop.set()
