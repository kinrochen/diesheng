from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase

from agent_group_chat.common import ident, token
from agent_group_chat.relay import Relay

BASE = "https://relay.example.test"


def temp_certificate(root):
    cert, key, conf = (root / name for name in ("cert.pem", "key.pem", "cert.cnf"))
    conf.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n"
                    "[ext]\nsubjectAltName=DNS:localhost,IP:192.0.2.12\nbasicConstraints=critical,CA:TRUE\n"
                    "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-config", str(conf), "-keyout", str(key), "-out", str(cert)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cert, key


class RelayCase(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1_800_000_000
        self.relay = Relay(self.root / "server", BASE, lambda: self.now)
        self.owner = self.relay.create_owner()
        self.g = self.relay.owner("create", {"name":"订单联调"}, self.owner)["group"]
        self.a, self.ak = self.member("前端")
        self.b, self.bk = self.member("后端")

    def invite(self, name, gid=None):
        return self.relay.owner("invite", {"group":gid or self.g, "name":name, "role":"接口联调"}, self.owner)

    def member(self, name, gid=None):
        key = token()
        invite = self.invite(name, gid)
        result = self.relay.channel("join", {"code":invite["code"], "type":"personal", "agent":"codex"}, key)
        return result["member"], key

    def call(self, key, action, sid="A", **data):
        return self.relay.project(action, {"session":sid, **data}, key)

    def ask(self, **more):
        return self.call(self.ak, "post", kind="question", request=ident(), target=self.b,
                         body="旧订单字段是否省略？", **more)["id"]

    def answer(self, q, sid="B", human=False):
        receipt = self.call(self.bk, "receive", sid, message=q, human=human)
        return self.call(self.bk, "post", sid, kind="answer", request="answer:"+str(q), parent=q,
                         ticket=receipt["ticket"], body="返回 null", evidence="release · order_detail_test", human=human)["id"]


    def next_delivery(self, key, sid="A"):
        return self.call(key, "next", sid)["delivery"]
