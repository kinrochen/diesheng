import argparse
import os
import sys
from pathlib import Path

from .common import Problem, private_dir
from .httpd import serve
from .relay import Relay


def main():
    directory = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "agent-group-chat-web"
    p = argparse.ArgumentParser(description="叠声 · 网页工作区与 Agent 消息服务")
    p.add_argument("--data-dir", type=Path, default=directory, help="群聊服务的私有数据目录")
    sub = p.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve", help="启动群聊服务与管理页面")
    server.add_argument("--port", type=int, default=7810)
    mode = server.add_mutually_exclusive_group()
    mode.add_argument("--lan", action="store_true", help="通过局域网共享，加上地址和证书可提供 HTTPS")
    mode.add_argument("--lan-ip", metavar="IPv4", help="选用本机指定内网网卡，共享 HTTP 邀请（默认自动识别）")
    mode.add_argument("--local-only", action="store_true", help="仅在本机回环地址预览")
    mode.add_argument("--behind-proxy", action="store_true", help="在代理的私有网络监听 HTTP，由代理提供 HTTPS")
    server.add_argument("--public-url", help="开发机可访问的 HTTPS 服务地址")
    server.add_argument("--cert", help="服务证书链 PEM")
    server.add_argument("--key", help="服务证书私钥 PEM")
    sub.add_parser("create-owner", help="生成独立的群主密钥，仅显示一次")
    args = p.parse_args()
    os.umask(0o077)
    try:
        relay = Relay(private_dir(args.data_dir) / "server", getattr(args, "public_url", None))
        if args.command == "create-owner":
            print(relay.create_owner())
        else:
            relay.first_owner(args.data_dir.expanduser() / "owner.key")
            serve(relay, args.port, args.lan, args.cert, args.key, args.behind_proxy,
                  lan_ip=args.lan_ip, local_only=args.local_only)
    except (Problem, OSError) as exc:
        sys.exit(str(exc))
