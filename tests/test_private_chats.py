from concurrent.futures import ThreadPoolExecutor
import sqlite3

from agent_group_chat.agent_bindings import OFFER_LIFETIME
from agent_group_chat.common import Problem, token
from agent_group_chat.private_chats import LEASE, RETENTION, SETUP_LEASE
from tests.support import RelayCase


class PrivateChats(RelayCase):
    def owner_action(self, action, **data):
        return self.relay.owner(action, {"group":self.g, **data}, self.owner)

    def human(self, name):
        invite = self.owner_action("human_invite")["url"].split("#invite=")[1]
        _, session = self.relay.member_entry("register",self.g,
            {"code":invite,"name":name,"username":"test-"+token()[:14],"password":"fixture-only-password"},"","")
        return lambda action, **data:self.relay.web_member(action,self.g,data,"",session)

    def binding_code(self, human, agent):
        return human("agent-bind-code",agent=agent)["url"].split("#bind=")[1]

    def runtime(self, public_key, *, listening=True):
        secret = token()
        self.relay.private_agent("enable", {"credential":secret}, public_key)
        if listening:
            self.relay.private_agent("pulse", {}, secret)
        return secret

    def service(self, secret, action, **data):
        return self.relay.private_agent(action, {"session":"solo", **data}, secret)

    def take(self, secret):
        return self.service(secret,"next")["delivery"]

    def answer_private(self, secret, event, body):
        return self.service(secret,"reply",thread=event["thread"],message=event["id"],ticket=event["ticket"],body=body)

    def test_private_topic_picker_and_reference_use_both_members_visibility(self):
        earlier = self.ask()
        recipient, key = self.member("稍后入群的项目")
        title = "v13 接口核对"
        topic = self.owner_action("speak",target=recipient,body=title,request="v13")["topic"]
        newer = self.owner_action("speak",target=recipient,body="v13 的另一项检查",request="v13-2")["topic"]
        update = self.owner_action("broadcast",body="v13 普通同步",request="sync-v13")["id"]
        other = self.relay.owner("create",{"name":"其他频道"},self.owner)["group"]
        other_member, _ = self.member("其他项目",other)
        outside = self.relay.owner("speak",{"group":other,"target":other_member,"body":title,"request":"outside"},
                                   self.owner)["topic"]
        first = self.owner_action("private-topics",target=recipient,query="v13",limit=1)
        self.assertEqual([newer],[t["id"] for t in first["topics"]])
        self.assertTrue(first["more"])
        page = self.owner_action("private-topics",target=recipient,query="v13",before=first["topics"][0]["last_id"],limit=1)
        self.assertEqual([topic],[t["id"] for t in page["topics"]])
        self.assertFalse(page["more"])
        for unavailable in (earlier, update, outside):
            self.assertEqual([],self.owner_action("private-topics",target=recipient,query=f"#{unavailable}")["topics"])
            with self.assertRaisesRegex(Problem,"已不可关联"):
                self.owner_action("private-start",target=recipient,topic=unavailable,body="测试",request="unavailable")

        initial = self.owner_action("changes")
        self.now += 60
        payload = {"target":recipient,"topic":topic,"body":"请私下核对样本","request":"link-v13"}
        created = self.owner_action("private-start",**payload)
        self.assertEqual(created,self.owner_action("private-start",**payload))
        with self.assertRaisesRegex(Problem,"发送编号"):
            self.owner_action("private-start",**{**payload,"topic":newer})
        private = self.owner_action("private-read",thread=created["thread"])
        self.assertEqual(title,private["thread"]["title"])
        reference = {"id":topic,"title":title}
        self.assertEqual(reference,private["topic"])
        job = self.take(self.runtime(key,listening=False))
        self.assertEqual(reference,job["topic"])
        self.assertEqual([],job["history"])
        self.assertEqual(initial,self.owner_action("changes"))
        self.assertEqual([],self.owner_action("history",query=payload["body"])["messages"])
        self.now += RETENTION - 59
        self.relay.prune()
        self.assertIsNone(self.owner_action("private-read",thread=created["thread"])["topic"])

    def test_first_private_send_reminds_only_recipient_once(self):
        alice, bob = self.human("小雨"), self.human("小江")
        initial = self.owner_action("changes")
        thread = alice("private-start",target=self.b,title="私聊主题",body="私聊正文",request="alice")["thread"]
        other = bob("private-start",target=self.b,body="另一条私聊",request="bob")["thread"]
        self.assertEqual("queued",alice("private-read",thread=thread)["setup_status"])
        self.assertNotIn("notice",self.call(self.bk,"next","legacy"))
        self.assertNotIn("notice",self.call(self.ak,"next","unrelated",private_setup=True))
        offered = self.call(self.bk,"next","first",private_setup=True)
        self.assertIsNone(offered["delivery"])
        notice = offered["notice"]
        self.assertEqual({"kind","id"},set(notice))
        self.assertEqual("private_setup",notice["kind"])
        self.assertNotIn("notice",self.call(self.bk,"next","second",private_setup=True))
        with self.assertRaises(Problem) as hidden:
            self.call(self.ak,"private-setup-response","first",id=notice["id"],decision="notified")
        self.assertEqual(404,hidden.exception.status)
        self.now += SETUP_LEASE + 1
        self.assertEqual(notice,self.call(self.bk,"next","second",private_setup=True)["notice"])
        with self.assertRaises(Problem) as stale:
            self.call(self.bk,"private-setup-response","first",id=notice["id"],decision="notified")
        self.assertEqual(409,stale.exception.status)
        self.call(self.bk,"private-setup-response","second",id=notice["id"],decision="notified")
        self.assertEqual("notified",bob("private-read",thread=other)["setup_status"])
        self.call(self.bk,"private-setup-response","second",id=notice["id"],decision="defer")
        alice("private-send",thread=thread,body="补充消息",request="again")
        self.assertNotIn("notice",self.call(self.bk,"next","second",private_setup=True))
        self.assertEqual("deferred",alice("private-read",thread=thread)["setup_status"])
        self.assertEqual(initial,self.owner_action("changes"))
        self.assertEqual([],self.owner_action("history")["messages"])
        recipient = self.runtime(self.bk,listening=False)
        self.assertEqual("私聊正文",self.take(recipient)["body"])
        read = alice("private-read",thread=thread)
        self.assertEqual(("online",None),(read["state"],read["setup_status"]))

    def test_existing_private_queue_can_be_reminded_by_sender(self):
        thread = self.owner_action("private-start",target=self.b,body="旧私聊",request="existing")["thread"]
        with sqlite3.connect(self.relay.path) as db:
            db.execute("DELETE FROM private_setup_notices")
        self.assertEqual("not_requested",self.owner_action("private-read",thread=thread)["setup_status"])
        self.assertNotIn("notice",self.call(self.bk,"next","receiver",private_setup=True))
        with self.assertRaises(Problem) as hidden:
            self.human("小雨")("private-remind",thread=thread)
        self.assertEqual(404,hidden.exception.status)
        self.assertEqual("queued",self.owner_action("private-remind",thread=thread)["setup_status"])
        notice = self.call(self.bk,"next","receiver",private_setup=True)["notice"]
        self.call(self.bk,"private-setup-response","receiver",id=notice["id"],decision="notified")
        self.assertEqual("notified",self.owner_action("private-remind",thread=thread)["setup_status"])
        self.assertNotIn("notice",self.call(self.bk,"next","receiver",private_setup=True))

    def test_browser_private_reads_and_public_side_effects(self):
        alice, bob = self.human("小雨"), self.human("小江")
        third = self.runtime(self.ak)
        initial = self.owner_action("changes")
        created = alice("private-start",target=self.b,title="样本核对",body="私下的样本编号 R42",request="new-sample")
        self.assertEqual(created,alice("private-start",target=self.b,title="样本核对",body="私下的样本编号 R42",request="new-sample"))
        thread = created["thread"]
        queued = alice("private-read",thread=thread)
        self.assertEqual(("offline",False,"pending"),(queued["state"],queued["listener_enabled"],queued["messages"][0]["status"]))
        self.assertIsNone(self.next_delivery(self.bk))
        self.assertEqual([],self.call(self.bk,"context")["recent"])
        self.assertEqual([],self.call(self.bk,"inbox")["messages"])
        self.assertEqual([],self.owner_action("topics")["topics"])
        self.assertEqual([],self.owner_action("history",query="R42",scope="mine")["messages"])
        self.assertEqual([],self.owner_action("private-list")["threads"])
        self.assertEqual([],bob("private-list")["threads"])
        self.assertEqual([],self.service(third,"list")["threads"])
        for get in (lambda: self.owner_action("private-read",thread=thread),
                    lambda: bob("private-read",thread=thread),
                    lambda: self.service(third,"read",thread=thread)):
            with self.assertRaisesRegex(Problem,"不可用") as hidden:
                get()
            self.assertEqual(404,hidden.exception.status)
        with self.assertRaises(Problem):
            self.service(self.bk,"read",thread=thread)
        target = self.runtime(self.bk,listening=False)
        with self.assertRaises(Problem):
            self.call(target,"context")
        with self.assertRaisesRegex(Problem,"运行器"):
            self.service(target,"send",thread=thread,body="通过签收答复同事",request="human-direct")
        job = self.take(target)
        self.assertEqual([],job["history"])
        reply = self.answer_private(target,job,"R42 使用 v12 规则")
        self.assertEqual(reply,self.answer_private(target,job,"R42 使用 v12 规则"))
        self.assertEqual(initial,self.owner_action("changes"))
        self.assertEqual(1,alice("private-list")["threads"][0]["unread"])
        messages = alice("private-read",thread=thread,seen=True)["messages"]
        self.assertEqual([created["id"],reply["id"]],[m["id"] for m in messages])
        self.assertEqual(0,alice("private-list")["threads"][0]["unread"])
        share = {"thread":thread,"message":reply["id"],"body":"已核对规则版本","request":"share-one"}
        with self.assertRaises(Problem):
            bob("private-share",**share)
        self.assertEqual(alice("private-share",**share),alice("private-share",**share))
        public, = self.owner_action("history")["messages"]
        self.assertEqual(("broadcast","已核对规则版本"),(public["kind"],public["body"]))
        self.assertIsNone(self.next_delivery(self.bk))

    def test_agent_initiates_and_context_is_scoped_to_one_conversation(self):
        caller = self.runtime(self.ak,listening=False)
        first = self.service(caller,"start",target=self.b,body="私聊甲",request="thread-a")
        second = self.service(caller,"start",target=self.b,body="私聊乙",request="thread-b")
        self.service(caller,"send",thread=first["thread"],body="甲续聊",request="again")
        recipient = self.runtime(self.bk,listening=False)
        job = self.take(recipient)
        self.assertEqual(first["thread"],job["thread"])
        self.answer_private(recipient,job,"甲的答复")
        job = self.take(recipient)
        self.assertEqual(second["thread"],job["thread"])
        self.assertEqual([],job["history"])
        self.answer_private(recipient,job,"乙的答复")
        job = self.take(recipient)
        self.assertEqual(["私聊甲","甲的答复"],[m["body"] for m in job["history"]])
        self.assertEqual(1,len(self.service(caller,"read",thread=first["thread"],before=job["id"],limit=1)["messages"]))
        self.assertIsNone(self.take(caller))
        with self.assertRaisesRegex(Problem,"原运行器"):
            self.relay.private_agent("enable",{"credential":token()},self.bk)

    def test_queue_retry_revocation_and_retention(self):
        alice = self.human("小雨")
        secret = self.runtime(self.bk)
        self.now+=61
        request = alice("private-start",target=self.b,body="离线期间的私聊",request="offline")
        thread = request["thread"]
        self.assertEqual("offline",alice("private-read",thread=thread)["state"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = [job for job in pool.map(lambda i:self.service(secret,"next",session=str(i))["delivery"],range(4)) if job]
        lease, = claimed
        self.now+=LEASE+1
        current = self.take(secret)
        self.assertEqual(lease["id"],current["id"])
        self.assertNotEqual(lease["ticket"],current["ticket"])
        with self.assertRaises(Problem):
            self.answer_private(secret,lease,"旧签收")
        self.service(secret,"fail",thread=thread,message=current["id"],ticket=current["ticket"])
        alice("private-retry",thread=thread,message=current["id"])
        current = self.take(secret)
        self.owner_action("pause",paused=True)
        self.assertTrue(alice("private-read",thread=thread)["paused"])
        with self.assertRaises(Problem):
            self.answer_private(secret,current,"频道已暂停")
        self.owner_action("pause",paused=False)
        self.owner_action("remove",member=self.b)
        visible = alice("private-read",thread=thread)
        self.assertEqual("left",visible["state"])
        self.assertEqual("cancelled",visible["messages"][0]["status"])
        with self.assertRaises(Problem):
            alice("private-send",thread=thread,body="继续",request="removed")
        with self.assertRaises(Problem):
            self.service(secret,"read",thread=thread)
        new_member, rejoined_key = self.member("后端")
        rejoined = self.runtime(rejoined_key)
        self.assertEqual([],self.service(rejoined,"list")["threads"])
        self.owner_action("private-start",target=new_member,body="应按保留期清除的记录",request="expiry")
        self.now+=RETENTION+1
        self.assertEqual([],self.owner_action("private-list")["threads"])
        self.relay.prune()

    def test_binding_requires_the_selected_agent_and_cannot_replace_an_owner(self):
        alice, bob = self.human("小雨"), self.human("小江")
        old = self.binding_code(alice,self.a)
        self.now += OFFER_LIFETIME + 1
        with self.assertRaisesRegex(Problem,"过期"):
            self.call(self.ak,"bind-human",code=old)
        alice_code = self.binding_code(alice,self.a)
        bob_code = self.binding_code(bob,self.a)
        with self.assertRaises(Problem) as wrong_agent:
            self.call(self.bk,"bind-human",code=alice_code)
        self.assertEqual(410,wrong_agent.exception.status)
        preview = self.call(self.ak,"binding-preview",code=alice_code)
        self.assertEqual("小雨",preview["human"]["name"])
        self.assertEqual([],alice("history",scope="mine")["my_agents"])
        self.assertEqual(preview,self.call(self.ak,"bind-human",code=alice_code))
        self.assertEqual(preview,self.call(self.ak,"bind-human",code=alice_code))
        self.assertEqual([self.a],alice("detail",scope="mine")["history"]["my_agents"])
        self.assertEqual([],bob("history",scope="mine")["my_agents"])
        with self.assertRaises(Problem):
            self.call(self.ak,"bind-human",code=bob_code)
        with self.assertRaises(Problem):
            self.binding_code(self.owner_action,self.a)
        with self.assertRaises(Problem):
            self.owner_action("agent-unbind",agent=self.a)
        self.call(self.ak,"unbind-human")
        with self.assertRaises(Problem):
            self.call(self.ak,"bind-human",code=alice_code)
        self.call(self.ak,"bind-human",code=self.binding_code(bob,self.a))
        bob("leave")
        self.call(self.ak,"bind-human",code=self.binding_code(self.owner_action,self.a))
        self.assertEqual([self.a],self.owner_action("history",scope="mine")["my_agents"])

    def test_personal_feed_includes_bound_agents_in_search_and_pagination(self):
        alice = self.human("小雨")
        human = alice("groups")["self"]["id"]
        own = alice("broadcast",body="我发出的更新",request="mine")["id"]
        sent = self.call(self.ak,"post",kind="broadcast",body="Agent 发出的更新",request="agent-update")["id"]
        inbound = self.call(self.bk,"post",kind="question",target=self.a,body="给 Agent 的问题",request="question")["id"]
        multi = self.owner_action("group_question",targets=[self.a,self.b],body="多人问题",request="group-question")["id"]
        mention = self.call(self.bk,"post",kind="broadcast",body="仅提到 Agent",mentions=[self.a],request="cc-agent")["id"]
        direct_mention = self.call(self.bk,"post",kind="broadcast",body="仅提到同事",mentions=[human],request="cc-human")["id"]
        unrelated = self.call(self.bk,"post",kind="broadcast",body="另一位项目的更新",request="unrelated")["id"]
        def selected(**criteria):
            return [m["id"] for m in alice("history",scope="mine",**criteria)["messages"]]
        self.assertEqual([own,direct_mention],selected())
        self.call(self.ak,"bind-human",code=self.binding_code(alice,self.a))
        self.assertEqual([own,sent,inbound,multi,mention,direct_mention],selected())
        self.assertEqual([own,sent,inbound],selected(before=multi,limit=3))
        self.assertEqual([sent,inbound],selected(query="Agent",after=own,limit=2))
        self.assertEqual([mention],selected(query="仅提到 Agent"))
        self.call(self.bk,"bind-human",code=self.binding_code(alice,self.b))
        self.assertEqual([own,sent,inbound,multi,mention,direct_mention,unrelated],selected())
        alice("agent-unbind",agent=self.b)
        alice("agent-unbind",agent=self.a)
        self.assertEqual([own,direct_mention],selected())

    def test_bound_private_history_is_read_only_and_rechecked_after_agent_leaves(self):
        alice, bob = self.human("小雨"), self.human("小江")
        _, third = self.member("测试")
        sender, receiver, stranger = (self.runtime(key) for key in (self.ak,self.bk,third))
        started = self.service(sender,"start",target=self.b,title="Agent 主动发起",body="内部核对",request="agent-start")
        thread = started["thread"]
        reply = self.answer_private(receiver,self.take(receiver),"核对完毕")
        direct = alice("private-start",target=self.a,body="我自己的会话",request="direct")["thread"]
        inbound = self.service(stranger,"start",target=self.a,body="发给 Agent 的私聊",request="agent-inbound")["thread"]
        self.service(receiver,"start",target="测试",body="无关私聊",request="unrelated")
        self.assertEqual([direct],[t["id"] for t in alice("private-list")["threads"]])
        self.call(self.ak,"bind-human",code=self.binding_code(alice,self.a))
        self.assertEqual([inbound,direct,thread],[t["id"] for t in alice("private-list")["threads"]])
        self.assertNotIn("via_agent",alice("private-read",thread=direct))
        item, = alice("private-list",peer=self.b,limit=1)["threads"]
        self.assertEqual((thread,self.a,2),(item["id"],item["via_agent"]["id"],item["unread"]))
        read = alice("private-read",thread=thread,seen=True)
        self.assertEqual(self.a,read["via_agent"]["id"])
        self.assertEqual([started["id"],reply["id"]],[m["id"] for m in read["messages"]])
        self.assertTrue(all(not m["own"] for m in read["messages"]))
        self.assertEqual(0,alice("private-list",peer=self.b)["threads"][0]["unread"])
        self.assertEqual(1,next(t for t in self.service(sender,"list")["threads"] if t["id"]==thread)["unread"])
        for act, action in ((bob,"private-read"),(self.owner_action,"private-read"),
                            (alice,"private-send"),(alice,"private-share"),(alice,"private-retry"),(alice,"private-remind")):
            with self.assertRaises(Problem) as denied:
                act(action,thread=thread,message=reply["id"],body="未授权",request="invalid")
            self.assertEqual(404,denied.exception.status)
        self.call(self.ak,"leave")
        with self.assertRaises(Problem) as ended:
            alice("private-read",thread=thread)
        self.assertEqual(404,ended.exception.status)
        self.assertEqual([direct],[t["id"] for t in alice("private-list")["threads"]])
