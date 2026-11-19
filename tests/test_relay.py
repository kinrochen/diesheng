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

    def test_group_isolation_and_sensitive_fields(self):
        other=self.relay.owner("create",{"name":"其他"},self.owner)["group"]
        _, key=self.member("后端",other)
        q=self.ask()
        with self.assertRaises(Problem):
            self.call(key,"receive",message=q)
        with self.assertRaises(Problem):
            self.call(key,"post",kind="question",request=ident(),target=self.b,body="错群")
        for text in ("/Users/someone/work/api", "Bearer secret-token-1234567890"):
            with self.assertRaises(Problem):
                self.call(self.ak,"post",kind="broadcast",request=ident(),body=text)
        self.assertEqual([],self.call(key,"history")["messages"])

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
        for address in ("172.16.8.29", "192.168.4.2", "10.2.1.8"):
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
