from agent_group_chat.common import Problem
from agent_group_chat.relay import RESPONSE_TIMEOUT
from tests.support import RelayCase


class Topics(RelayCase):
    def take(self, key, sid="listener"):
        return self.call(key, "next", sid, collaborate=True)["delivery"]

    def turn(self, key, event, decision, sid="listener", **fields):
        return self.call(key, "turn", sid, message=event["id"], ticket=event["ticket"], decision=decision, **fields)

    def directory(self, **filters):
        return self.relay.owner("topics", {"group": self.g, **filters}, self.owner)

    def test_peer_returns_through_nested_calls_and_retries(self):
        c, ck = self.member("测试")
        root = self.relay.owner("group_question", {"group":self.g,"targets":[self.a,self.b],
                                "body":"v13 是否能发布？","request":"root"}, self.owner)["id"]
        original = self.take(self.ak)
        self.assertEqual(root, original["topic"])
        self.assertEqual("active", self.directory(category="group_question")["topics"][0]["status"])
        call = self.turn(self.ak, original, "ask", target=c, body="请核对 v13 报告")
        self.assertEqual(call,self.turn(self.ak,original,"ask",target=c,body="请核对 v13 报告"))
        with self.assertRaises(Problem):
            self.turn(self.bk, original, "ask", target=c, body="不是我的领取")
        with self.assertRaises(Problem):
            self.turn(self.ak, original, "ask", target=c, body="修改已提交的决定")
        self.assertIsNone(self.take(self.ak))
        sub = self.take(ck)
        answer = self.turn(ck, sub, "reply", body="找到 v12 报告")
        self.assertIsNone(self.next_delivery(self.ak,"legacy"))
        with self.assertRaisesRegex(Problem,"协作"):
            self.call(self.ak,"receive","legacy",message=answer["id"])
        with self.assertRaisesRegex(Problem,"协作"):
            self.call(self.ak,"post",kind="followup",parent=answer["id"],body="不带票据追问",request="old-follow")
        review = self.take(self.ak,"restarted")
        self.assertEqual((root,original["id"],call["id"]),(review["topic"],review["assignment"],review["parent"]))
        self.assertIn("v13 是否能发布？",[m["body"] for m in review["background"]])
        self.assertIsNone(self.call(self.ak,"next","restarted",collaborate=True,awaiting_ticket=review["ticket"])["delivery"])
        with self.assertRaises(Problem):
            self.turn(self.ak,review,"finish",sid="restarted")
        self.turn(self.ak,review,"followup",sid="restarted",body="请核对 v13")
        follow = self.take(ck)
        # A peer may itself consult another project before returning its result.
        self.turn(ck,follow,"ask",target=self.b,body="v13 的实际批次？")
        sibling = self.take(self.bk)
        self.turn(self.bk,sibling,"reply",body="我的原始分支已完成")
        nested = self.take(self.bk)
        self.turn(self.bk,nested,"reply",body="批次 13")
        result = self.take(ck)
        self.assertEqual(0,result["remaining_calls"])
        with self.assertRaisesRegex(Problem,"上限"):
            self.turn(ck,result,"followup",body="再来一轮")
        self.turn(ck,result,"reply",body="v13 已通过")
        self.turn(self.ak,self.take(self.ak),"reply",body="可以发布 v13")
        self.assertIsNone(self.take(self.ak))
        self.assertIsNone(self.take(self.bk))
        self.assertIsNone(self.take(ck))
        records = self.relay.owner("history", {"group":self.g,"topic":root},self.owner)["messages"]
        self.assertEqual({root},{m["topic"] for m in records})
        self.assertEqual("可以发布 v13",records[-1]["body"])
        self.assertEqual(["replied","replied"],[p["status"] for p in records[0]["recipients"]])
        summary, = self.directory(category="collaboration",status="answered")["topics"]
        self.assertEqual(root,summary["id"])
        self.assertEqual({"前端","后端","测试","群主"},set(summary["participants"]))
        self.assertEqual([],self.directory(status="active")["topics"])

    def test_standalone_answer_can_finish_without_waking_the_sender(self):
        q=self.ask()
        self.turn(self.bk,self.take(self.bk),"reply",body="可以")
        review=self.take(self.ak)
        self.assertEqual(q,review["topic"])
        self.assertIsNone(review["assignment"])
        with self.assertRaises(Problem):
            self.turn(self.ak,review,"reply",body="收到")
        self.turn(self.ak,review,"finish")
        self.assertIsNone(self.take(self.ak))
        self.assertIsNone(self.take(self.bk))
        self.assertEqual(2,len(self.call(self.ak,"history",topic=q)["messages"]))

    def test_peer_leaves_upstream_can_choose_again(self):
        c, ck=self.member("测试")
        q=self.ask()
        first=self.take(self.bk)
        self.turn(self.bk,first,"ask",target=c,body="核对结果")
        self.call(ck,"leave")
        pending=self.take(self.bk)
        self.assertEqual(q,pending["id"])
        self.assertNotEqual(first["ticket"],pending["ticket"])
        with self.assertRaises(Problem):
            self.turn(self.bk,first,"reply",body="过时的签收")
        self.turn(self.bk,pending,"reply",body="测试已离开，当前无法确认")

    def test_removing_root_caller_releases_nested_peer_review(self):
        c, ck = self.member("测试")
        d, dk = self.member("样本")
        self.ask()
        self.turn(self.bk,self.take(self.bk),"ask",target=c,body="核对报告")
        self.turn(ck,self.take(ck),"ask",target=d,body="核对样本")
        self.turn(dk,self.take(dk),"reply",body="样本确认")
        abandoned = self.take(ck)
        self.relay.owner("remove",{"group":self.g,"member":self.a},self.owner)
        with self.assertRaises(Problem):
            self.turn(ck,abandoned,"reply",body="旧问题的结果")
        new = self.relay.owner("speak",{"group":self.g,"target":c,"body":"另一个问题","request":"else"},self.owner)
        self.assertEqual(new["id"],self.take(ck)["id"])

    def test_slow_review_keeps_its_ticket_and_can_resume(self):
        q=self.ask()
        self.answer(q)
        review=self.take(self.ak)
        self.now+=RESPONSE_TIMEOUT+1
        new=self.relay.owner("speak",{"group":self.g,"target":self.a,"body":"独立问题","request":"new"},self.owner)
        self.assertIsNone(self.call(self.ak,"next","listener",collaborate=True,awaiting_ticket=review["ticket"])["delivery"])
        resumed=self.take(self.ak)
        self.assertEqual(review["ticket"],resumed["ticket"])
        self.turn(self.ak,resumed,"finish")
        self.assertEqual(new["id"],self.take(self.ak)["id"])

    def test_directory_filters_before_paging_and_excludes_broadcasts(self):
        first=self.ask()
        for _ in range(5):
            self.ask()
        self.call(self.bk,"post",kind="broadcast",body="频道公告",request="notice")
        page=self.directory(limit=2,status="pending")
        self.assertTrue(page["more"])
        next_page=self.directory(before=page["topics"][-1]["last_id"],limit=10)
        self.assertEqual(4,len(next_page["topics"]))
        self.assertEqual(first,next_page["topics"][-1]["id"])
        answer = self.answer(first)
        self.assertEqual(first,self.directory(status="active")["topics"][0]["id"])
        self.turn(self.ak,self.take(self.ak),"finish")
        self.call(self.ak,"accept",answer=answer)
        self.assertEqual(first,self.directory(status="answered")["topics"][0]["id"])
        other=self.relay.owner("create",{"name":"独立频道"},self.owner)["group"]
        self.assertEqual([],self.relay.owner("topics",{"group":other},self.owner)["topics"])
