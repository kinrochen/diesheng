import concurrent.futures
import sqlite3
from agent_group_chat.common import Problem, ident, lan_ipv4, origin, token
from agent_group_chat.relay import ONLINE, RESPONSE_TIMEOUT, RETENTION, Relay
from tests.support import BASE, RelayCase


class Messaging(RelayCase):
    def test_greetings_require_an_agent_answer_even_from_older_listeners(self):
        for body in ("hi", "@后端：ＨＩ！", "你好", "在吗？", "ping", "👋"):
            with self.subTest(body=body):
                question = self.call(self.ak, "post", kind="question", target=self.b, body=body, request=ident())["id"]
                delivery = self.call(self.bk, "next", "B", presence_replies=True)["delivery"]
                self.assertEqual((question, body), (delivery["id"], delivery["body"]))
                before = self.call(self.ak, "history", topic=question)["messages"]
                self.assertEqual(["question"], [m["kind"] for m in before])
                sent = self.call(self.bk, "post", "B", kind="answer", parent=question, ticket=delivery["ticket"],
                                 body="测试会话已收到你的消息 " + str(question), request=ident())
                answer = self.call(self.ak, "history", topic=question)["messages"][-1]
                self.assertEqual((sent["id"], self.b, question), (answer["id"], answer["sender"], answer["parent"]))
                self.assertFalse(answer["quick_reply"])

    def test_once_only_invites_and_exact_retry(self):
        invite = self.invite("测试")
        p, key = {"code":invite["code"],"type":"personal","agent":"codex"}, token()
        original = self.relay.channel("join", p, key)
        self.assertEqual(original, self.relay.channel("join", p, key))
        with self.assertRaisesRegex(Problem, "已被使用"):
            self.relay.channel("join", p, token())
        self.assertNotIn(invite["code"], str(self.relay.owner("detail", {"group":self.g}, self.owner)))
        with sqlite3.connect(self.relay.path) as db:
            self.assertNotIn(key, "\n".join(db.iterdump()))

    def test_revocation_expiry_and_owner_scope(self):
        inv = self.invite("测试")
        other = self.relay.create_owner()
        with self.assertRaises(Problem):
            self.relay.owner("detail", {"group":self.g}, other)
        self.relay.owner("revoke", {"group":self.g,"invite":inv["id"]}, self.owner)
        with self.assertRaises(Problem):
            self.relay.channel("preview",{"type":"personal","code":inv["code"]})
        inv = self.invite("测试")
        self.now += 1801
        with self.assertRaisesRegex(Problem,"过期"):
            self.relay.channel("join", {"code":inv["code"],"type":"personal","agent":"codex"}, token())

    def test_isolated_history_and_retention(self):
        self.call(self.ak,"post",kind="broadcast",request=ident(),body="较早的广播")
        q = self.ask()
        _, newcomer = self.member("新成员")
        self.assertEqual([], self.call(newcomer,"history")["messages"])
        ans = self.answer(q)
        self.call(self.ak,"accept",answer=ans)
        self.assertEqual([], self.call(newcomer,"conclusions",query="旧订单字段")["messages"])
        self.assertIsNone(self.call(newcomer,"history")["messages"][0]["question"])
        self.assertEqual([], self.call(newcomer,"inbox")["messages"])
        self.now += RETENTION+1
        self.relay.prune()
        self.assertEqual([], self.call(self.bk,"history")["messages"])

    def test_end_to_end_after_restart_and_origin_closed(self):
        q = self.ask()
        self.relay = Relay(self.root/"server",BASE,lambda:self.now)
        received = self.call(self.bk,"inbox","B-new",unread=True)["messages"]
        self.assertEqual([(q,"pending")],[(m["id"],m["receipt"]) for m in received])
        ans = self.answer(q,"B-new")
        self.assertEqual(ans, self.next_delivery(self.ak, "another")["id"])
        self.assertIsNone(self.next_delivery(self.ak, "A"))
        log = self.call(self.ak,"inbox","fresh")["messages"]
        self.assertEqual([(q,"replied"),(ans,"received")],[(m["id"],m["receipt"]) for m in log])
        self.assertIsNone(self.next_delivery(self.bk,"B-new"))
        with self.assertRaises(Problem):
            self.call(self.ak,"post",kind="answer",parent=ans,request=ident(),body="循环",ticket="no")

    def test_competing_claims_and_duplicate_sends(self):
        q = self.ask()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            claims = list(pool.map(lambda s:(s,self.next_delivery(self.bk,s)), ["B1","B2","B3","B4"]))
        won = [(sid,c) for sid,c in claims if c]
        self.assertEqual(1,len(won))
        sid, c = won[0]
        with self.assertRaises(Problem):
            self.call(self.bk,"receive","other",message=q)
        self.call(self.bk,"receive",sid,message=q,ticket=c["ticket"])
        args = dict(kind="answer",request="answer:1",parent=q,body="null",ticket=c["ticket"])
        send=lambda _:self.call(self.bk,"post",sid,**args)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results=list(pool.map(send,range(4)))
        self.assertEqual(1,len({r["id"] for r in results}))
        with self.assertRaisesRegex(Problem,"请求编号"):
            self.call(self.bk,"post",sid,**{**args,"body":"修改"})
        self.assertEqual(2,len(self.call(self.ak,"history")["messages"]))

    def test_ambiguous_delivery_fences_old_ticket(self):
        q=self.ask()
        old=self.next_delivery(self.bk,"B")
        self.now+=RESPONSE_TIMEOUT+1
        self.assertIsNone(self.next_delivery(self.bk,"C"))
        self.assertEqual("uncertain",self.call(self.ak,"history")["messages"][0]["receipt"])
        self.relay.owner("requeue",{"group":self.g,"message":q},self.owner)
        with self.assertRaisesRegex(Problem,"失效"):
            self.call(self.bk,"receive","B",message=q,ticket=old["ticket"])
        new=self.next_delivery(self.bk,"C")
        self.assertNotEqual(old["ticket"],new["ticket"])
        self.call(self.bk,"receive","C",message=q,ticket=new["ticket"])
        self.assertEqual(new["ticket"],self.next_delivery(self.bk,"C")["ticket"])

    def test_three_reply_handoff(self):
        q=self.ask()
        ans=self.answer(q,"recent")
        for i in range(3):
            self.now+=1
            self.call(self.bk,"history","other")
            follow=self.call(self.ak,"post",kind="followup",parent=ans,body="再确认一次",request=ident())["id"]
            if i<2:
                self.assertEqual(follow,self.next_delivery(self.bk,"recent")["id"])
                ans=self.answer(follow,"recent")
        self.assertIsNone(self.next_delivery(self.bk,"other"))
        with self.assertRaisesRegex(Problem,"人工|开发者"):
            self.call(self.bk,"receive","other",message=follow)
        self.answer(follow,"other",human=True)

    def test_pause_resume_membership_and_broadcast(self):
        self.call(self.ak,"post",kind="broadcast",body="公告",request=ident())
        self.assertIsNone(self.next_delivery(self.bk,"B"))
        q=self.ask()
        self.call(self.bk,"project_pause",paused=True)
        self.assertIsNone(self.next_delivery(self.bk,"B"))
        self.call(self.bk,"project_pause",paused=False)
        self.relay.owner("pause",{"group":self.g,"paused":True},self.owner)
        self.assertIsNone(self.next_delivery(self.bk,"B"))
        self.assertEqual(q,self.call(self.bk,"inbox")["messages"][0]["id"])
        self.relay.owner("pause",{"group":self.g,"paused":False},self.owner)
        self.assertEqual(q,self.next_delivery(self.bk,"B")["id"])
        self.call(self.bk,"profile",name="后端 release",role="发布")
        self.relay.owner("remove",{"group":self.g,"member":self.b},self.owner)
        for sid in ("B","new"):
            with self.assertRaisesRegex(Problem,"授权已失效"):
                self.call(self.bk,"history",sid)
        self.assertEqual(1,len(self.call(self.ak,"members")["members"]))

    def test_adoption_evidence_search_and_paging(self):
        q=self.ask()
        a=self.answer(q)
        with self.assertRaisesRegex(Problem,"提问项目"):
            self.call(self.bk,"accept",answer=a)
        self.call(self.ak,"accept",answer=a)
        self.call(self.ak,"accept",answer=a)
        by_question=self.call(self.bk,"conclusions",query="旧订单")["messages"]
        self.assertEqual([a],[v["id"] for v in by_question])
        self.assertEqual(by_question,self.call(self.ak,"conclusions",query="ORDER_DETAIL_TEST")["messages"])
        self.assertEqual([],self.call(self.bk,"history",query="%")["messages"])
        first=self.call(self.bk,"history",after=0,limit=1)
        second=self.call(self.bk,"history",after=q,limit=1)
        self.assertTrue(first["more"])
        self.assertEqual(a,second["messages"][0]["id"])
        self.assertFalse(second["more"])

    def test_group_isolation_and_local_path_validation(self):
        other=self.relay.owner("create",{"name":"其他"},self.owner)["group"]
        _, key=self.member("后端",other)
        q=self.ask()
        with self.assertRaises(Problem):
            self.call(key,"receive",message=q)
        with self.assertRaises(Problem):
            self.call(key,"post",kind="question",request=ident(),target=self.b,body="错群")
        for text in ("/Users/someone/work/api", r"C:\work\private", "C:/work/private"):
            with self.assertRaisesRegex(Problem, "本机绝对路径"):
                self.call(self.ak,"post",kind="broadcast",request=ident(),body=text)
        self.assertEqual([],self.call(key,"history")["messages"])

    def test_old_announcement_receipts_replay_for_agents_md(self):
        for version in range(2):
            self.relay.owner("announcement-publish", {"group":self.g, "version":version,
                             "body":f"规则 {version+1}"}, self.owner)
        with sqlite3.connect(self.relay.path) as db:
            db.execute("INSERT INTO announcement_progress VALUES (?,?)", (self.a,2))
        self.relay = Relay(self.root/"server", BASE, lambda: self.now)

        def receipts():
            return self.relay.owner("detail", {"group":self.g}, self.owner)["announcement_receipts"]

        self.assertEqual([], receipts())
        self.assertNotIn("notice", self.call(self.ak, "next", announcements=True))
        with self.assertRaisesRegex(Problem, "AGENTS.md"):
            self.call(self.ak, "announcement-ack", version=2, file="agent.md")
        notice = self.call(self.ak, "context", announcements="AGENTS.md")["notice"]
        self.assertEqual((2,"AGENTS.md"), (notice["version"],notice["file"]))
        self.assertEqual(1, self.call(self.bk, "context", announcements="AGENTS.md")["notice"]["version"])
        self.assertIsNone(self.call(self.ak, "announcement-ack", version=2, file="AGENTS.md")["notice"])
        self.relay = Relay(self.root/"server", BASE, lambda: self.now)
        self.assertEqual([self.a], receipts())
        self.assertNotIn("notice", self.call(self.ak, "context", announcements="AGENTS.md"))

    def test_markdown_links_are_not_mistaken_for_local_paths(self):
        for body in ("[说明](https://example.com/docs)", "<http://example.com/docs>",
                     "![示意图](https://example.com/image.png)"):
            with self.subTest(body=body):
                message=self.call(self.ak,"post",kind="broadcast",body=body,request=ident())
                saved=self.call(self.ak,"history")["messages"][-1]
                self.assertEqual((message["id"],body),(saved["id"],saved["body"]))

    def test_challenge_and_presence_are_separate(self):
        def detail():
            return next(m for m in self.relay.owner("detail",{"group":self.g},self.owner)["members"] if m["id"]==self.a)
        self.assertTrue(detail()["online"])
        self.assertFalse(detail()["verified"])
        s=self.call(self.ak,"status")
        with self.assertRaises(Problem):
            self.call(self.ak,"check",challenge="wrong")
        self.assertFalse(detail()["verified"])
        self.call(self.ak,"check",challenge=s["challenge"])
        self.next_delivery(self.ak)
        self.assertTrue(detail()["verified"])
        self.assertTrue(detail()["automatic"])
        self.now+=ONLINE+1
        self.assertFalse(detail()["online"])
        self.assertTrue(detail()["verified"])

    def test_configured_https_origins(self):
        self.assertEqual(BASE,origin(BASE+"/"))
        for u in ("http://internal.example", "https://localhost.", "https://127.1", "https://[::1]",
                  "https://[::ffff:127.0.0.1]","https://0.0.0.0","https://user@relay.test","https://relay.test/path",
                  "https://relay.test?secret=1","https://relay.test#fragment"):
            with self.subTest(u=u),self.assertRaises(Problem):
                origin(u)

    def test_lan_addresses_and_existing_invitations(self):
        for address in ("172.23.5.8", "192.168.4.2", "10.2.1.8"):
            self.assertEqual(address, lan_ipv4(address))
        for address in ("127.0.0.1", "localhost", "0.0.0.0", "203.0.113.1", "192.168.1.2:7810"):
            with self.subTest(address=address), self.assertRaises(Problem):
                lan_ipv4(address)
        shared = self.relay.owner("channel_link", {"group":self.g}, self.owner)
        personal = self.invite("新成员")
        self.relay.public_url = "http://192.168.4.2:7810"
        for mode, issued in (("channel", shared), ("personal", personal)):
            fragment = issued["url"].split("#")[1]
            preview = self.relay.channel("preview", {"type":mode, "code":fragment.split("=")[1]})
            self.assertEqual(self.relay.public_url + "/join#" + fragment, preview["url"])

    def test_shared_link_concurrency_expiry_and_stop(self):
        fields = {"group":self.g}
        link = self.relay.owner("channel_link",fields,self.owner)
        self.assertEqual(link,self.relay.owner("channel_link",fields,self.owner))
        code = link["url"].split("#c=")[1]
        creds = [token() for _ in range(3)]
        payload = {"code":code,"agent":"claude"}
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            members = list(pool.map(lambda key:self.relay.channel("join",payload,key), creds))
        self.assertEqual({"Claude Code","Claude Code 2","Claude Code 3"}, {m["name"] for m in members})
        self.assertEqual(members[0], self.relay.channel("join",payload,creds[0]))
        self.assertNotIn(code, str(self.relay.owner("detail",fields,self.owner)))
        self.assertNotIn(code, str(self.call(self.ak,"members")))
        with self.assertRaises(Problem):
            self.relay.channel("join",payload,code)
        self.relay.owner("disable_link",fields,self.owner)
        with self.assertRaises(Problem):
            self.relay.channel("preview",payload)
        self.assertEqual(self.g,self.call(creds[0],"status")["group"])
        fresh = self.relay.owner("channel_link",fields,self.owner)
        self.assertNotEqual(link["url"],fresh["url"])
        self.now=fresh["expires"]
        with self.assertRaises(Problem):
            self.relay.channel("join",{"code":fresh["url"].split("#c=")[1],"agent":"codex"},token())

    def test_failed_delivery_retry_and_host_fanout(self):
        send = {"group":self.g,"body":"请核对发布分支","request":ident()}
        discussion = self.relay.owner("speak",send,self.owner)["id"]
        delivered = self.next_delivery(self.ak)
        self.call(self.ak,"fail",message=delivered["id"],ticket=delivered["ticket"],reason="等待开发者确认")
        self.relay.owner("requeue",{"group":self.g,"message":discussion},self.owner)
        retry = self.next_delivery(self.ak)
        self.assertNotEqual(delivered["ticket"],retry["ticket"])
        self.assertNotEqual(retry["id"],self.next_delivery(self.bk)["id"])
        detail = self.relay.owner("detail",{"group":self.g},self.owner)
        self.assertEqual([discussion],[msg["id"] for msg in detail["history"]["messages"]])
        summary=self.relay.owner("groups",{},self.owner)["groups"][0]
        self.assertEqual(("host",send["body"]),(summary["last_agent"],summary["last_body"]))

    def test_selected_members_answer_one_public_topic_independently(self):
        _, observer = self.member("旁观")
        fields = {"group": self.g, "body": "@前端 @后端 请分别确认", "request": ident(),
                  "targets": [self.a, self.b, self.a]}
        topic = self.relay.owner("group_question", fields, self.owner)["id"]
        def history():
            return self.relay.owner("history", {"group": self.g, "topic": topic}, self.owner)["messages"]
        def receipts():
            return [(r["member"], r["status"]) for r in history()[0]["recipients"]]

        self.assertEqual(["group_question"], [m["kind"] for m in history()])
        self.assertEqual([(self.a, "pending"), (self.b, "pending")], receipts())
        first = self.next_delivery(self.ak)
        self.assertEqual(topic, first["topic"])
        self.assertEqual([(self.a, "received"), (self.b, "pending")], receipts())
        self.assertIsNone(self.next_delivery(observer))
        self.assertEqual([], self.call(observer, "inbox")["messages"])
        with self.assertRaises(Problem):
            self.call(observer, "receive", message=first["id"])
        reply = dict(kind="answer", parent=first["id"], ticket=first["ticket"], body="前端已确认", request=ident())
        answer = self.call(self.ak, "post", **reply)
        self.assertEqual(answer, self.call(self.ak, "post", **reply))
        self.assertEqual(topic, answer["topic"])
        self.assertEqual([(self.a, "replied"), (self.b, "pending")], receipts())

        self.now += ONLINE + 1
        self.relay = Relay(self.root / "server", BASE, lambda: self.now)
        second = self.next_delivery(self.bk, "重连")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(topic, second["topic"])
        self.answer(second["id"], "重连")
        self.assertEqual([(self.a, "replied"), (self.b, "replied")], receipts())
        self.assertEqual([("group_question", topic), ("answer", topic), ("answer", topic)],
                         [(m["kind"], m["topic"]) for m in self.call(observer, "history")["messages"]])

    def test_group_question_validates_all_targets_and_retries_atomically(self):
        other = self.relay.owner("create", {"name": "其他频道"}, self.owner)["group"]
        foreign, _ = self.member("其他成员", other)
        left, _ = self.member("已退出")
        self.relay.owner("remove", {"group": self.g, "member": left}, self.owner)
        sender, sender_key = self.member("提问项目")
        fields = dict(kind="group_question", body="请一起核对", request=ident())
        for targets in ([], self.a, [None], [self.a, sender], [self.a, left], [self.a, foreign]):
            with self.subTest(targets=targets), self.assertRaises(Problem):
                self.call(sender_key, "post", **fields, targets=targets)
        self.assertEqual([], self.call(self.ak, "history")["messages"])
        self.assertIsNone(self.next_delivery(self.ak))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda ids: self.call(sender_key, "post", **fields, targets=ids),
                                    ([self.a, self.b], [self.b, self.a, self.a])))
        self.assertEqual(results[0], results[1])
        with self.assertRaisesRegex(Problem, "请求编号"):
            self.call(sender_key, "post", **fields, targets=[self.b])
        self.relay.owner("remove", {"group": self.g, "member": self.a}, self.owner)
        self.assertEqual(results[0], self.call(sender_key, "post", **fields, targets=[self.a, self.b]))
        root, = self.call(sender_key, "history")["messages"]
        self.assertEqual({self.a: "cancelled", self.b: "pending"}, {r["member"]: r["status"] for r in root["recipients"]})

    def test_web_broadcast_with_mentions_only_updates_timeline(self):
        fields = {"group": self.g, "body": "@前端 @后端 今天十点发布", "request": ident()}
        rev = self.relay.owner("changes", {"group": self.g}, self.owner)["revision"]
        posted = self.relay.owner("broadcast", fields, self.owner)
        self.assertEqual(posted, self.relay.owner("broadcast", fields, self.owner))
        self.assertGreater(self.relay.owner("changes", {"group": self.g}, self.owner)["revision"], rev)
        for key in (self.ak, self.bk):
            self.assertIsNone(self.next_delivery(key))
            self.assertEqual([], self.call(key, "inbox")["messages"])
            note, = self.call(key, "history")["messages"]
            self.assertEqual((posted["id"], "broadcast", None, "recorded"),
                             (note["id"], note["kind"], note["target"], note["receipt"]))
        with self.assertRaisesRegex(Problem, "请求编号"):
            self.relay.owner("group_question", {**fields, "targets": [self.a, self.b]}, self.owner)

    def test_personal_history_filters_before_paging_and_can_open_the_whole_topic(self):
        _, spectator_key = self.member("旁听")
        host = self.relay.owner("detail", {"group": self.g}, self.owner)["self"]
        def personal(**fields):
            return self.relay.owner("history", {"group": self.g, "scope": "mine", **fields}, self.owner)
        root = self.call(self.ak, "post", kind="group_question", targets=[host, self.b],
                         body="验收窗口分别确认", request=ident())["id"]
        branch = self.next_delivery(self.bk, "B")
        sibling = self.answer(branch["id"], "B")
        for i in range(45):
            self.call(self.bk, "post", kind="broadcast", body=f"无关进度 {i}", request=ident())
        direct = self.call(self.ak, "post", kind="question", target=host, body="验收窗口请群主确认", request=ident())["id"]
        answer = self.relay.owner("speak", {"group": self.g, "parent": direct, "body": "验收窗口已确认", "request": ident()}, self.owner)["id"]
        sent = self.relay.owner("speak", {"group": self.g, "body": "我发起的讨论", "request": ident()}, self.owner)["id"]
        follow = self.call(self.ak, "post", kind="followup", parent=answer, body="请提供依据", request=ident())["id"]
        self.assertEqual([root, direct, answer, sent, follow], [m["id"] for m in personal()["messages"]])
        first = personal(query="验收窗口", limit=2)
        self.assertTrue(first["more"])
        self.assertEqual([direct, answer], [m["id"] for m in first["messages"]])
        earlier = personal(query="验收窗口", limit=2, before=direct)
        self.assertFalse(earlier["more"])
        self.assertEqual([root], [m["id"] for m in earlier["messages"]])
        self.assertEqual([follow], [m["id"] for m in personal(after=sent)["messages"]])
        self.assertEqual([root, sibling], [m["id"] for m in self.relay.owner("history", {"group": self.g, "topic": root}, self.owner)["messages"]])
        self.assertEqual([], self.call(spectator_key, "history", scope="mine", viewer=host, member=self.a)["messages"])

    def test_quiet_mentions_keep_member_identity_without_creating_a_delivery(self):
        _, spectator_key = self.member("旁听")
        foreign_group = self.relay.owner("create", {"name": "其他频道"}, self.owner)["group"]
        foreign, foreign_key = self.member("其他成员", foreign_group)
        payload = {"group": self.g, "body": "@后端 验收结果", "request": ident()}
        legacy = self.relay.owner("broadcast", payload, self.owner)["id"]
        note = {**payload, "request": ident(), "mentions": [self.b, self.b, foreign]}
        posted = self.relay.owner("broadcast", note, self.owner)
        self.assertEqual(posted, self.relay.owner("broadcast", {**note, "mentions": [foreign, self.b]}, self.owner))
        with self.assertRaisesRegex(Problem, "请求编号"):
            self.relay.owner("broadcast", {**note, "mentions": []}, self.owner)
        self.call(self.bk, "profile", name="发布负责人", role="验收")
        self.call(spectator_key, "profile", name="后端", role="旁听")
        self.assertEqual([posted["id"]], [m["id"] for m in self.call(self.bk, "history", scope="mine")["messages"]])
        self.assertEqual([], self.call(spectator_key, "history", scope="mine")["messages"])
        self.assertEqual([], self.call(foreign_key, "history", scope="mine")["messages"])
        self.assertIsNone(self.next_delivery(self.bk))
        self.assertEqual([], self.call(self.bk, "inbox")["messages"])
        self.assertEqual([legacy, posted["id"]], [m["id"] for m in self.call(self.bk, "history")["messages"]])
        with sqlite3.connect(self.relay.path) as db:
            self.assertEqual([(self.b,)], db.execute("SELECT member FROM message_mentions WHERE mid=?", (posted["id"],)).fetchall())
