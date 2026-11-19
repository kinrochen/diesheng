import hashlib
import ipaddress
import os
import re
import secrets
import socket
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class Problem(Exception):
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def atomic(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def ident():
    return secrets.token_hex(16)


def token():
    return secrets.token_urlsafe(32)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def string(data, key, limit=100, *, optional=False):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise Problem(f"字段 {key} 格式有误，最多 {limit} 个字符")
    value = value.strip()
    if not optional and not value:
        raise Problem(f"请填写 {key}")
    return value


def integer(data, key, default=None, low=1):
    value = data.get(key, default)
    if type(value) is not int or value < low or value > 2**63 - 1:
        raise Problem(f"字段 {key} 需要有效的整数")
    return value


def boolean(data, key):
    value = data.get(key)
    if type(value) is not bool:
        raise Problem(f"请提供 {key} 的开关状态")
    return value


_PRIVATE = re.compile(
    r"(?:/(?:Users|home|private|Volumes|mnt|tmp|var|opt|etc|root|usr|workspaces)/|[A-Za-z]:[\\/]|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"Bearer\s+[A-Za-z0-9_.-]{16,}|(?:api[_-]?key|access[_-]?token)\s*[=:]\s*[^\s]{12,})",
    re.I,
)


def public_text(data, key, limit, *, optional=False):
    value = string(data, key, limit, optional=optional)
    if _PRIVATE.search(value):
        raise Problem("请移除本机绝对路径或凭据，依据可使用仓库相对路径、分支或测试名")
    return value


def private_dir(path):
    path = Path(path).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise Problem("请选择当前用户拥有的专用数据目录")
    # Never change permissions on a directory supplied by the operator.
    if path.stat().st_mode & 0o077:
        raise Problem(f"数据目录需要仅当前用户可访问，请检查 {path} 的权限（0700）")
    return path.resolve()


@contextmanager
def transaction(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(cur):
    return [dict(row) for row in cur]


_LAN_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_lan_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in _LAN_NETWORKS)


def lan_ipv4(preferred=None):
    if preferred is not None:
        if not is_lan_ipv4(preferred):
            raise Problem("--lan-ip 需要本机网卡的内网 IPv4 地址，例如 192.168.1.20")
        return preferred
    # A datagram connect selects the OS route without sending a packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
            if is_lan_ipv4(address):
                return address
    except OSError:
        pass
    try:
        candidates = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    for _, _, _, _, (address, _) in candidates:
        if is_lan_ipv4(address):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.bind((address, 0))
                return address
            except OSError:
                continue
    return None


def origin(value, *, resolve=False):
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if (url.scheme != "https" or not host or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in ("", "/") or port == 0
                or re.search(r"[\s\\%\x00-\x1f]", value)):
            raise ValueError
        host = host.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError
            try:
                # Also recognize legacy IPv4 spellings such as 127.1.
                ips = [ipaddress.ip_address(socket.inet_aton(host))]
            except OSError:
                ips = []
            if resolve and not ips:
                ips = [ipaddress.ip_address(addr[4][0]) for addr in socket.getaddrinfo(host, port or 443)]
        for addr in ips:
            addr = getattr(addr, "ipv4_mapped", None) or addr
            if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
                raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        return "https://" + netloc + (f":{port}" if port and port != 443 else "")
    except (TypeError, ValueError, OSError) as exc:
        raise Problem("请填写团队可以访问的 HTTPS 地址并检查 DNS。邀请需要可路由的主机名或 IP 地址。") from exc
