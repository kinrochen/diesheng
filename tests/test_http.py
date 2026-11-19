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

from agent_group_chat.common import token
from agent_group_chat.httpd import RelayHTTP
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
        def events(count):
            deadline = time.monotonic() + 3
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
        self.assertEqual({"name","purpose","expires"}, set(json.loads(preview)))
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Secure", "Path="+path):
            self.assertIn(flag, cookie)
        auth = {"Cookie": cookie.split(";")[0]}
        other_browser = {"Cookie": self.request(path+"preview", invitation)[1]["Set-Cookie"].split(";")[0]}
        self.assertEqual(401, self.request(path+"history", {}, **auth)[0])
        self.assertEqual(410, self.request("/api/channel/preview", invitation)[0])
        def member(action, **body):
            code, _, raw = self.request(path+action, body, **auth)
            self.assertEqual(200, code, raw)
            return json.loads(raw)
        joined = member("join", name="小雨", **invitation)
        self.assertEqual(joined, member("join", name="小雨", **invitation))
        self.assertEqual(410, self.request(path+"join", {**invitation, "name":"另一人"}, **other_browser)[0])
        self.assertEqual(401, self.request("/api/owner/groups", {}, **auth)[0])
        self.assertEqual(403, self.request(path+"human_invite", {}, **auth)[0])
        self.assertEqual(403, self.request(path+"history", {"group":other}, **auth)[0])
        self.assertEqual(401, self.request("/api/member/"+other+"/detail", {}, **auth)[0])
        self.assertEqual(401, self.request("/api/member/"+other+"/changes", {"since":0,"timeout":0}, **auth)[0])
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
        self.assertEqual(401, self.request(path+"history", {}, **auth)[0])
        self.assertEqual(401, self.request(path+"changes", {"since":0,"timeout":0}, **auth)[0])
        self.assertEqual(401, self.request(path+"speak", {"body":"已离开"}, **auth)[0])
        for op in ("revoke", "expire"):
            new = self.relay.owner("human_invite", {"group":self.g}, self.owner)
            if op=="revoke":
                self.relay.owner("revoke", {"group":self.g, "invite":new["id"]}, self.owner)
            else:
                self.now += 86401
            self.assertEqual(410, self.request(path+"preview", {"code":new["url"].split("#invite=")[1]})[0])

    def test_human_only_channel_can_chat_and_session_expires(self):
        gid = self.relay.owner("create", {"name":"产品讨论"}, self.owner)["group"]
        link = self.relay.owner("human_invite", {"group":gid}, self.owner)["url"]
        code = {"code":link.split("#invite=")[1]}
        root = "/api/member/"+gid+"/"
        auth = {"Cookie":self.request(root+"preview",code)[1]["Set-Cookie"].split(";")[0]}
        self.assertEqual(200, self.request(root+"join",{**code,"name":"小林"},**auth)[0])
        self.assertEqual(200, self.request(root+"speak",{"body":"第一条讨论","request":"first"},**auth)[0])
        result = self.relay.owner("speak",{"group":gid,"body":"收到","request":"second"},self.owner)
        feed = json.loads(self.request(root+"history",{},**auth)[2])["messages"]
        self.assertEqual(["第一条讨论","收到"], [m["body"] for m in feed])
        self.assertEqual(result["id"], feed[-1]["id"])
        self.now += 30*86400+1
        self.assertEqual(401, self.request(root+"detail",{},**auth)[0])
        self.relay.prune()
        self.assertFalse(self.relay.owner("detail",{"group":gid},self.owner)["members"])

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
        def run(example,client,invite="",work=root):
            script=example.replace("用户给的完整链接",invite).replace("CLIENT=codex","CLIENT="+client)
            return subprocess.run(["/bin/sh","-eu","-c",script],cwd=work,env=env,
                                  capture_output=True,text=True,check=True).stdout
        def invitation(group):
            issued=self.relay.owner("channel_link",{"group":group},self.owner)["url"]
            return f"https://localhost:{self.server.server_port}/join#"+issued.split("#")[1]
        link=invitation(self.g)
        before=self.call(self.ak,"post",kind="broadcast",body="入群前消息",request="before")["id"]
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
        other=self.relay.owner("create",{"name":"另一个频道"},self.owner)["group"]
        run(join,"codex",invitation(other))
        run(join,"codex",link)
        self.relay.owner("disable_link",{"group":self.g},self.owner)
        rules=[(root/name).read_text() for name in ("AGENTS.md","CLAUDE.local.md")]
        self.assertTrue(rules[0].startswith(original))
        for text in rules:
            self.assertEqual(1,text.count("<!-- diesheng-project-v1 -->"))
            self.assertNotIn(link.split("#")[1],text)
            for _,_,auth in identities.values():
                self.assertNotIn(auth,text)
        for client,(member,previous_sid,auth) in identities.items():
            question=self.relay.owner("speak",{"group":self.g,"target":member,"body":"当前项目能处理吗？",
                                               "request":"from-owner-"+client},self.owner)["id"]
            # A new process receives only its working directory and client type.
            recovered=[json.loads(line) for line in run(restore,client).splitlines()]
            self.assertEqual(2 if client=="codex" else 1,len(recovered))
            binding=next(b for b in recovered if b["context"]["group"]["id"]==self.g)
            context=binding["context"]
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
        unrelated=self.root/"another-checkout"
        unrelated.mkdir()
        self.assertEqual("",run(restore,"codex",work=unrelated))
