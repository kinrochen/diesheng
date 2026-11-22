"""Private conversations and their separate delivery queue."""
import json
import re

from .agent_bindings import CONTROLLED
from .common import Problem, digest, ident, integer, rows, string, token
from .topic_index import list_topics, topic_reference

RETENTION = 30 * 86400
RUNNER_ONLINE = 60
LEASE = 180
SETUP_LEASE = 90
BROWSER = {"private-list", "private-read", "private-topics", "private-start", "private-send", "private-retry", "private-share", "private-remind"}
PARTICIPANT = {"list", "read", "topics", "start", "send", "retry", "remind"}
READABLE = f"""WITH managed_agents AS ({CONTROLLED}), readable AS (
    SELECT t.*, CASE WHEN t.starter=:viewer THEN t.starter WHEN t.peer=:viewer THEN t.peer
        WHEN t.starter IN (SELECT id FROM managed_agents) THEN t.starter ELSE t.peer END AS subject
    FROM private_threads t WHERE t.gid=:gid AND (t.starter=:viewer OR t.peer=:viewer
        OR t.starter IN (SELECT id FROM managed_agents) OR t.peer IN (SELECT id FROM managed_agents))
)"""


def _missing():
    return Problem("这段私聊不可用或已过保留期", 404)


def _read_access(actor):
    return {"viewer":actor["id"],"gid":actor["gid"],
            "human":actor["id"] if actor["agent"] in ("human","host") else None}


def _availability(active, seen, paused, now):
    if not active:
        return "left"
    if seen is None:
        return "offline"
    if paused:
        return "paused"
    return "online" if seen > now - RUNNER_ONLINE else "offline"


def enable(db, member, data):
    secret = string(data, "credential", 100)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,100}", secret):
        raise Problem("请为私聊运行器生成独立凭据")
    key = digest(secret)
    if db.execute("SELECT 1 FROM members WHERE key_hash=?", (key,)).fetchone():
        raise Problem("请使用与公开成员凭据不同的私聊凭据")
    existing = db.execute("SELECT key_hash FROM private_runners WHERE member=?", (member,)).fetchone()
    if existing and existing[0] != key:
        raise Problem("该项目已绑定私聊运行器，请使用原运行器凭据", 409)
    db.execute("INSERT OR IGNORE INTO private_runners(member,key_hash) VALUES (?,?)", (member, key))
    if not db.execute("SELECT 1 FROM private_runners WHERE member=? AND key_hash=?", (member,key)).fetchone():
        raise Problem("请为这个项目生成独立的私聊凭据", 409)
    return {"enabled": True}


def _agents(db, actor, now):
    agents = rows(db.execute("""SELECT m.id,m.name,m.agent,m.role,m.paused,r.seen
        FROM members m LEFT JOIN private_runners r ON r.member=m.id
        WHERE m.gid=? AND m.active=1 AND m.id!=? AND m.agent NOT IN ('host','human')
        ORDER BY m.name,m.id""", (actor["gid"], actor["id"])))
    for member in agents:
        seen = member.pop("seen")
        member["state"] = _availability(True, seen, member.pop("paused"), now)
        member["listener_enabled"] = seen is not None
    return agents


def _thread(db, actor, thread, now):
    item = db.execute("""SELECT t.id,t.title,p.id AS peer,p.name,p.agent,p.role,p.paused,p.active,r.seen
        FROM private_threads t JOIN members p ON p.id=CASE WHEN t.starter=? THEN t.peer ELSE t.starter END
        LEFT JOIN private_runners r ON r.member=p.id
        WHERE t.gid=? AND t.id=? AND (t.starter=? OR t.peer=?)
          AND EXISTS(SELECT 1 FROM private_messages m WHERE m.thread=t.id AND m.created>=?)""",
                      (actor["id"], actor["gid"], thread, actor["id"], actor["id"], now-RETENTION)).fetchone()
    if not item:
        raise _missing()
    return item


def _recipient(db, actor, target):
    member = db.execute("""SELECT id,start_id FROM members
        WHERE gid=? AND active=1 AND agent NOT IN ('human','host') AND id!=?
          AND (id=? OR name=?) ORDER BY id=? DESC LIMIT 1""",
                        (actor["gid"],actor["id"],target,target,target)).fetchone()
    if not member:
        raise Problem("请选择本频道的另一位项目 Agent", 404)
    return member


def _linked_topic(db, actor, thread, now):
    link = db.execute("SELECT topic FROM private_topic_links WHERE thread=?", (thread,)).fetchone()
    return topic_reference(db, actor["gid"], actor["start_id"], link["topic"], now-RETENTION) if link else None


def _queue_setup(db, recipient, now):
    db.execute("""INSERT INTO private_setup_notices(member,id,created)
        SELECT ?,?,? WHERE NOT EXISTS(SELECT 1 FROM private_runners WHERE member=? AND seen IS NOT NULL)
        ON CONFLICT(member) DO UPDATE SET id=excluded.id,created=excluded.created,
          sid=NULL,lease_until=NULL,notified=NULL,deferred=NULL WHERE private_setup_notices.created<?""",
               (recipient,ident(),now,recipient,now-RETENTION))


def _has_pending(db, thread, sender, recipient, now):
    return db.execute("""SELECT 1 FROM private_messages m JOIN private_deliveries d ON d.message=m.id
        WHERE m.thread=? AND m.sender=? AND d.recipient=? AND m.created>=? AND d.status='pending' LIMIT 1""",
                      (thread,sender,recipient,now-RETENTION)).fetchone() is not None


def _setup_status(db, thread, actor, now):
    if not thread["active"] or thread["seen"] is not None or not _has_pending(
            db,thread["id"],actor["id"],thread["peer"],now):
        return None
    notice = db.execute("SELECT notified,deferred FROM private_setup_notices WHERE member=? AND created>=?",
                        (thread["peer"],now-RETENTION)).fetchone()
    if not notice:
        return "not_requested"
    if notice["deferred"] is not None:
        return "deferred"
    return "notified" if notice["notified"] is not None else "queued"


def setup_notice(db, member, sid, now):
    notice = db.execute("""SELECT id FROM private_setup_notices WHERE member=? AND created>=?
        AND notified IS NULL AND deferred IS NULL AND (sid IS NULL OR sid=? OR lease_until<=?)
        AND NOT EXISTS(SELECT 1 FROM private_runners WHERE member=? AND seen IS NOT NULL)
        AND EXISTS(SELECT 1 FROM private_deliveries d JOIN private_messages q ON q.id=d.message
            JOIN members s ON s.id=q.sender WHERE d.recipient=? AND d.status='pending' AND q.created>=? AND s.active=1)""",
                        (member,now-RETENTION,sid,now,member,member,now-RETENTION)).fetchone()
    if not notice:
        return None
    db.execute("UPDATE private_setup_notices SET sid=?,lease_until=? WHERE id=?",
               (sid,now+SETUP_LEASE,notice["id"]))
    return {"kind":"private_setup","id":notice["id"]}


def setup_response(db, member, sid, data, now):
    notice_id = string(data,"id")
    decision = string(data,"decision")
    notice = db.execute("SELECT sid,notified FROM private_setup_notices WHERE member=? AND id=? AND created>=?",
                        (member,notice_id,now-RETENTION)).fetchone()
    if not notice or notice["sid"] is None:
        raise Problem("私聊开启提醒不可用",404)
    if decision == "notified":
        if notice["notified"] is None and notice["sid"] != sid:
            raise Problem("私聊开启提醒已由另一会话领取",409)
        db.execute("UPDATE private_setup_notices SET notified=coalesce(notified,?) WHERE id=?",(now,notice_id))
    elif decision == "defer":
        db.execute("""UPDATE private_setup_notices SET notified=coalesce(notified,?),deferred=coalesce(deferred,?)
            WHERE id=?""",(now,now,notice_id))
    else:
        raise Problem("请选择确认收到提醒或暂缓开启")
    return {"decision":decision}


def _directory(db, actor, data, now):
    peer = string(data, "peer", optional=True)
    before = integer(data, "before", low=1) if data.get("before") is not None else None
    limit = min(100, integer(data, "limit", 40))
    threads = rows(db.execute(READABLE + """ SELECT t.id,t.title,p.id AS peer,p.name,p.agent,last.id AS last_id,last.created,
            a.id AS via_id,a.name AS via_name,
            substr(last.body,1,128) AS preview,
            (SELECT count(*) FROM private_messages m WHERE m.thread=t.id AND m.created>=:since AND m.sender!=:viewer
             AND m.id>coalesce(seen.last_read,0)) AS unread
        FROM readable t JOIN members p ON p.id=CASE WHEN t.starter=t.subject THEN t.peer ELSE t.starter END
        LEFT JOIN members a ON a.id=t.subject AND t.subject!=:viewer
        JOIN private_messages last ON last.id=(SELECT max(id) FROM private_messages
                                               WHERE thread=t.id AND created>=:since)
        LEFT JOIN private_reads seen ON seen.thread=t.id AND seen.member=:viewer
        WHERE (:peer='' OR p.id=:peer) AND (:before IS NULL OR last.id<:before)
        ORDER BY last.id DESC LIMIT :limit""", {**_read_access(actor),"since":now-RETENTION,
                                                "peer":peer,"before":before,"limit":limit+1}))
    for thread in threads:
        agent, name = thread.pop("via_id"), thread.pop("via_name")
        if agent:
            thread["via_agent"] = {"id":agent,"name":name}
    return {"threads": threads[:limit], "more": len(threads)>limit, "agents": _agents(db, actor, now),
            "paused": bool(actor["group_paused"])}


def _read(db, actor, data, now):
    thread_id = string(data, "thread")
    access = _read_access(actor)
    subject = actor
    if access["human"]:
        subject = db.execute(READABLE + """ SELECT m.* FROM readable t JOIN members m ON m.id=t.subject
            WHERE t.id=:thread""", {**access,"thread":thread_id}).fetchone()
        if not subject:
            raise _missing()
    thread = _thread(db, subject, thread_id, now)
    delegated = subject["id"] != actor["id"]
    where, args = ["m.thread=?", "m.created>=?"], [thread["id"], now-RETENTION]
    earlier = data.get("before") is not None
    forward = data.get("after") is not None
    if earlier:
        where.append("m.id<?")
        args.append(integer(data, "before"))
    if forward:
        where.append("m.id>?")
        args.append(integer(data, "after", low=0))
    limit = min(100, integer(data, "limit", 40))
    order = "ASC" if forward else "DESC"
    messages = rows(db.execute(f"""SELECT m.id,m.body,m.created,m.reply_to,s.name AS author,s.agent,s.id=? AS own,d.status
        FROM private_messages m JOIN members s ON s.id=m.sender LEFT JOIN private_deliveries d ON d.message=m.id
        WHERE {' AND '.join(where)} ORDER BY m.id {order} LIMIT ?""", (actor["id"], *args, limit+1)))
    more = len(messages)>limit
    messages = messages[:limit]
    if not forward:
        messages.reverse()
    if data.get("seen") is True and not earlier and messages:
        db.execute("""INSERT INTO private_reads VALUES (?,?,?) ON CONFLICT(thread,member)
            DO UPDATE SET last_read=max(private_reads.last_read,excluded.last_read)""",
                   (thread["id"], actor["id"], messages[-1]["id"]))
    state = _availability(thread["active"], thread["seen"], thread["paused"], now)
    result = {"thread": {k:thread[k] for k in ("id","title","peer","name","agent","role")},
              "topic": _linked_topic(db, subject, thread["id"], now),
              "state": state, "listener_enabled": thread["seen"] is not None,
              "setup_status": None if delegated else _setup_status(db,thread,actor,now),
              "paused": bool(actor["group_paused"]), "messages": messages, "more": more}
    if delegated:
        result["via_agent"] = {"id":subject["id"],"name":subject["name"]}
    return result


def _require_send(actor, *, active):
    if not active:
        raise Problem("对方已离开频道，这段私聊可查看已有记录", 409)
    if actor["group_paused"]:
        raise Problem("频道已暂停，恢复后可以私聊", 409)


def _send(db, actor, action, data, now):
    body = string(data, "body", 6000)
    request = string(data, "request", 128)
    if action == "start":
        target = string(data, "target")
        title = string(data, "title", 80, optional=True)
        topic_id = integer(data, "topic") if data.get("topic") is not None else None
        fields = [action,target,title or "私聊",body]
        if topic_id is not None:
            fields = [action,target,title,body,topic_id]
        fingerprint = digest(json.dumps(fields))
    else:
        thread = _thread(db, actor, string(data, "thread"), now)
        fingerprint = digest(json.dumps([action,thread["id"],body]))
    found = db.execute("""SELECT id,thread,fingerprint,created FROM private_messages
        WHERE sender=? AND request=?""", (actor["id"], request)).fetchone()
    if found:
        if found["created"] < now-RETENTION:
            raise Problem("原消息已过保留期，请重新发送", 409)
        if found["fingerprint"] != fingerprint:
            raise Problem("这次发送编号已用于其他私聊内容", 409)
        return {"thread": found["thread"], "id": found["id"]}
    if action == "start":
        target = _recipient(db, actor, target)
        _require_send(actor, active=True)
        if topic_id is not None:
            start = max(actor["start_id"], target["start_id"])
            topic = topic_reference(db, actor["gid"], start, topic_id, now-RETENTION)
            if not topic:
                raise Problem("这条话题已不可关联，请重新选择或取消关联", 409)
            if not title:
                title = " ".join(topic["title"].split())[:80].strip()
        thread_id, recipient = ident(), target["id"]
        db.execute("INSERT INTO private_threads VALUES (?,?,?,?,?,?)",
                   (thread_id, actor["gid"], actor["id"], recipient, title or "私聊", now))
        if topic_id is not None:
            db.execute("INSERT INTO private_topic_links(thread,topic) VALUES (?,?)", (thread_id,topic_id))
    else:
        # Agents answer human participants through their signed delivery. Both Agents can send in an Agent-to-Agent thread.
        _require_send(actor, active=thread["active"])
        if thread["agent"] in ("host", "human"):
            raise Problem("请通过私聊运行器答复收到的问题", 409)
        thread_id, recipient = thread["id"], thread["peer"]
    mid = db.execute("""INSERT INTO private_messages(thread,sender,body,created,request,fingerprint)
        VALUES (?,?,?,?,?,?)""", (thread_id,actor["id"],body,now,request,fingerprint)).lastrowid
    db.execute("INSERT INTO private_deliveries(message,recipient,status) VALUES (?,?,'pending')", (mid, recipient))
    _queue_setup(db,recipient,now)
    return {"thread": thread_id, "id": mid}


def participant(db, actor, action, data, now):
    if action == "list":
        return _directory(db, actor, data, now)
    if action == "read":
        return _read(db, actor, data, now)
    if action == "topics":
        target = _recipient(db, actor, string(data, "target"))
        return list_topics(db, actor["gid"], max(actor["start_id"], target["start_id"]), data, since=now-RETENTION)
    if action in ("start", "send"):
        return _send(db, actor, action, data, now)
    if action == "remind":
        thread = _thread(db,actor,string(data,"thread"),now)
        _require_send(actor,active=thread["active"])
        if thread["seen"] is not None or not _has_pending(db,thread["id"],actor["id"],thread["peer"],now):
            raise Problem("这段私聊当前无需开启提醒",409)
        _queue_setup(db,thread["peer"],now)
        return {"setup_status":_setup_status(db,thread,actor,now)}
    if action == "retry":
        thread = _thread(db, actor, string(data, "thread"), now)
        _require_send(actor, active=thread["active"])
        mid = integer(data, "message")
        changed = db.execute("""UPDATE private_deliveries SET status='pending',sid=NULL,ticket_hash=NULL,until=NULL
            WHERE status='failed' AND message=(SELECT id FROM private_messages WHERE id=? AND thread=? AND sender=?
                AND created>=?)""", (mid,thread["id"],actor["id"],now-RETENTION))
        if not changed.rowcount:
            raise Problem("这条消息当前无需重新投递", 409)
        return {"status": "pending"}
    raise Problem("未知的私聊操作", 404)


def share_source(db, actor, data, now):
    thread = _thread(db, actor, string(data, "thread"), now)
    _require_send(actor, active=thread["active"])
    if not db.execute("""SELECT 1 FROM private_messages WHERE id=? AND thread=? AND sender=? AND reply_to IS NOT NULL
           AND created>=?""", (integer(data, "message"), thread["id"], thread["peer"], now-RETENTION)).fetchone():
        raise _missing()


def touch(db, member, now):
    db.execute("UPDATE private_runners SET seen=? WHERE member=?", (now, member))


def claim(db, actor, data, now):
    sid = string(data, "session")
    touch(db, actor["id"], now)
    paused = bool(actor["paused"] or actor["group_paused"])
    if paused:
        return {"delivery":None, "paused":True}
    db.execute("""UPDATE private_deliveries SET status='pending',sid=NULL,ticket_hash=NULL,until=NULL
        WHERE recipient=? AND status='processing' AND until<=?""", (actor["id"], now))
    job = db.execute("""SELECT m.id,m.thread,m.body,t.title,s.name AS author
        FROM private_deliveries d JOIN private_messages m ON m.id=d.message
        JOIN private_threads t ON t.id=m.thread JOIN members s ON s.id=m.sender
        WHERE d.recipient=? AND d.status='pending' AND m.created>=? AND t.gid=?
          AND (t.starter=? OR t.peer=?) AND s.active=1
          AND NOT EXISTS(SELECT 1 FROM private_deliveries running JOIN private_messages q ON q.id=running.message
              WHERE q.thread=t.id AND running.recipient=d.recipient AND running.status='processing')
        ORDER BY m.id LIMIT 1""",
                     (actor["id"], now-RETENTION, actor["gid"], actor["id"], actor["id"])).fetchone()
    if not job:
        return {"delivery":None, "paused":False}
    ticket = token()
    db.execute("""UPDATE private_deliveries SET status='processing',sid=?,ticket_hash=?,until=? WHERE message=?""",
               (sid, digest(ticket), now+LEASE, job["id"]))
    history = rows(db.execute("""SELECT m.id,s.name AS author,s.id=? AS own,substr(m.body,1,1200) AS body
        FROM private_messages m JOIN members s ON s.id=m.sender WHERE m.thread=? AND m.created>=?
        AND (m.id<? OR m.reply_to<?) ORDER BY m.id DESC LIMIT 12""",
                             (actor["id"], job["thread"], now-RETENTION, job["id"], job["id"])))
    return {"delivery":{**dict(job), "ticket":ticket, "history":list(reversed(history)), "role":actor["role"],
                         "topic":_linked_topic(db, actor, job["thread"], now)},
            "paused":False}


def complete(db, actor, action, data, now):
    thread = _thread(db, actor, string(data, "thread"), now)
    mid = integer(data, "message")
    sid, ticket = string(data, "session"), string(data, "ticket", 128)
    job = db.execute("""SELECT d.* FROM private_deliveries d JOIN private_messages m ON m.id=d.message
        WHERE m.id=? AND m.thread=? AND d.recipient=? AND m.sender=? AND m.created>=?""",
                     (mid,thread["id"],actor["id"],thread["peer"],now-RETENTION)).fetchone()
    if not job or job["sid"] != sid or job["ticket_hash"] != digest(ticket):
        raise _missing()
    if action == "reply":
        body = string(data, "body", 6000)
        if job["status"] == "replied":
            existing = db.execute("SELECT id,body FROM private_messages WHERE reply_to=?", (mid,)).fetchone()
            if existing and existing["body"] == body:
                return {"id": existing["id"], "status":"replied"}
            raise Problem("该问题已有答复", 409)
    elif job["status"] == "failed":
        return {"status":"failed"}
    if job["status"] != "processing" or job["until"] <= now:
        raise Problem("本次私聊投递已失效", 409)
    _require_send(actor, active=thread["active"])
    if actor["paused"]:
        raise Problem("项目已暂停自动应答", 409)
    if action == "fail":
        db.execute("UPDATE private_deliveries SET status='failed',until=NULL WHERE message=?", (mid,))
        return {"status":"failed"}
    db.execute("UPDATE private_deliveries SET status='replied',until=NULL WHERE message=?", (mid,))
    result = db.execute("""INSERT INTO private_messages(thread,sender,body,created,request,fingerprint,reply_to)
        VALUES (?,?,?,?,?,?,?)""", (thread["id"],actor["id"],body,now,"private-reply-"+digest(ticket),digest(body),mid)).lastrowid
    return {"id":result, "status":"replied"}


def member_left(db, member):
    db.execute("DELETE FROM private_runners WHERE member=?", (member,))
    db.execute("DELETE FROM private_setup_notices WHERE member=?", (member,))
    db.execute("""UPDATE private_deliveries SET status='cancelled',sid=NULL,ticket_hash=NULL,until=NULL
        WHERE status IN ('pending','processing','failed') AND (recipient=? OR message IN
          (SELECT id FROM private_messages WHERE sender=?))""", (member, member))


def prune(db, now):
    db.execute("DELETE FROM private_setup_notices WHERE created<?", (now-RETENTION,))
    db.execute("DELETE FROM private_messages WHERE created<?", (now-RETENTION,))
    db.execute("DELETE FROM private_threads WHERE NOT EXISTS (SELECT 1 FROM private_messages m WHERE m.thread=private_threads.id)")
