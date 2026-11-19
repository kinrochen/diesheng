"""Install or update a private Linux relay for the current user."""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

PACKAGE = Path(__file__).resolve().with_name("relay.pyz")
sys.path.insert(0, str(PACKAGE))
from agent_group_chat.common import Problem, atomic, ident, origin, private_dir
from agent_group_chat.httpd import lan_context
from agent_group_chat.relay import Relay


def openssl(*args):
    result = subprocess.run(["openssl", *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise Problem("OpenSSL 未能生成证书：" + result.stderr.strip()[-500:])


def certificates(root, host, previous):
    # Publish the CA and each matching leaf/key pair together. Keep the CA on repeat installs.
    tls = root / "tls"
    if not tls.exists():
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            stage = Path(tmp) / "authority"
            stage.mkdir(mode=0o700)
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                    "-subj", "/CN=Diesheng Private CA", "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
                    "-keyout", stage / "ca.key", "-out", stage / "ca.pem")
            stage.rename(tls)
    if previous.get("managed") and urlsplit(previous["url"]).hostname == host:
        cert, key = Path(previous["cert"]), Path(previous["key"])
        if cert.is_file() and key.is_file() and subprocess.run(
                ["openssl", "x509", "-in", cert, "-noout", "-checkend", str(30 * 86400)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return cert, key
    try:
        san = "IP:" + str(ipaddress.ip_address(host))
    except ValueError:
        san = "DNS:" + host
    with tempfile.TemporaryDirectory(dir=tls) as tmp:
        stage = Path(tmp)
        pair = stage / "pair"
        pair.mkdir(mode=0o700)
        ext = stage / "extensions.cnf"
        ext.write_text("[server]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                       "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n"
                       "subjectAltName=" + san + "\n")
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Diesheng Relay",
                "-keyout", pair / "privkey.pem", "-out", stage / "request.pem")
        openssl("x509", "-req", "-in", stage / "request.pem", "-CA", tls / "ca.pem", "-CAkey", tls / "ca.key",
                "-set_serial", "0x" + secrets.token_hex(16), "-days", "365", "-sha256",
                "-extfile", ext, "-extensions", "server", "-out", pair / "server.pem")
        published = tls / ("server-" + ident())
        pair.rename(published)
    return published / "server.pem", published / "privkey.pem"


def setup(root, url, cert, key):
    if bool(cert) != bool(key):
        raise Problem("--cert 和 --key 需要同时提供")
    config = root / "hosting.json"
    parsed = urlsplit(url)
    with (root / "hosting.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = json.loads(config.read_text()) if config.is_file() else {}
        managed = True
        if not cert and old and not old["managed"]:
            cert, key = old["cert"], old["key"]
        if cert:
            cert, key = Path(cert).expanduser().resolve(strict=True), Path(key).expanduser().resolve(strict=True)
            managed = False
        else:
            cert, key = certificates(root, parsed.hostname, old)
        lan_context(url, str(cert), str(key))
        # Check the deployed name even for a certificate supplied by the operator.
        try:
            ipaddress.ip_address(parsed.hostname)
            flag = "-verify_ip"
        except ValueError:
            flag = "-verify_hostname"
        match = subprocess.run(["openssl", "verify", "-partial_chain", "-trusted", cert, "-purpose", "sslserver",
                                flag, parsed.hostname, cert], capture_output=True, text=True)
        if match.returncode:
            raise Problem("请检查服务证书有效期和 SAN，域名或 IP 须匹配服务器地址")
        Relay(root / "server").first_owner(root / "owner.key")
        installed = root / "relay.pyz"
        atomic(installed, PACKAGE.read_bytes())
        atomic(config, json.dumps({"url": url, "cert": str(cert), "key": str(key), "managed": managed}).encode())
    return [sys.executable, "-I", str(installed), "--data-dir", str(root), "serve", "--lan", "--port",
            str(parsed.port or 443), "--public-url", url, "--cert", str(cert), "--key", str(key)], managed


def install_unit(command):
    folder = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"
    folder.mkdir(parents=True, exist_ok=True)
    unit = folder / "agent-group-chat.service"
    marker = "# Managed by agent-group-chat\n"
    if unit.exists() and not unit.read_text().startswith(marker):
        raise Problem(f"已有自定义服务文件 {unit}，请保留并手动更新")
    def argument(value):
        if "\n" in value or "\r" in value:
            raise Problem("安装路径不能包含换行")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
    text = (marker + "[Unit]\nDescription=叠声 Agent 群聊服务\nAfter=network.target\n\n"
            "[Service]\nExecStart=" + " ".join(map(argument, command)) +
            "\nRestart=on-failure\nRestartSec=5\nUMask=0077\nNoNewPrivileges=true\n\n[Install]\nWantedBy=default.target\n")
    atomic(unit, text.encode())
    for step in (["daemon-reload"], ["enable", unit.name], ["restart", unit.name]):
        subprocess.run(["systemctl", "--user", *step], check=True)


def main():
    parser = argparse.ArgumentParser(description="部署叠声 Linux 服务器")
    parser.add_argument("url", help="团队可访问的 HTTPS 地址，例如 https://192.168.1.50:7810")
    parser.add_argument("--foreground", action="store_true", help="在前台启动，适合容器或无 systemd 环境")
    parser.add_argument("--cert", help="已有服务证书链 PEM")
    parser.add_argument("--key", help="配套服务私钥 PEM")
    default = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "agent-group-chat-web"
    parser.add_argument("--data-dir", type=Path, default=default)
    args = parser.parse_args()
    os.umask(0o077)
    url = origin(args.url, resolve=True)
    root = private_dir(args.data_dir)
    command, managed = setup(root, url, args.cert, args.key)
    print("服务器地址", url)
    if (root / "owner.key").exists():
        print("群主密钥文件", root / "owner.key")
    else:
        print("使用这个数据目录原有的群主密钥。")
    if managed:
        print("团队浏览器与 Agent 可信任的公开 CA 文件", root / "tls/ca.pem")
    sys.stdout.flush()
    if args.foreground:
        os.execv(command[0], command)
    install_unit(command)
    print("已启动用户服务。查看状态：systemctl --user status agent-group-chat")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, Problem, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
