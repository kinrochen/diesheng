import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import re
import select
import ssl
import subprocess
import sys
import threading
import time
from uuid import uuid4

from agent_group_chat.common import digest, token, transaction
from agent_group_chat.httpd import RelayHTTP
from agent_group_chat.relay import BROWSER_LIFETIME, RECOVERY_LINK_LIFETIME, RECOVERY_RETRY, RESPONSE_TIMEOUT
from tests.support import RelayCase, temp_certificate


class HTTPS(RelayCase):
    def setUp(self):
        super().setUp()
        cert, key = temp_certificate(self.root)
        self.client_tls = ssl.create_default_context(cafile=cert)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        self.server = RelayHTTP(("127.0.0.1", 0), self.relay, tls)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":.02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, **headers):
        conn = http.client.HTTPSConnection("localhost",self.server.server_port,context=self.client_tls,timeout=5)
        try:
            conn.request("GET" if data is None else "POST", path, None if data is None else json.dumps(data),
                         {"Content-Type":"application/json", **headers})
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def test_web_routes_and_sessions(self):
        self.assertEqual(401,self.request("/api/owner/groups",{})[0])
        status,headers,_=self.request("/api/owner/login",{"key":self.owner})
        self.assertEqual(200,status)
        cookie=headers["Set-Cookie"]
        self.assertIn("HttpOnly",cookie)
        self.assertIn("Secure",cookie)
        auth={"Cookie":cookie.split(";")[0]}
        self.assertEqual(403,self.request("/api/owner/create",{"name":"其他"},Origin="https://elsewhere",**auth)[0])
        self.assertEqual(200,self.request("/api/owner/groups",{},**auth)[0])
        status,headers,guide=self.request("/join")
        self.assertEqual(200,status)
        self.assertIn("text/markdown",headers["Content-Type"])
        self.assertIn(b"/api/channel/join",guide)
        self.assertIn(b"join.js",self.request("/join",Accept="text/html")[2])
        self.assertIn(b"/api/project/wait",self.request("/agent.md")[2])
        self.request("/api/owner/logout",{},**auth)
        self.assertEqual(401,self.request("/api/owner/groups",{},**auth)[0])

    def test_markdown_assets_are_served_locally(self):
        index=self.request("/")[2]
        for name in ("vendor-marked.js", "vendor-purify.js", "markdown.js"):
            with self.subTest(asset=name):
                status, headers, body=self.request("/"+name)
                self.assertEqual(200,status)
                self.assertIn("text/javascript",headers["Content-Type"])
                self.assertTrue(body)
                self.assertIn(('src="/'+name+'"').encode(),index)
        self.assertLess(index.index(b"/vendor-marked.js"),index.index(b"/markdown.js"))
        self.assertLess(index.index(b"/markdown.js"),index.index(b"/app.js"))

    def test_browser_inbox_matches_counts_and_public_messages(self):
        group = {"group": self.g}
        owner = {"Authorization": "Bearer "+self.owner}
        _, colleague = self.legacy_colleague(self.g, "协作同事")

        def inbox(status, **filters):
            code, _, raw = self.request("/api/owner/inbox", {**group,"status":status,**filters}, **owner)
            self.assertEqual(200, code, raw)
            return json.loads(raw)

        question = self.ask()
        self.assertEqual(question, self.next_delivery(self.bk, "B")["id"])
        root = self.relay.owner("group_question", {**group,"targets":[self.a,self.b],
                                "body":"请分别确认发布安排","request":"release"}, self.owner)["id"]
        branch = self.next_delivery(self.ak, "A")["id"]
        latest = self.ask()
        other = self.relay.owner("create", {"name":"其他频道"}, self.owner)["group"]
        other_recipient, _ = self.member("群外接收方", other)
        self.relay.owner("speak", {"group":other,"target":other_recipient,
                         "body":"其他频道的请求","request":"unrelated"}, self.owner)

        page = inbox("pending", limit=1)
        self.assertTrue(page["more"])
        self.assertEqual([latest], [item["id"] for item in page["items"]])
        other_branch, = inbox("pending", before=latest)["items"]
        self.assertEqual((root,root,"后端","pending"),
                         (other_branch["topic"],other_branch["message"],other_branch["recipient"],other_branch["receipt"]))
        processing = inbox("processing")["items"]
        self.assertEqual([(branch,root),(question,question)], [(item["id"],item["message"]) for item in processing])
        self.assertTrue(all("ticket" not in item and "sid" not in item for item in processing))

        reply = self.answer(question, "B")
        pending = inbox("pending")["items"]
        self.assertEqual((reply,"answer","pending"), (pending[0]["id"],pending[0]["kind"],pending[0]["receipt"]))
        processing = inbox("processing")["items"]
        self.assertEqual([branch], [item["id"] for item in processing])
        members = self.relay.owner("detail", group, self.owner)["members"]
        self.assertEqual((len(pending),len(processing)), tuple(sum(m[key] for m in members) for key in ("pending","processing")))

        path = f"/api/member/{self.g}/inbox"
        code, _, raw = self.request(path, {"status":"processing"}, **colleague)
        self.assertEqual(200, code, raw)
        self.assertEqual(processing, json.loads(raw)["items"])
        self.assertEqual(403, self.request(path, {"group":other,"status":"pending"}, **colleague)[0])
        self.assertEqual(401, self.request(path, {"status":"pending"})[0])

        self.now += RESPONSE_TIMEOUT + 1
        self.assertEqual([], inbox("processing")["items"])
        timed_out = next(item for item in inbox("pending")["items"] if item["id"] == branch)
        self.assertEqual("uncertain", timed_out["receipt"])

    def test_shared_link_three_clients_and_wait(self):
        cookie=self.request("/api/owner/login",{"key":self.owner})[1]["Set-Cookie"].split(";")[0]
        def rpc(path, body, key=None, owner=False):
            headers={"Cookie":cookie} if owner else ({"Authorization":"Bearer "+key} if key else {})
            code, _, raw=self.request("/api/"+path,body,**headers)
            self.assertEqual(200,code,raw)
            return json.loads(raw)
        code=rpc("owner/channel_link",{"group":self.g},owner=True)["url"].split("#c=")[1]
        members={}
        for client in ("codex","claude","deepseek"):
            key=token()
            member=rpc("channel/join",{"code":code,"agent":client},key)
            members[client]=key,member
            info=rpc("project/status",{"session":client},key)
            rpc("project/check",{"session":client,"challenge":info["challenge"]},key)
        sender, receiver=members["codex"][0],members["claude"][0]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            call=pool.submit(rpc,"project/wait",{"session":"claude","timeout":4},receiver)
            time.sleep(.1)
            message=rpc("project/post",{"session":"codex","kind":"question","target":members["claude"][1]["member"],
                                        "body":"可以联调吗？","request":"q"},sender)
            delivered=call.result(3)["delivery"]
        self.assertEqual(message["id"],delivered["id"])
        answer=rpc("project/post",{"session":"claude","kind":"answer","parent":delivered["id"],"ticket":delivered["ticket"],
                                   "body":"可以，分支已就绪。","request":"a"},receiver)
        self.assertEqual(answer["id"],rpc("project/wait",{"session":"codex","timeout":0},sender)["delivery"]["id"])
        self.assertIsNone(rpc("project/wait",{"session":"d","timeout":0},members["deepseek"][0])["delivery"])
        rpc("owner/disable_link",{"group":self.g},owner=True)
        self.assertEqual(410,self.request("/api/channel/preview",{"code":code})[0])
        self.assertEqual(403,self.request("/api/project/wait",{"session":"d","timeout":0},Authorization="Bearer "+code)[0])

    def test_plaintext_key_formats_are_delivered_as_channel_messages(self):
        body = "\n".join((
            "Authorization: Bearer fixture-token-1234567890",
            "api_key: sk-fixture-1234567890abcdef",
            "access_token=fixture-access-token-1234",
            "-----BEGIN PRIVATE KEY-----",
            "fixture-only",
            "-----END PRIVATE KEY-----",
        ))
        requests = (
            ("/api/owner/speak", {"group":self.g, "target":self.b}, self.owner),
            ("/api/project/post", {"session":"sender", "kind":"question", "target":self.b}, self.ak),
        )
        for path, data, key in requests:
            with self.subTest(path=path):
                status, _, raw = self.request(path, {**data, "body":body, "request":uuid4().hex},
                                              Authorization="Bearer "+key)
                self.assertEqual(200, status, raw)
                message = json.loads(raw)["id"]
                status, _, raw = self.request("/api/project/receive", {"session":"reader", "message":message},
                                              Authorization="Bearer "+self.bk)
                self.assertEqual(200, status, raw)
                self.assertEqual(body, json.loads(raw)["body"])

    def test_announcement_wakes_agents_and_requires_per_member_acknowledgement(self):
        owner = {"Authorization":"Bearer "+self.owner}
        _, colleague = self.legacy_colleague(self.g, "小雨")
        room = "/api/member/"+self.g+"/"
        publish = "/api/owner/announcement-publish"
        first = {"group":self.g, "version":0, "body":"## 群规则\n\n- 先核对变更范围"}
        self.assertEqual(401, self.request(publish, first)[0])
        self.assertEqual(404, self.request(publish, first, Authorization="Bearer "+self.relay.create_owner())[0])
        self.assertEqual(403, self.request(room+"announcement-publish", first, **colleague)[0])
        self.assertIsNone(self.call(self.ak, "context")["announcement"])
        since = json.loads(self.request(room+"detail", {}, **colleague)[2])["revision"]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            agent_polls = [pool.submit(self.request, "/api/project/wait",
                                      {"session":"listener","announcements":"AGENTS.md","timeout":4},
                                      Authorization="Bearer "+key) for key in (self.ak,self.bk)]
            browser_poll = pool.submit(self.request, room+"changes", {"since":since,"timeout":4}, **colleague)
            time.sleep(.1)
            status, _, raw = self.request(publish, first, **owner)
            self.assertEqual(200, status, raw)
            version_one = json.loads(raw)["announcement"]
            self.assertEqual(1, version_one["version"])
            for member, poll in zip((self.a,self.b), agent_polls):
                status, _, raw = poll.result(timeout=3)
                self.assertEqual(200, status, raw)
                data = json.loads(raw)
                self.assertIsNone(data["delivery"])
                self.assertEqual({"group":self.g, "member":member, "group_name":"订单联调",
                                  "kind":"announcement_updated", "file":"AGENTS.md", **version_one}, data["notice"])
            self.assertEqual(since+1, json.loads(browser_poll.result(timeout=3)[2])["revision"])
        self.assertEqual(version_one, json.loads(self.request(room+"detail", {}, **colleague)[2])["announcement"])
        self.assertEqual(version_one, json.loads(self.request(publish, first, **owner)[2])["announcement"])
        second = {**first, "version":1, "body":"- 每次发布附上验收记录"}
        self.assertEqual(409, self.request(publish, {**second,"version":0}, **owner)[0])
        self.assertEqual(2, json.loads(self.request(publish, second, **owner)[2])["announcement"]["version"])
        self.assertNotIn("notice", self.call(self.bk, "next"))
        self.assertEqual(1, self.call(self.bk, "context", announcements="AGENTS.md")["notice"]["version"])
        auth = {"Authorization":"Bearer "+self.ak}
        self.assertEqual(409, self.request("/api/project/announcement-ack",
                                         {"session":"early","version":2,"file":"AGENTS.md"}, **auth)[0])
        for _ in range(2):
            self.assertEqual(2, self.call(self.ak, "announcement-ack", version=1, file="AGENTS.md")["notice"]["version"])
        details = json.loads(self.request(room+"detail", {}, **colleague)[2])
        self.assertEqual([], details["announcement_receipts"])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            poll = pool.submit(self.request, room+"changes", {"since":details["revision"],"timeout":4}, **colleague)
            time.sleep(.1)
            status, _, raw = self.request("/api/project/announcement-ack",
                                          {"session":"writer","version":2,"file":"AGENTS.md"}, **auth)
            self.assertEqual(200, status, raw)
            self.assertIsNone(json.loads(raw)["notice"])
            self.assertGreater(json.loads(poll.result(timeout=3)[2])["revision"], details["revision"])
        self.assertEqual([self.a], json.loads(self.request(room+"detail", {}, **colleague)[2])["announcement_receipts"])
        self.assertEqual(1, self.call(self.bk, "next", announcements="AGENTS.md")["notice"]["version"])
        newcomer, key = self.member("新同伴")
        self.assertEqual(2, self.call(key, "context", announcements="AGENTS.md")["notice"]["version"])
        self.assertIsNone(self.call(key, "announcement-ack", version=2, file="AGENTS.md")["notice"])
        self.relay.owner("pause", {"group":self.g, "paused":True}, self.owner)
        self.assertEqual(200, self.request(publish, {**second,"version":2,"body":""}, **owner)[0])
        self.assertEqual([], json.loads(self.request(room+"detail", {}, **colleague)[2])["announcement_receipts"])
        cleared = self.call(key, "next", announcements="AGENTS.md")
        self.assertTrue(cleared["paused"])
        self.assertEqual((3,""), (cleared["notice"]["version"],cleared["notice"]["body"]))
        self.relay.owner("remove", {"group":self.g, "member":newcomer}, self.owner)
        self.assertEqual("membership_ended", self.call(key, "context", lifecycle=True, announcements="AGENTS.md")["notice"]["kind"])
        self.assertEqual(403, self.request("/api/project/announcement-ack", {"session":"gone","version":3,"file":"AGENTS.md"},
                                         Authorization="Bearer "+key)[0])

    def test_only_owner_can_delegate_and_revoke_channel_management(self):
        owner = {"Authorization":"Bearer "+self.owner}
        room = "/api/member/"+self.g+"/"

        def invite(group):
            return self.relay.owner("human_invite", {"group":group}, self.owner)["url"].split("#invite=")[1]

        status, headers, raw = self.request(room+"register", {"code":invite(self.g), "name":"值班同事",
                                      "username":"oncall", "password":"test-password"})
        self.assertEqual(200, status, raw)
        member = json.loads(raw)["member"]
        colleague = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        other = self.relay.owner("create", {"name":"另一个频道"}, self.owner)["group"]
        other_room = "/api/member/"+other+"/"
        status, _, raw = self.request(other_room+"join", {"code":invite(other), "name":"值班同事"}, **colleague)
        self.assertEqual(200, status, raw)
        other_member = json.loads(raw)["member"]
        _, peer = self.legacy_colleague(self.g, "旧同事")
        role = {"group":self.g, "member":member, "admin":True}
        role_path = "/api/owner/admin-role"
        before = json.loads(self.request(room+"detail", {}, **colleague)[2])
        self.assertFalse(before["can_manage"])
        self.assertNotIn("invites", before)
        self.assertEqual(403, self.request(room+"channel_link", {}, **colleague)[0])
        self.assertEqual(404, self.request(role_path, {**role, "member":self.b}, **owner)[0])
        self.assertEqual(404, self.request(role_path, {**role, "member":other_member}, **owner)[0])
        self.assertEqual(200, self.request(role_path, role, **owner)[0])
        detail = json.loads(self.request(room+"detail", {}, **colleague)[2])
        self.assertTrue(detail["can_manage"])
        self.assertGreater(detail["revision"], before["revision"])
        self.assertTrue(next(m for m in detail["members"] if m["id"]==member)["admin"])
        self.assertIn("invites", detail)
        self.assertEqual(403, self.request(room+"admin-role", {**role, "admin":False}, **colleague)[0])
        status, _, raw = self.request(room+"dissolve", {}, **colleague)
        self.assertEqual(403, status)
        self.assertEqual("只有群主可以解散频道", json.loads(raw)["error"])
        self.assertEqual(401, self.request(role_path, role, **colleague)[0])
        self.assertEqual(403, self.request(room+"channel_link", {"group":other}, **colleague)[0])
        self.assertEqual(403, self.request(other_room+"channel_link", {}, **colleague)[0])
        self.assertEqual(403, self.request(room+"pause", {"paused":True}, **peer)[0])
        self.assertEqual(404, self.request(room+"create", {"name":"没有权限"}, **colleague)[0])

        def manage(action, **data):
            status, _, raw = self.request(room+action, data, **colleague)
            self.assertEqual(200, status, raw)
            return json.loads(raw)

        announcement = manage("announcement-publish", body="发布前核对记录。", version=0)
        self.assertEqual(announcement["announcement"], self.call(self.ak, "context")["announcement"])
        self.assertEqual(manage("channel_link"), self.relay.owner("channel_link", {"group":self.g}, self.owner))
        manage("disable_link")
        self.assertEqual(200, self.request("/api/channel/preview", {"code":manage("channel_link")["url"].split("#c=")[1]})[0])
        for op, fields in (("invite", {"name":"候补项目"}), ("human_invite", {})):
            manage("revoke", invite=manage(op, **fields)["id"])
        manage("profile", member=self.b, name="后端值班", role="核对接口变更")
        message = self.ask()
        ticket = self.call(self.bk, "receive", "check", message=message)["ticket"]
        self.call(self.bk, "fail", "check", message=message, ticket=ticket, reason="稍后重新处理")
        manage("requeue", message=message)
        manage("pause", paused=True)
        manage("pause", paused=False)
        host = next(m for m in detail["members"] if m["agent"]=="host")
        self.assertEqual(404, self.request(room+"remove", {"member":host["id"]}, **colleague)[0])
        manage("remove", member=self.b)
        self.assertEqual(200, self.request(role_path, {**role,"admin":False}, **owner)[0])
        self.assertEqual(403, self.request(room+"dissolve", {}, **colleague)[0])
        self.assertFalse(json.loads(self.request(room+"detail", {}, **colleague)[2])["can_manage"])
        self.assertEqual(200, self.request(role_path, role, **owner)[0])
        self.assertEqual(200, self.request("/api/owner/dissolve", {"group":self.g}, **owner)[0])
        self.assertEqual(410, self.request(room+"detail", {}, **colleague)[0])
        self.assertEqual(200, self.request(other_room+"detail", {}, **colleague)[0])

    def test_codex_wakes_on_questions_and_deduplicates_across_restart(self):
        status, headers, source = self.request("/codex-listen.py")
        self.assertEqual(200, status)
        self.assertIn("text/plain", headers["Content-Type"])
        script = self.root / "listener.py"
        script.write_bytes(source)
        record = self.root / "channel.json"
        record.write_text(json.dumps({"base": f"https://localhost:{self.server.server_port}", "group": self.g,
                                      "member": self.b, "agent": "codex"}))
        (self.root / "auth.header").write_text("Authorization: Bearer " + self.bk)
        calls, cli = self.root / "native-events", self.root / "codex"
        cli.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                       "if '--help' not in sys.argv:\n"
                       "    with open(os.environ['TEST_NATIVE_EVENTS'], 'a') as out:\n"
                       "        print(json.dumps(sys.argv[1:]), file=out)\n")
        cli.chmod(0o700)
        thread = str(uuid4())
        def start():
            proc = subprocess.Popen([sys.executable, str(script), "--binding", str(record),
                                     "--thread", thread, "--codex-bin", str(cli)], stdout=subprocess.PIPE,
                                    env={**os.environ, "SSL_CERT_FILE": str(self.root / "cert.pem"),
                                         "TEST_NATIVE_EVENTS": str(calls)})
            self.addCleanup(stop, proc)
            self.assertTrue(select.select([proc.stdout], [], [], 4)[0], "监听器未启动")
            self.assertIn("已开始", proc.stdout.readline().decode())
            return proc
        def stop(proc):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=4)
            proc.stdout.close()
        def events(count, timeout=3):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                lines = calls.read_text().splitlines() if calls.exists() else []
                if len(lines) >= count:
                    return [json.loads(line) for line in lines]
                time.sleep(.01)
            self.fail("投递未触发 Codex 队列")
        def post(kind, key, **data):
            code, _, raw = self.request("/api/project/post", {"kind": kind, **data}, Authorization="Bearer "+key)
            self.assertEqual(200, code, raw)
            return json.loads(raw)["id"]
        proc = start()
        self.assertFalse(calls.exists())
        post("broadcast", self.ak, session="sender", body="频道广播", request="broadcast")
        first = post("question", self.ak, session="sender", target=self.b, body="来自频道的原文", request="first")
        args, = events(1)
        self.assertEqual(["queue", "--thread", thread, "--message"], args[:4])
        self.assertIn(f"message={first}", args[4])
        self.assertNotIn("来自频道的原文", args[4])
        self.assertNotIn(self.bk, args[4])
        sid = (self.root / "sessions" / (thread + ".id")).read_text().strip()
        first_receipt = self.call(self.bk, "receive", sid, message=first)
        self.assertNotIn(first_receipt["ticket"], args[4])
        command = [sys.executable, str(script), "--binding", str(record), "--thread", thread]
        read = subprocess.check_output([*command, "--read", str(first)], text=True)
        self.assertEqual("来自频道的原文", json.loads(read)["body"])
        self.assertNotIn(first_receipt["ticket"], read)
        self.assertNotIn(self.bk, read)
        # Wait for the durable queue acknowledgement before simulating a listener restart.
        checkpoint = self.root / "sessions" / (thread + ".wake.json")
        deadline = time.monotonic() + 2
        while not json.loads(checkpoint.read_text())["queued"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(json.loads(checkpoint.read_text())["queued"])
        stop(proc)
        start()
        second = post("question", self.ak, session="sender", target=self.b, body="ＨＩ！", request="second")
        self.assertIsNone(self.call(self.bk, "next", sid, awaiting_ticket=first_receipt["ticket"])["delivery"])
        time.sleep(.12)
        self.assertEqual(1, len(events(1)))
        sent = subprocess.run([*command, "--reply", str(first)], input="已收到", capture_output=True,
                              text=True, check=True, env={**os.environ, "SSL_CERT_FILE": str(self.root/"cert.pem")})
        self.assertIn("已答复问题", sent.stdout)
        ordinary = next(m for m in self.call(self.ak,"history",topic=first)["messages"] if m["kind"]=="answer")
        self.assertFalse(ordinary["quick_reply"])
        self.assertEqual("已收到", ordinary["body"])
        self.assertIn(f"message={second}", events(2)[1][4])
        read = subprocess.check_output([*command, "--read", str(second)], text=True)
        self.assertEqual("ＨＩ！", json.loads(read)["body"])
        self.assertEqual(["question"], [m["kind"] for m in self.call(self.ak, "history", topic=second)["messages"]])
        authorization = {"Authorization":"Bearer "+self.owner}
        status, _, raw = self.request("/api/owner/private-start", {"group":self.g,"target":self.b,
                                      "body":"私聊正文不进入通知","request":"first-private"}, **authorization)
        self.assertEqual(200,status,raw)
        private_thread = json.loads(raw)["thread"]
        notice = events(3,timeout=9)[2][4]
        self.assertIn("叠声系统请求",notice)
        self.assertIn("等用户明确同意",notice)
        self.assertNotIn("私聊正文不进入通知",notice)
        self.assertNotIn("--read",notice)
        notice_id = re.search(r"--defer-private ([0-9a-f]{32})",notice)[1]
        request = {"group":self.g,"thread":private_thread}
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            queued = json.loads(self.request("/api/owner/private-read",request,**authorization)[2])
            if queued["setup_status"] == "notified":
                break
            time.sleep(.01)
        self.assertEqual("notified",queued["setup_status"])
        self.assertFalse(queued["listener_enabled"])
        self.assertEqual("pending",queued["messages"][0]["status"])
        subprocess.run([*command,"--defer-private",notice_id],check=True,capture_output=True,
                       env={**os.environ,"SSL_CERT_FILE":str(self.root/"cert.pem")})
        self.assertEqual("deferred",json.loads(self.request("/api/owner/private-read",request,**authorization)[2])["setup_status"])

    def test_two_listeners_autonomously_consult_follow_up_and_return(self):
        listener = self.root / "codex-listen.py"
        listener.write_bytes(self.request("/codex-listen.py")[2])
        cli = self.root / "codex"
        cli.write_bytes(Path(__file__).with_name("peer_queue.py").read_bytes())
        cli.chmod(0o700)
        procs = []
        def stop_listeners():
            for proc in procs:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()
        self.addCleanup(stop_listeners)
        for name, member, key in (("analysis", self.a, self.ak), ("testing", self.b, self.bk)):
            folder = self.root / name
            folder.mkdir()
            record = folder / "channel.json"
            record.write_text(json.dumps({"agent":"codex","member":member,"group":self.g,
                                          "base":f"https://localhost:{self.server.server_port}"}))
            (folder / "auth.header").write_text("Authorization: Bearer " + key)
            proc = subprocess.Popen([sys.executable,str(listener),"--binding",str(record),"--thread",str(uuid4()),
                                     "--codex-bin",str(cli)], stdout=subprocess.PIPE,
                                    env={**os.environ,"SSL_CERT_FILE":str(self.root/"cert.pem"),"TEST_PEER":name})
            procs.append(proc)
            self.assertTrue(select.select([proc.stdout],[],[],4)[0])
            self.assertIn("已开始",proc.stdout.readline().decode())
        status, _, raw = self.request("/api/owner/speak", {"group":self.g,"target":self.a,"body":"v13 可以发布吗？",
                                      "request":"one-human-question"}, Authorization="Bearer "+self.owner)
        self.assertEqual(200,status)
        topic = json.loads(raw)["topic"]
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            records = self.relay.owner("history",{"group":self.g,"topic":topic},self.owner)["messages"]
            if records[-1]["body"] == "已核对 v13，可以发布":
                break
            time.sleep(.05)
        self.assertEqual(["question","question","answer","followup","answer","answer"], [r["kind"] for r in records])
        self.assertEqual([None,topic,records[1]["id"],records[2]["id"],records[3]["id"],topic],[r["parent"] for r in records])
        self.assertEqual({topic}, {r["topic"] for r in records})
        self.assertEqual("群主",records[-1]["recipient"])
        self.assertEqual("answered",self.relay.owner("topics",{"group":self.g},self.owner)["topics"][0]["status"])

    def test_codex_private_runner_and_direct_agent_initiation(self):
        script = self.root/"private.py"
        script.write_bytes(self.request("/codex-private.py")[2])
        model = self.root/"fake-codex"
        model.write_text("#!/usr/bin/env python3\n"
                         "import json,sys\nfrom pathlib import Path\n"
                         "args=sys.argv\n"
                         "assert '--ephemeral' in args and '--ignore-user-config' in args and 'shell_tool' in args\n"
                         "task=json.loads(sys.stdin.read().split('\\n',1)[1])['私聊']\n"
                         "assert all('公开标记' not in x['body'] for x in task['history'])\n"
                         "if task['body']=='请继续':\n"
                         "    assert [x['body'] for x in task['history']]==['项目私聊首问','私聊答复']\n"
                         "reply='续聊已验证' if task['history'] else '私聊答复'\n"
                         "Path(args[args.index('--output-last-message')+1]).write_text(reply)\n")
        model.chmod(0o700)
        def binding(name, key, member):
            folder=self.root/name
            folder.mkdir()
            config=folder/"channel.json"
            config.write_text(json.dumps({"agent":"codex","member":member,"group":self.g,
                                          "base":f"https://localhost:{self.server.server_port}"}))
            (folder/"auth.header").write_text("Authorization: Bearer "+key)
            return [sys.executable,str(script),"--binding",str(config)]
        receiver=binding("receiver",self.bk,self.b)
        caller=binding("caller",self.ak,self.a)
        env={**os.environ,"SSL_CERT_FILE":str(self.root/"cert.pem")}
        proc=subprocess.Popen([*receiver,"listen","--codex-bin",str(model)],env=env,stdout=subprocess.PIPE)
        def stop():
            proc.terminate()
            proc.wait(timeout=5)
            proc.stdout.close()
        self.addCleanup(stop)
        self.assertTrue(select.select([proc.stdout],[],[],4)[0])
        self.assertIn("独立",proc.stdout.readline().decode())
        def run(*args,body=""):
            return json.loads(subprocess.check_output([*caller,*args],input=body,text=True,timeout=18,env=env))
        online = False
        for _ in range(20):
            if next(m for m in run("members") if m["id"]==self.b)["state"]=="online":
                online=True
                break
            time.sleep(.05)
        self.assertTrue(online)
        self.call(self.ak,"post",kind="broadcast",body="公开标记",request="public-not-private")
        started=run("start",self.b,"--title","项目间私聊","--wait","10",body="项目私聊首问")
        self.assertEqual("私聊答复",started["reply"])
        after=run("send",started["thread"],"--wait","10",body="请继续")
        self.assertEqual("续聊已验证",after["reply"])
        others=self.request("/api/owner/private-read",{"group":self.g,"thread":started["thread"]},
                            Authorization="Bearer "+self.owner)
        self.assertEqual(404,others[0])
        self.assertEqual(["公开标记"],[m["body"] for m in self.call(self.bk,"history")["messages"]])
        self.assertIsNone(self.next_delivery(self.bk))

    def test_web_changes_wake_on_message_and_claim_and_check_membership(self):
        def owner(action, **data):
            code, _, raw = self.request("/api/owner/"+action, {"group":self.g, **data}, Authorization="Bearer "+self.owner)
            self.assertEqual(200, code, raw)
            return json.loads(raw)
        revision = owner("detail")["revision"]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            watch = pool.submit(owner, "changes", since=revision, timeout=4)
            time.sleep(.06)
            self.assertFalse(watch.done())
            message = owner("speak", target=self.b, body="项目状态如何？", request="state")["id"]
            after_post = watch.result(timeout=2)["revision"]
            self.assertGreater(after_post, revision)
            watch = pool.submit(owner, "changes", since=after_post, timeout=4)
            time.sleep(.06)
            self.assertFalse(watch.done())
            receipt = self.request("/api/project/wait", {"session":"real","timeout":0}, Authorization="Bearer "+self.bk)
            self.assertEqual(message, json.loads(receipt[2])["delivery"]["id"])
            self.assertGreater(watch.result(timeout=2)["revision"], after_post)
        code = self.request("/api/owner/changes", {"group":self.g,"since":0})[0]
        self.assertEqual(401, code)

    def test_browser_member_has_only_the_invited_channel_and_can_talk(self):
        other = self.relay.owner("create", {"name": "另一个频道"}, self.owner)["group"]
        proposal = self.relay.owner("human_invite", {"group": self.g}, self.owner)
        invitation = {"code": proposal["url"].split("#invite=")[1]}
        path = "/api/member/" + self.g + "/"
        before = self.call(self.ak, "post", kind="broadcast", body="加入之前的讨论", request="before")["id"]
        self.assertEqual(200, self.request("/room/" + self.g)[0])
        self.assertEqual(401, self.request(path+"detail", {})[0])
        status, headers, preview = self.request(path+"preview", invitation)
        self.assertEqual(200, status)
        self.assertEqual({"name","purpose","expires","username","member","legacy"}, set(json.loads(preview)))
        self.assertNotIn("Set-Cookie",headers)
        self.assertEqual(401,self.request(path+"join",{**invitation,"name":"小雨"})[0])
        form = {**invitation,"name":"小雨","username":"xiaoyu","password":"sample-password-42"}
        status, headers, raw = self.request(path+"register",form)
        self.assertEqual(200,status,raw)
        joined = json.loads(raw)
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/api", "diesheng_account="):
            self.assertIn(flag, cookie)
        auth = {"Cookie": cookie.split(";")[0]}
        self.assertEqual(410,self.request(path+"register",{**form,"username":"another"})[0])
        self.assertEqual(410, self.request("/api/channel/preview", invitation)[0])
        def member(action, **body):
            code, _, raw = self.request(path+action, body, **auth)
            self.assertEqual(200, code, raw)
            return json.loads(raw)
        self.assertEqual(joined["member"],member("join",name="小雨",**invitation)["member"])
        self.assertEqual(401, self.request("/api/owner/groups", {}, **auth)[0])
        self.assertEqual(403, self.request(path+"human_invite", {}, **auth)[0])
        self.assertEqual(403, self.request(path+"history", {"group":other}, **auth)[0])
        self.assertEqual(403, self.request("/api/member/"+other+"/detail", {}, **auth)[0])
        self.assertEqual(403, self.request("/api/member/"+other+"/changes", {"since":0,"timeout":0}, **auth)[0])
        creds = auth["Cookie"].split("=")[1]
        self.assertEqual(403, self.request("/api/project/history", {"session":"browser"}, Authorization="Bearer "+creds)[0])
        self.assertEqual([self.g], [g["id"] for g in member("groups")["groups"]])
        detail = member("detail")
        self.assertNotIn("invites", detail)
        self.assertIn(before, {m["id"] for m in detail["history"]["messages"]})
        self.assertIn(("小雨","human"), {(m["name"],m["agent"]) for m in detail["members"]})
        msg = member("speak", target=self.b, body="能解释这个接口吗？", request="from-human")["id"]
        reply = self.answer(msg)
        history = {m["id"]:m for m in member("history")["messages"]}
        self.assertEqual(("小雨","human"), (history[msg]["author"], history[msg]["author_agent"]))
        self.assertEqual(msg, history[reply]["parent"])
        question = self.call(self.ak, "post", kind="question", target=joined["member"], body="可以发布吗？", request="to-human")["id"]
        data = {"parent": question, "body":"可以发布。", "request":"human-answer"}
        answer = member("speak", **data)["id"]
        self.assertEqual(answer, member("speak", **data)["id"])
        self.assertEqual(answer, self.next_delivery(self.ak)["id"])
        owner_note = self.relay.owner("speak", {"group":self.g, "target":joined["member"], "body":"明天见",
                                                "request":"owner-note"}, self.owner)["id"]
        self.assertEqual("chat", next(m for m in member("history")["messages"] if m["id"]==owner_note)["kind"])
        announcement = member("speak", body="请大家核对一下", request="everyone")["id"]
        record = next(m for m in member("history")["messages"] if m["id"]==announcement)
        self.assertEqual({"前端", "后端"}, {r["name"] for r in record["recipients"]})
        self.relay.owner("remove", {"group":self.g, "member":joined["member"]}, self.owner)
        self.assertEqual(403, self.request(path+"history", {}, **auth)[0])
        self.assertEqual(403, self.request(path+"changes", {"since":0,"timeout":0}, **auth)[0])
        self.assertEqual(403, self.request(path+"speak", {"body":"已离开"}, **auth)[0])
        for op in ("revoke", "expire"):
            new = self.relay.owner("human_invite", {"group":self.g}, self.owner)
            if op=="revoke":
                self.relay.owner("revoke", {"group":self.g, "invite":new["id"]}, self.owner)
            else:
                self.now += 86401
            self.assertEqual(410, self.request(path+"preview", {"code":new["url"].split("#invite=")[1]})[0])

    def legacy_colleague(self, gid, name):
        session = token()
        with transaction(self.relay.path) as db:
            member = self.relay._new_colleague(db,gid,{"name":name},self.now)
            db.execute("INSERT INTO browser_sessions VALUES (?,?,?)",(digest(session),member,self.now+BROWSER_LIFETIME))
        return member,{"Cookie":"diesheng_member="+session}

    def test_account_login_restores_multiple_channels_and_logs_out_one_browser(self):
        other = self.relay.owner("create",{"name":"其他联调"},self.owner)["group"]
        creds = {"username":"Test.User","password":"Test!842"}
        root = "/api/member/"+self.g+"/"
        second = "/api/member/"+other+"/"
        def invite(group):
            return self.relay.owner("human_invite",{"group":group},self.owner)["url"].split("#invite=")[1]
        code = invite(self.g)
        status, _, body = self.request(root+"register",{**creds,"code":code,"name":"小雨","password":creds["password"][:-1]})
        self.assertEqual(400,status,body)
        self.assertEqual("请使用 8–128 个字符的密码",json.loads(body)["error"])
        status, headers, body = self.request(root+"register",{**creds,"code":code,"name":"小雨"})
        self.assertEqual(200,status,body)
        first_member = json.loads(body)["member"]
        browser = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        code = invite(other)
        self.assertEqual(409,self.request(second+"register",{**creds,"code":code,"name":"小雨"})[0])
        self.assertEqual(409,self.request(second+"join",{"code":code,"name":"小雨","username":"other.user"},**browser)[0])
        result = self.request(second+"join",{"code":code,"name":"小雨","username":"test.user"},**browser)
        self.assertEqual(200,result[0],result[2])
        second_member = json.loads(result[2])["member"]
        self.assertNotEqual(first_member,second_member)
        wrong = self.request("/api/account/login",{**creds,"password":"wrong"})
        unknown = self.request("/api/account/login",{**creds,"username":"unknown"})
        self.assertEqual(401,wrong[0])
        self.assertEqual(wrong[2],unknown[2])
        status, headers, body = self.request("/api/account/login",{**creds,"username":"test.user"})
        self.assertEqual(200,status,body)
        another_browser = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        channels = json.loads(self.request("/api/account/groups",{},**another_browser)[2])
        self.assertEqual("test.user",channels["username"])
        self.assertEqual({self.g,other},{g["id"] for g in channels["groups"]})
        for base, member in ((root,first_member),(second,second_member)):
            self.assertEqual(member,json.loads(self.request(base+"groups",{},**another_browser)[2])["self"]["id"])
        logout = self.request("/api/account/logout",{},**browser)
        self.assertIn("Max-Age=0",logout[1]["Set-Cookie"])
        self.assertEqual(401,self.request(root+"groups",{},**browser)[0])
        self.assertEqual(401,self.request(second+"groups",{},**browser)[0])
        self.assertEqual(200,self.request(root+"groups",{},**another_browser)[0])
        self.assertEqual(200,self.request(second+"leave",{},**another_browser)[0])
        self.assertEqual(403,self.request(second+"detail",{},**another_browser)[0])
        self.assertEqual(200,self.request(root+"groups",{},**another_browser)[0])
        self.now += BROWSER_LIFETIME+1
        self.assertEqual(401,self.request("/api/account/groups",{},**another_browser)[0])
        status, headers, _ = self.request("/api/account/login",creds)
        self.assertEqual(200,status)
        renewed = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        self.assertEqual(first_member,json.loads(self.request(root+"groups",{},**renewed)[2])["self"]["id"])
        with transaction(self.relay.path) as db:
            self.assertNotIn(creds["password"],"\n".join(db.iterdump()))

    def test_colleague_can_change_login_name_and_password(self):
        root = "/api/member/"+self.g+"/"
        def register(username, name):
            invite = self.relay.owner("human_invite",{"group":self.g},self.owner)["url"].split("#invite=")[1]
            status, headers, raw = self.request(root+"register",{"code":invite,"name":name,"username":username,"password":"Test!842"})
            self.assertEqual(200,status,raw)
            return json.loads(raw)["member"],{"Cookie":headers["Set-Cookie"].split(";")[0]}

        member, browser = register("test.user","小雨")
        _, other_account = register("other.user","小林")
        login = "/api/account/login"
        update = "/api/account/update"
        old = {"username":"test.user","password":"Test!842"}
        status, headers, raw = self.request(login,old)
        self.assertEqual(200,status,raw)
        other_browser = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        change = {"current_username":"test.user","username":"Renamed.User","current_password":old["password"]}
        self.assertEqual(401,self.request(update,change)[0])
        self.assertEqual(409,self.request(update,change,**other_account)[0])
        status, _, raw = self.request(update,{**change,"current_password":"incorrect"},**browser)
        self.assertEqual(401,status,raw)
        self.assertEqual("invalid_current_password",json.loads(raw)["code"])
        status, _, raw = self.request(update,{**change,"username":"OTHER.USER","new_password":"Next!842"},**browser)
        self.assertEqual(409,status,raw)
        self.assertEqual("username_taken",json.loads(raw)["code"])
        self.assertEqual(400,self.request(update,{**change,"new_password":"Next!84"},**browser)[0])

        status, _, raw = self.request(update,change,**browser)
        self.assertEqual(200,status,raw)
        self.assertEqual({"username":"renamed.user"},json.loads(raw))
        self.assertEqual("renamed.user",json.loads(self.request("/api/account/groups",{},**other_browser)[2])["username"])
        self.assertEqual(401,self.request(login,old)[0])
        self.assertEqual(409,self.request(update,change,**browser)[0])

        status, _, raw = self.request(update,{**change,"current_username":"renamed.user","new_password":"Next!842"},**browser)
        self.assertEqual(200,status,raw)
        account = json.loads(self.request(root+"groups",{},**browser)[2])
        self.assertEqual({"name":"小雨","id":member},account["self"])
        self.assertEqual(401,self.request("/api/account/groups",{},**other_browser)[0])
        self.assertEqual(200,self.request("/api/account/groups",{},**other_account)[0])
        self.assertEqual(401,self.request(login,{**old,"username":"renamed.user"})[0])
        self.assertEqual(200,self.request(login,{"username":"renamed.user","password":"Next!842"})[0])

    def test_legacy_memberships_can_attach_without_replacing_identity_or_private_access(self):
        member, old = self.legacy_colleague(self.g,"小雨")
        root = "/api/member/"+self.g+"/"
        earlier = json.loads(self.request(root+"private-start",{"target":self.a,"body":"原来的私聊","request":"old"},**old)[2])
        offer = json.loads(self.request(root+"agent-bind-code",{"agent":self.a},**old)[2])["url"].split("#bind=")[1]
        self.call(self.ak,"bind-human",code=offer)
        link = self.relay.owner("recovery_link",{"group":self.g,"member":member},self.owner)["url"].split("#recover=")[1]
        creds = {"username":"old-member","password":"Test!926"}
        self.assertEqual(401,self.request(root+"upgrade",creds)[0])
        status, headers, raw = self.request(root+"upgrade",creds,**old)
        self.assertEqual(200,status,raw)
        self.assertEqual(member,json.loads(raw)["member"])
        self.assertEqual(410,self.request(root+"recover_preview",{"code":link})[0])
        self.assertEqual(401,self.request(root+"groups",{},**old)[0])
        auth = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        self.assertEqual(200,self.request(root+"private-read",{"thread":earlier["thread"]},**auth)[0])
        self.assertEqual([self.a],json.loads(self.request(root+"history",{"scope":"mine"},**auth)[2])["my_agents"])
        other = self.relay.owner("create",{"name":"老频道"},self.owner)["group"]
        old_member, other_session = self.legacy_colleague(other,"原昵称")
        another_root = "/api/member/"+other+"/"
        both = {"Cookie":auth["Cookie"]+"; "+other_session["Cookie"]}
        self.assertEqual(403,self.request(another_root+"groups",{},**both)[0])
        self.assertEqual(401,self.request(another_root+"claim",{},**auth)[0])
        self.assertEqual(409,self.request(another_root+"claim",{"username":"other.user"},**both)[0])
        self.assertEqual(200,self.request(another_root+"claim",{"username":"old-member"},**both)[0])
        self.assertEqual(old_member,json.loads(self.request(another_root+"groups",{},**auth)[2])["self"]["id"])

    def test_human_only_channel_can_chat_and_session_expires(self):
        gid = self.relay.owner("create", {"name":"产品讨论"}, self.owner)["group"]
        member, auth = self.legacy_colleague(gid,"小林")
        root = "/api/member/"+gid+"/"
        self.assertEqual(200, self.request(root+"speak",{"body":"第一条讨论","request":"first"},**auth)[0])
        result = self.relay.owner("speak",{"group":gid,"body":"收到","request":"second"},self.owner)
        feed = json.loads(self.request(root+"history",{},**auth)[2])["messages"]
        self.assertEqual(["第一条讨论","收到"], [m["body"] for m in feed])
        self.assertEqual(result["id"], feed[-1]["id"])
        self.now += 30*86400+1
        self.assertEqual(401, self.request(root+"detail",{},**auth)[0])
        self.relay.prune()
        self.assertEqual([member],[m["id"] for m in self.relay.owner("detail",{"group":gid},self.owner)["members"]])
        self.assertEqual(401,self.request(root+"detail",{},**auth)[0])

    def test_colleague_logout_revokes_session_without_leaving_channel(self):
        member, auth = self.legacy_colleague(self.g,"小林")
        other_group = self.relay.owner("create",{"name":"发布协调"},self.owner)["group"]
        other_member, other_auth = self.legacy_colleague(other_group,"小林")
        root = "/api/member/"+self.g+"/"
        other = "/api/member/"+other_group+"/"
        self.assertEqual(200,self.request(other+"logout",{},**auth)[0])
        self.assertEqual(200,self.request(root+"groups",{},**auth)[0])
        response = self.request(root+"logout",{},**auth)
        self.assertEqual(200,response[0])
        self.assertIn("Max-Age=0",response[1]["Set-Cookie"])
        self.assertEqual(200,self.request(root+"logout",{})[0])
        self.assertEqual(401,self.request(root+"groups",{},**auth)[0])
        self.assertEqual(other_member,json.loads(self.request(other+"groups",{},**other_auth)[2])["self"]["id"])
        self.relay.prune()
        self.assertIn(member,{m["id"] for m in self.relay.owner("detail",{"group":self.g},self.owner)["members"]})
        link = self.relay.owner("recovery_link",{"group":self.g,"member":member},self.owner)["url"]
        status, _, raw = self.request(root+"recover",{"code":link.split("#recover=")[1],"request":uuid4().hex})
        self.assertEqual(200,status,raw)
        self.assertEqual(member,json.loads(raw)["member"])

    def test_saved_code_restores_member_and_private_history_after_expiration(self):
        member, old = self.legacy_colleague(self.g,"小林")
        root = "/api/member/"+self.g+"/"
        self.assertEqual(401,self.request(root+"recovery_code",{})[0])
        def issue():
            status, headers, raw = self.request(root+"recovery_code",{},**old)
            self.assertEqual(200,status,raw)
            self.assertEqual("no-store",headers["Cache-Control"])
            group, code = json.loads(raw)["code"].split(".")
            self.assertEqual(self.g,group)
            return code
        obsolete = issue()
        code = issue()
        self.assertEqual(410,self.request(root+"recover",{"kind":"code","code":obsolete,"request":uuid4().hex})[0])
        self.now += BROWSER_LIFETIME - 10
        private = self.request(root+"private-start",{"target":self.b,"body":"私下核对样本","request":"private"},**old)
        self.assertEqual(200,private[0],private[2])
        thread = json.loads(private[2])["thread"]
        self.relay.owner("broadcast",{"group":self.g,"mentions":[member],"body":"保留的点名记录","request":"ping"},self.owner)
        link = self.relay.owner("recovery_link",{"group":self.g,"member":member},self.owner)["url"].split("#recover=")[1]
        self.now += 11
        self.relay.prune()
        self.assertEqual(401,self.request(root+"groups",{},**old)[0])

        attempt = {"kind":"code","code":code,"request":uuid4().hex}
        status, headers, raw = self.request(root+"recover",attempt)
        self.assertEqual(200,status,raw)
        self.assertEqual({"member":member},json.loads(raw))
        for flag in ("HttpOnly","SameSite=Strict","Secure","Path="+root):
            self.assertIn(flag,headers["Set-Cookie"])
        current = {"Cookie":headers["Set-Cookie"].split(";")[0]}
        self.assertNotEqual(old,current)
        recovered = json.loads(self.request(root+"groups",{},**current)[2])["self"]
        self.assertEqual({"id":member,"name":"小林"},recovered)
        read = json.loads(self.request(root+"private-read",{"thread":thread},**current)[2])
        self.assertEqual(["私下核对样本"],[m["body"] for m in read["messages"]])
        feed = json.loads(self.request(root+"history",{"scope":"mine"},**current)[2])["messages"]
        self.assertEqual(["保留的点名记录"],[m["body"] for m in feed])
        self.assertEqual(410,self.request(root+"recover_preview",{"code":link})[0])
        self.assertEqual(headers["Set-Cookie"],self.request(root+"recover",attempt)[1]["Set-Cookie"])
        self.assertEqual(410,self.request(root+"recover",{**attempt,"request":uuid4().hex})[0])
        self.now += RECOVERY_RETRY + 1
        self.assertEqual(410,self.request(root+"recover",attempt)[0])

    def test_owner_link_is_scoped_single_use_and_revoked_when_member_leaves(self):
        member, old = self.legacy_colleague(self.g,"小林")
        other, other_auth = self.legacy_colleague(self.g,"小雨")
        outside = self.relay.owner("create",{"name":"其他频道"},self.owner)["group"]
        root = "/api/member/"+self.g+"/"
        owner = {"Authorization":"Bearer "+self.owner}
        payload = {"group":self.g,"member":member}
        self.assertEqual(401,self.request("/api/owner/recovery_link",payload,**old)[0])
        self.assertEqual(404,self.request("/api/owner/recovery_link",{**payload,"group":outside},**owner)[0])
        self.assertEqual(404,self.request("/api/owner/recovery_link",{**payload,"member":self.a},**owner)[0])
        def link_for():
            status, _, raw = self.request("/api/owner/recovery_link",payload,**owner)
            self.assertEqual(200,status,raw)
            return json.loads(raw)["url"].split("#recover=")[1]

        replaced = link_for()
        code = link_for()
        self.assertEqual(410,self.request(root+"recover_preview",{"code":replaced})[0])
        self.assertEqual(410,self.request("/api/member/"+outside+"/recover_preview",{"code":code})[0])
        status, headers, raw = self.request(root+"recover_preview",{"code":code})
        self.assertEqual(200,status,raw)
        self.assertNotIn("Set-Cookie",headers)
        self.assertEqual("小林",json.loads(raw)["name"])
        attempt = {"code":code,"request":uuid4().hex}
        self.assertEqual(409,self.request(root+"recover",attempt,**other_auth)[0])
        self.assertEqual(other,json.loads(self.request(root+"groups",{},**other_auth)[2])["self"]["id"])
        with concurrent.futures.ThreadPoolExecutor() as pool:
            a = pool.submit(self.request,root+"recover",attempt)
            b = pool.submit(self.request,root+"recover",{**attempt,"request":uuid4().hex})
            outcomes = [a.result(),b.result()]
        self.assertEqual([200,410],sorted(outcome[0] for outcome in outcomes))
        success = next(outcome for outcome in outcomes if outcome[0]==200)
        current = {"Cookie":success[1]["Set-Cookie"].split(";")[0]}
        self.assertEqual(member,json.loads(self.request(root+"groups",{},**current)[2])["self"]["id"])
        self.assertEqual(401,self.request(root+"groups",{},**old)[0])
        self.assertEqual(410,self.request(root+"recover_preview",{"code":code})[0])
        expired = link_for()
        self.now += RECOVERY_LINK_LIFETIME + 1
        self.assertEqual(410,self.request(root+"recover",{"code":expired,"request":uuid4().hex})[0])
        pending = link_for()
        saved = json.loads(self.request(root+"recovery_code",{},**current)[2])["code"].split(".")[1]
        self.assertEqual(200,self.request(root+"leave",{},**current)[0])
        self.assertEqual(410,self.request(root+"recover",{"code":pending,"request":uuid4().hex})[0])
        self.assertEqual(410,self.request(root+"recover",{"code":saved,"kind":"code","request":uuid4().hex})[0])
        self.assertEqual(404,self.request("/api/owner/recovery_link",payload,**owner)[0])

    def test_published_join_and_recovery_in_fresh_sessions(self):
        self.assertIn(b"/project-memory.md",self.request("/join")[2])
        guide=self.request("/project-memory.md")[2].decode()
        examples=re.findall(chr(96)*3+r"sh\n(.*?)\n"+chr(96)*3,guide,re.S)
        join,restore=examples
        listen=re.findall(chr(96)*3+r"sh\n(.*?)\n"+chr(96)*3,self.request("/agent.md")[2].decode(),re.S)[0]
        root=self.root/"checkout"
        root.mkdir()
        original="原有项目规则\n"
        (root/"AGENTS.md").write_text(original)
        env={**os.environ,"XDG_STATE_HOME":str(self.root/"state"),"TMPDIR":str(self.root),
             "CURL_CA_BUNDLE":str(self.root/"cert.pem"),"NO_PROXY":"localhost","no_proxy":"localhost"}
        tool = self.root/"state/diesheng/binding_state.py"
        tool.parent.mkdir(parents=True)
        tool.write_bytes(self.request("/binding_state.py")[2])
        def run(example,client,invite="",work=root):
            script=example.replace("用户给的完整链接",invite).replace("CLIENT=codex","CLIENT="+client)
            return subprocess.run(["/bin/sh","-eu","-c",script],cwd=work,env=env,
                                  capture_output=True,text=True,check=True).stdout
        def invitation(group):
            issued=self.relay.owner("channel_link",{"group":group},self.owner)["url"]
            return f"https://localhost:{self.server.server_port}/join#"+issued.split("#")[1]
        link=invitation(self.g)
        before=self.call(self.ak,"post",kind="broadcast",body="入群前消息",request="before")["id"]
        local_rules=root/"AGENTS.md"
        def publish(version, body):
            return self.request("/api/owner/announcement-publish",
                                {"group":self.g, "version":version, "body":body}, Authorization="Bearer "+self.owner)[0]
        self.assertEqual(200, publish(0, "规则一。"))
        identities={}
        for client in ("codex","claude","deepseek"):
            output=run(join,client,link)
            path=Path(re.search(r"^AUTH_FILE=(.+)$",output,re.M)[1])
            first_sid=re.search(r"^SESSION_ID=(.+)$",output,re.M)[1]
            self.assertTrue(path.is_relative_to(self.root/"state"))
            self.assertEqual(0,path.stat().st_mode & 0o077)
            auth=path.read_text().strip().removeprefix("Authorization: Bearer ")
            self.assertNotIn(auth,output)
            status=json.loads(self.request("/api/project/status",{"session":first_sid},Authorization="Bearer "+auth)[2])
            self.assertTrue(status["verified"])
            self.assertEqual(client,status["agent"])
            identities[client]=status["member"],first_sid,auth
        self.assertEqual(1, local_rules.read_text().count("> 规则一。"))
        other=self.relay.owner("create",{"name":"另一个频道"},self.owner)["group"]
        other_join = run(join,"codex",invitation(other))
        other_auth = Path(re.search(r"^AUTH_FILE=(.+)$",other_join,re.M)[1])
        run(join,"codex",link)
        self.relay.owner("disable_link",{"group":self.g},self.owner)
        self.assertEqual(200, publish(1, "规则二。"))
        self.assertEqual(200, publish(2, "规则三。"))
        rules=[(root/name).read_text() for name in ("AGENTS.md","CLAUDE.local.md")]
        self.assertTrue(rules[0].startswith(original))
        for text in rules:
            self.assertEqual(1,text.count("<!-- diesheng-project-v1 -->"))
            self.assertNotIn(link.split("#")[1],text)
            for _,_,auth in identities.values():
                self.assertNotIn(auth,text)
        bindings = {}
        for client,(member,previous_sid,auth) in identities.items():
            question=self.relay.owner("speak",{"group":self.g,"target":member,"body":"当前项目能处理吗？",
                                               "request":"from-owner-"+client},self.owner)["id"]
            # A new process receives only its working directory and client type.
            recovered=[json.loads(line) for line in run(restore,client).splitlines()]
            self.assertEqual(2 if client=="codex" else 1,len(recovered))
            binding=next(b for b in recovered if b["context"]["group"]["id"]==self.g)
            bindings[client] = Path(binding["auth_file"]).with_name("channel.json")
            context=binding["context"]
            self.assertEqual(2,context["announcements_synced"])
            self.assertEqual(member,context["self"]["id"])
            self.assertEqual("群主",context["group"]["owner_name"])
            self.assertTrue(set(i[0] for i in identities.values()) <= {m["id"] for m in context["members"]})
            self.assertNotIn(before,{m["id"] for m in context["recent"]})
            sid=binding["session"]
            self.assertNotEqual(previous_sid,sid)
            header={"Authorization":"Bearer "+auth}
            listener_env={**env,"BASE":binding["base"],"AUTH_FILE":binding["auth_file"],"SESSION_ID":sid}
            waiting=subprocess.run(["/bin/sh","-eu","-c",listen],cwd=root,env=listener_env,
                                   capture_output=True,text=True,check=True,timeout=10)
            delivery=json.loads(waiting.stdout)["delivery"]
            self.assertEqual(question,delivery["id"])
            answer={"session":sid,"kind":"answer","parent":question,"ticket":delivery["ticket"],"body":"可以处理。",
                    "request":"answer-"+str(question)}
            self.assertEqual(200,self.request("/api/project/post",answer,**header)[0])
        self.assertEqual(200, publish(3, "等待中的新规则。"))
        waiting=subprocess.run(["/bin/sh","-eu","-c",listen],cwd=root,env=listener_env,
                               capture_output=True,text=True,check=True,timeout=10)
        self.assertEqual(1, json.loads(waiting.stdout)["announcements_synced"])
        self.assertIn("> 等待中的新规则。", local_rules.read_text())
        unrelated=self.root/"another-checkout"
        unrelated.mkdir()
        self.assertEqual("",run(restore,"codex",work=unrelated))
        with (root/"CLAUDE.local.md").open("a") as user_guide:
            user_guide.write("用户后来追加的说明\n")

        codex = tool.with_name("fake-codex")
        events = self.root/"announcement-events"
        codex.write_text("#!/bin/sh\nif [ \"$2\" != --help ]; then printf '%s\\n' \"$@\" >> \"$TEST_ANNOUNCEMENT_EVENTS\"; fi\n")
        codex.chmod(0o700)
        listener = tool.with_name("codex-listen.py")
        listener.write_bytes(self.request("/codex-listen.py")[2])
        extra_file = bindings["codex"].with_name("local-note.txt")
        extra_file.write_text("保留用户文件")
        bindings["codex"].with_name("private.header").write_text("测试凭据")
        command = [sys.executable,str(listener),"--binding",str(bindings["codex"]),"--thread",str(uuid4()),"--codex-bin",str(codex)]
        with subprocess.Popen(command, cwd=root, env={**env,"SSL_CERT_FILE":str(self.root/"cert.pem"),
                                                      "TEST_ANNOUNCEMENT_EVENTS":str(events)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as watcher:
            try:
                self.assertTrue(select.select([watcher.stdout], [], [], 4)[0], "频道监听未启动")
                self.assertIn("已开始等待",watcher.stdout.readline())
                self.assertEqual(200, publish(4, "Codex 在线追加的规则。"))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if ("第 5 版" in local_rules.read_text() and events.is_file()
                            and "群公告已更新" in events.read_text()
                            and "notice" not in self.call(identities["codex"][2], "context", announcements="AGENTS.md")):
                        break
                    time.sleep(.05)
                else:
                    self.fail("Codex 未同步并回执最新公告")
                self.assertNotIn("Codex 在线追加的规则。",events.read_text())
                self.assertEqual({"dissolved":True},self.relay.owner("dissolve",{"group":self.g},self.owner))
                stdout, stderr = watcher.communicate(timeout=8)
                self.assertEqual(0,watcher.returncode,stderr)
                self.assertIn("已清理本机绑定",stdout)
            finally:
                if watcher.poll() is None:
                    watcher.terminate()
        self.assertFalse(bindings["codex"].exists())
        self.assertFalse(bindings["codex"].with_name("private.header").exists())
        self.assertFalse(bindings["codex"].with_name("auth.header").exists())
        self.assertFalse(bindings["codex"].with_name("sessions").exists())
        self.assertEqual("保留用户文件",extra_file.read_text())
        self.assertTrue(other_auth.is_file())
        self.assertIn("diesheng-project-v1",(root/"AGENTS.md").read_text())

        for client in ("claude", "deepseek"):
            self.assertTrue(bindings[client].exists())
            self.assertEqual({"group":self.g,"status":"dissolved","cleaned":True},json.loads(run(restore,client)))
            self.assertFalse(bindings[client].exists())
        self.assertEqual("用户后来追加的说明\n",(root/"CLAUDE.local.md").read_text())
        self.assertIn("diesheng-project-v1",(root/"AGENTS.md").read_text())
        gone = subprocess.check_output([sys.executable,str(tool),"leave","--binding",str(other_auth.with_name("channel.json"))],
                                       cwd=root,env=env,text=True)
        self.assertEqual({"group":other,"status":"left","cleaned":True},json.loads(gone))
        self.assertFalse(other_auth.exists())
        agent_guide = local_rules.read_text()
        self.assertTrue(agent_guide.startswith(original))
        self.assertNotIn("diesheng-project-v1", agent_guide)
        self.assertFalse((root/"agent.md").exists())
        self.assertEqual([str(version) for version in range(1,6)],re.findall(r" · 第 (\d+) 版", agent_guide))

    def test_dissolve_wakes_projects_and_closes_access(self):
        payload = {"group":self.g}
        receiver = {"Authorization":"Bearer "+self.bk}
        owner = {"Authorization":"Bearer "+self.owner}
        self.assertEqual(401,self.request("/api/owner/dissolve",payload,**receiver)[0])
        self.assertEqual(403,self.request("/api/project/leave",{**payload,"session":"leave","member":self.a},**receiver)[0])
        code = self.relay.owner("channel_link",payload,self.owner)["url"].split("#c=")[1]
        member, browser = self.legacy_colleague(self.g,"小雨")
        recovery = self.relay.owner("recovery_link",{**payload,"member":member},self.owner)["url"].split("#recover=")[1]
        private_key = token()
        self.relay.private_agent("enable",{"credential":private_key},self.bk)
        private = {"Authorization":"Bearer "+private_key}
        question = self.ask()
        delivered = self.call(self.bk,"next","waiting")["delivery"]
        self.assertEqual(question,delivered["id"])
        waiting = {"session":"waiting","awaiting_ticket":delivered["ticket"],"timeout":4,"lifecycle":True}
        with concurrent.futures.ThreadPoolExecutor() as pool:
            poll = pool.submit(self.request,"/api/project/wait",waiting,**receiver)
            time.sleep(.12)
            self.assertEqual(200,self.request("/api/owner/dissolve",payload,**owner)[0])
            status, _, raw = poll.result(timeout=3)
        self.assertEqual(200,status,raw)
        notice = {"kind":"membership_ended","reason":"dissolved","group":self.g,"member":self.b}
        self.assertEqual({"delivery":None,"paused":False,"notice":notice},json.loads(raw))
        self.assertEqual(notice,self.call(self.bk,"context",lifecycle=True)["notice"])
        self.assertEqual(403,self.request("/api/project/history",waiting,**receiver)[0])
        self.assertEqual(403,self.request("/api/project/context",{"session":"legacy"},**receiver)[0])
        self.assertEqual(403,self.request("/api/private/next",{"session":"old"},**private)[0])
        self.assertEqual(410,self.request("/api/channel/preview",{"code":code})[0])
        self.assertEqual(410,self.request("/api/member/"+self.g+"/groups",{},**browser)[0])
        self.assertEqual(410,self.request("/api/member/"+self.g+"/recover_preview",{"code":recovery})[0])
        self.assertEqual(410,self.request("/api/owner/detail",payload,**owner)[0])
        self.assertEqual([],self.relay.owner("groups",{},self.owner)["groups"])
        self.assertEqual(200,self.request("/api/owner/dissolve",payload,**owner)[0])
