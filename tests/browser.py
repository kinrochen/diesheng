"""Run the owner UI in an isolated Chromium profile against a temporary loopback relay."""
import argparse
import os
from contextlib import ExitStack
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading

from agent_group_chat.common import lan_ipv4
from agent_group_chat.httpd import RelayHTTP, shared_http_server
from agent_group_chat.relay import Relay


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--playwright",default="playwright")
    parser.add_argument("--chromium",default="")
    parser.add_argument("--screenshots",default="")
    parser.add_argument("--lan",action="store_true",help="用回环地址管理频道，通过真实局域网网卡访问邀请")
    args=parser.parse_args()
    with TemporaryDirectory() as temp:
        relay=Relay(Path(temp)/"server")
        with ExitStack() as stack:
            if args.lan:
                address=lan_ipv4()
                if not address:
                    raise RuntimeError("测试需要一张有内网 IPv4 地址的网卡")
                server=shared_http_server(stack,relay,address,0)
            else:
                server=stack.enter_context(RelayHTTP(("127.0.0.1",0),relay))
            owner_url=f"http://127.0.0.1:{server.server_port}"
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                subprocess.run(["node",str(Path(__file__).with_name("browser.cjs"))],check=True,env={
                    **os.environ,"RELAY_TEST_URL":owner_url,"RELAY_TEST_SHARE_URL":relay.public_url or owner_url,
                    "RELAY_TEST_OWNER":relay.create_owner(),"RELAY_TEST_PLAYWRIGHT":args.playwright,
                    "RELAY_TEST_CHROMIUM":args.chromium,"RELAY_TEST_SHOTS":args.screenshots})
            finally: server.shutdown()


if __name__=="__main__":
    main()
