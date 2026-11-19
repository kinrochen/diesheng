"""Transactional group membership, public history and delivery receipts."""
import hashlib
import hmac
import json
import re
import sqlite3
import time
from importlib.resources import files

from .common import (Problem, atomic, boolean, digest, ident, integer, origin, private_dir,
                     public_text, rows, string, token, transaction)

ONLINE = 60
RESPONSE_TIMEOUT = 15 * 60
RETENTION = 30 * 86400
AUTO_LIMIT = 3
LINK_LIFETIME = 7 * 86400
BROWSER_LIFETIME = 30 * 86400
WEB_CLIENTS = {"codex": "Codex", "claude": "Claude Code", "deepseek": "DeepSeek Harness"}


class Relay:
    def __init__(self, directory, public_url=None, clock=time.time):
        self.path = private_dir(directory) / "workspace.sqlite3"
        self.public_url = origin(public_url) if public_url else None
        self.clock = clock
        with sqlite3.connect(self.path) as db:
            db.executescript(files("agent_group_chat").joinpath("schema.sql").read_text(encoding="utf-8"))
        self.path.chmod(0o600)
        self.prune()

    def create_owner(self):
        key = token()
        with transaction(self.path) as db:
            db.execute("INSERT INTO owners VALUES (?,?)", (ident(), digest(key)))
        return key

    def _invite_url(self, code, *, personal=False):
        return f"{self.public_url or ''}/join#{'i' if personal else 'c'}={code}"

    def first_owner(self, key_file):
        with transaction(self.path) as db:
            if not db.execute("SELECT 1 FROM owners LIMIT 1").fetchone():
                key = token()
                atomic(key_file, (key + "\n").encode())
                db.execute("INSERT INTO owners VALUES (?,?)", (ident(), digest(key)))

    def login(self, key):
        session = token()
        with transaction(self.path) as db:
            owner = db.execute("SELECT id FROM owners WHERE key_hash=?", (digest(key),)).fetchone()
            if not owner:
                raise Problem("群主密钥无效，请从服务主机重新复制", 401)
            db.execute("INSERT INTO logins VALUES (?,?,?)", (digest(session), owner["id"], self.clock() + 8*3600))
        return session

    def prune(self):
        with transaction(self.path) as db:
            now = self.clock()
            db.execute("DELETE FROM messages WHERE created<?", (now - RETENTION,))
            db.execute("DELETE FROM topics WHERE NOT EXISTS (SELECT 1 FROM messages WHERE topic=topics.id)")
            db.execute("DELETE FROM logins WHERE until<=?", (now,))
            db.execute("DELETE FROM invites WHERE expires<?", (now - RETENTION,))
            db.execute("DELETE FROM human_invites WHERE expires<?", (now - RETENTION,))
            expired = db.execute("""SELECT m.gid,m.id FROM browser_sessions s JOIN members m ON m.id=s.member
                WHERE s.expires<=? AND m.active=1""", (now,)).fetchall()
            for member in expired:
                self._remove(db, member["gid"], member["id"], now)
            db.execute("DELETE FROM sessions WHERE seen<?", (now - RETENTION,))

    def _expire(self, db, now):
        # A session may have received the message before it disconnected.
        db.execute("""UPDATE deliveries SET status='uncertain', updated=?
            WHERE (status='received' AND updated<? AND mid IN
                (SELECT id FROM messages WHERE kind IN ('question','followup')))""",
                   (now, now - RESPONSE_TIMEOUT))

    def owner(self, action, data, key):
        now = self.clock()
        with transaction(self.path) as db:
            owner = db.execute("""SELECT id FROM owners WHERE key_hash=? UNION ALL
                SELECT owner FROM logins WHERE key_hash=? AND until>?""", (digest(key), digest(key), now)).fetchone()
            if not owner:
                raise Problem("请使用群主密钥登录", 401)
            oid = owner[0]
            if action == "logout":
                db.execute("DELETE FROM logins WHERE key_hash=?", (digest(key),))
                return {}
            if action == "groups":
                return {"public_url": self.public_url, "groups": rows(db.execute("""
                    SELECT g.id,g.name,g.purpose,g.paused,g.created,
                    substr(latest.body,1,100) AS last_body, latest.created AS last_at,
                    author.name AS last_author, author.agent AS last_agent,
                    (SELECT count(*) FROM members WHERE gid=g.id AND active=1 AND agent!='host') AS members
                    FROM groups g
                    LEFT JOIN messages latest ON latest.id=(
                        SELECT msg.id FROM messages msg WHERE msg.gid=g.id
                        AND NOT EXISTS (SELECT 1 FROM discussion_mail copy WHERE copy.question=msg.id)
                        ORDER BY msg.id DESC LIMIT 1)
                    LEFT JOIN members author ON author.id=latest.sender
                    WHERE g.owner=? ORDER BY coalesce(latest.created,g.created) DESC,g.id""", (oid,)))}
            if action == "create":
                gid = ident()
                name = public_text(data, "name", 64)
                try:
                    db.execute("INSERT INTO groups(id,owner,name,purpose,created) VALUES (?,?,?,?,?)",
                               (gid, oid, name, public_text(data, "purpose", 240, optional=True), now))
                except sqlite3.IntegrityError:
                    raise Problem("已经有同名群，请换一个群名", 409)
                return {"group": gid}
            gid = string(data, "group")
            group = db.execute("SELECT * FROM groups WHERE id=? AND owner=?", (gid, oid)).fetchone()
            if not group:
                raise Problem("群不存在或当前账号无权管理", 404)
            if action == "changes":
                return {"revision": self._revision(db, gid)}
            self._expire(db, now)
            if action == "channel_link":
                link = db.execute("SELECT nonce,expires FROM channel_links WHERE gid=?", (gid,)).fetchone()
                signing_key = db.execute("SELECT key_hash FROM owners WHERE id=?", (oid,)).fetchone()[0]
                nonce, expires = (link["nonce"], link["expires"]) if link and link["expires"] > now else (ident(), now + LINK_LIFETIME)
                code = hmac.new(signing_key.encode(), ("channel-link:" + nonce).encode(), hashlib.sha256).hexdigest()
                if not link or link["expires"] <= now:
                    db.execute("INSERT OR REPLACE INTO channel_links VALUES (?,?,?,?)", (gid, nonce, digest(code), expires))
                return {"url": self._invite_url(code), "expires": expires}
            if action == "disable_link":
                db.execute("DELETE FROM channel_links WHERE gid=?", (gid,))
                return {}
            if action == "human_invite":
                code, iid, expires = token(), ident(), now + 86400
                db.execute("INSERT INTO human_invites(id,gid,code_hash,created,expires) VALUES (?,?,?,?,?)",
                           (iid, gid, digest(code), now, expires))
                return {"id": iid, "expires": expires, "url": f"{self.public_url or ''}/room/{gid}#invite={code}"}
            if action == "detail":
                host = self._host(db, group, now)
                db.execute("UPDATE members SET seen=? WHERE id=?", (now, host["id"]))
                link = db.execute("SELECT expires FROM channel_links WHERE gid=? AND expires>?", (gid, now)).fetchone()
                invitations = rows(db.execute("""SELECT id,name,role,created,expires,kind,
                    CASE WHEN used_by IS NOT NULL THEN 'used' WHEN revoked=1 THEN 'revoked'
                    WHEN expires<=? THEN 'expired' ELSE 'ready' END AS status
                    FROM (SELECT id,name,role,created,expires,used_by,revoked,'agent' AS kind FROM invites WHERE gid=?
                      UNION ALL SELECT h.id,coalesce(m.name,'同事邀请'),'',h.created,h.expires,h.used_by,h.revoked,'human'
                      FROM human_invites h LEFT JOIN members m ON m.id=h.used_by WHERE h.gid=?)
                    ORDER BY created DESC,id""", (now, gid, gid)))
                return {"self": host["id"], "revision": self._revision(db, gid), "members": self._members(db, gid, now), "invites": invitations, "link_expires": link[0] if link else None,
                        "paused": bool(group["paused"]), "history": self._history(db, gid, 0, data,
                            conclusions=data.get("conclusions") is True)}
            if action in ("history", "conclusions"):
                return self._history(db, gid, 0, data, conclusions=action == "conclusions")
            if action == "speak":
                return self._speak(db, self._host(db, group, now), data, now)
            if action == "profile":
                return self._profile(db, gid, string(data, "member"), data)
            if action == "invite":
                code, iid = token(), ident()
                name = public_text(data, "name", 64)
                role = public_text(data, "role", 160, optional=True)
                db.execute("INSERT INTO invites(id,gid,code_hash,name,role,created,expires) VALUES (?,?,?,?,?,?,?)",
                           (iid, gid, digest(code), name, role, now, now + 1800))
                return {"id": iid, "expires": now + 1800, "code": code,
                        "url": self._invite_url(code, personal=True)}
            if action == "revoke":
                args = (gid, string(data, "invite"))
                agents = db.execute("UPDATE invites SET revoked=1 WHERE gid=? AND id=? AND used_by IS NULL", args)
                humans = db.execute("UPDATE human_invites SET revoked=1 WHERE gid=? AND id=? AND used_by IS NULL", args)
                if not agents.rowcount and not humans.rowcount:
                    raise Problem("邀请已被使用或不存在，请刷新列表", 409)
            elif action == "pause":
                db.execute("UPDATE groups SET paused=? WHERE id=?", (boolean(data, "paused"), gid))
            elif action == "remove":
                self._remove(db, gid, string(data, "member"), now)
            elif action == "requeue":
                self._requeue(db, gid, integer(data, "message"), now)
            else:
                raise Problem("未知的管理操作", 404)
            return {}

    @staticmethod
    def _host(db, group, now):
        gid = group["id"]
        row = db.execute("SELECT * FROM members WHERE gid=? AND agent='host'", (gid,)).fetchone()
        if not row:
            name = "群主"
            while db.execute("SELECT 1 FROM members WHERE gid=? AND active=1 AND name=?", (gid, name)).fetchone():
                name = "群主·" + ident()[:6]
            db.execute("""INSERT INTO members(id,gid,name,role,key_hash,joined,start_id,challenge,verified,agent)
                VALUES (?,?,?,?,?,?,?,?,?,'host')""", (ident(), gid, name, "群主", digest(token()), now, 0, token(), now))
            row = db.execute("SELECT * FROM members WHERE gid=? AND agent='host'", (gid,)).fetchone()
        return {**dict(row), "group_paused": group["paused"]}

    def web_member(self, action, gid, data, cookie):
        now = self.clock()
        if data.get("group", gid) != gid:
            raise Problem("此成员只能访问受邀频道", 403)
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", cookie):
            raise Problem("请先打开同事邀请链接", 401)
        key_hash = digest(cookie)
        with transaction(self.path) as db:
            member = db.execute("""SELECT m.*,g.paused AS group_paused FROM browser_sessions s
                JOIN members m ON m.id=s.member JOIN groups g ON g.id=m.gid
                WHERE s.key_hash=? AND s.expires>? AND m.gid=? AND m.active=1 AND m.agent='human'""",
                                (key_hash, now, gid)).fetchone()
            if action in ("preview", "join"):
                code = string(data, "code", 150)
                invite = db.execute("""SELECT h.*,g.name,g.purpose FROM human_invites h JOIN groups g ON g.id=h.gid
                    WHERE h.gid=? AND h.code_hash=?""", (gid, digest(code))).fetchone()
                if (not invite or invite["revoked"] or invite["expires"] <= now
                        or (invite["used_by"] and (not member or invite["used_by"] != member["id"]))):
                    raise Problem("同事邀请已使用、撤销或过期，请向群主索取新链接", 410)
                if action == "preview":
                    return {"name": invite["name"], "purpose": invite["purpose"], "expires": invite["expires"]}
                if member:
                    return {"member": member["id"]}
                existing = db.execute("""SELECT m.gid,s.member,s.expires FROM browser_sessions s
                    JOIN members m ON m.id=s.member WHERE s.key_hash=?""", (key_hash,)).fetchone()
                if existing:
                    if existing["expires"] > now:
                        raise Problem("请使用此频道的浏览器身份加入", 403)
                    self._remove(db, existing["gid"], existing["member"], now)
                name = public_text(data, "name", 64)
                if name.casefold() in ("host", "群主", "管理员", "我", "我 · 群主"):
                    raise Problem("请使用自己的昵称")
                mid = ident()
                try:
                    db.execute("""INSERT INTO members(id,gid,agent,name,role,key_hash,joined,start_id,challenge,verified,seen)
                        VALUES (?,?,'human',?,'',?,?,0,?,?,?)""",
                               (mid, gid, name, digest(token()), now, token(), now, now))
                except sqlite3.IntegrityError:
                    raise Problem("频道里已经有人使用这个昵称，请换一个", 409)
                db.execute("INSERT INTO browser_sessions(key_hash,member,expires) VALUES (?,?,?)",
                           (key_hash, mid, now + BROWSER_LIFETIME))
                db.execute("UPDATE human_invites SET used_by=? WHERE id=?", (mid, invite["id"]))
                return {"member": mid}
            if not member:
                raise Problem("请使用这个频道的同事邀请加入，或联系群主重新邀请", 401)
            if action == "changes":
                return {"revision": self._revision(db, gid)}
            self._expire(db, now)
            db.execute("UPDATE members SET seen=? WHERE id=?", (now, member["id"]))
            if action == "groups":
                group = db.execute("""SELECT id,name,purpose,paused,created,(SELECT count(*) FROM members
                    WHERE gid=? AND active=1) AS members FROM groups WHERE id=?""", (gid, gid)).fetchone()
                return {"groups": [dict(group)], "public_url": self.public_url,
                        "self": {"id": member["id"], "name": member["name"]}}
            if action == "detail":
                self._host(db, {"id": gid, "paused": member["group_paused"]}, now)
                return {"revision": self._revision(db, gid), "paused": bool(member["group_paused"]), "members": self._members(db, gid, now, include_host=True),
                        "history": self._history(db, gid, 0, data, conclusions=data.get("conclusions") is True)}
            if action in ("history", "conclusions"):
                return self._history(db, gid, 0, data, conclusions=action == "conclusions")
            if action == "speak":
                return self._speak(db, member, data, now)
            if action == "leave":
                self._remove(db, gid, member["id"], now)
                return {}
            raise Problem("此操作需要群主权限", 403)

    @staticmethod
    def _revision(db, gid):
        row = db.execute("SELECT revision FROM channel_changes WHERE gid=?", (gid,)).fetchone()
        return row[0] if row else 0

    def _speak(self, db, member, data, now):
        target = string(data, "target", optional=True)
        body = public_text(data, "body", 6000)
        request = string(data, "request", 128)
        sid = "host" if member["agent"] == "host" else "browser"
        if data.get("parent") is not None:
            parent = integer(data, "parent")
            claim = db.execute("SELECT ticket,status FROM deliveries WHERE mid=? AND member=?",
                               (parent, member["id"])).fetchone()
            if not claim:
                raise Problem("找不到发给你的问题", 404)
            ticket = claim["ticket"] if claim["status"] == "replied" else self._receive(
                db, member, sid, {"message": parent, "human": True}, now)["ticket"]
            return self._post(db, member, sid, {"kind": "answer", "parent": parent, "ticket": ticket,
                              "body": body, "request": request, "human": True}, now)
        if not target and body.startswith("@"):
            names = db.execute("SELECT name FROM members WHERE gid=? AND active=1 ORDER BY length(name) DESC",
                               (member["gid"],))
            for row in names:
                if re.match("@" + re.escape(row[0]) + r"(?=$|[\s，,：:。！？!?])", body):
                    target = row[0]
                    break
            if not target and not re.match(r"@(?:全体|所有人)(?=$|[\s，,：:。！？!?])", body):
                raise Problem("没有找到点名的成员，请从收件人列表中选择")
        kind = "discussion"
        if target:
            recipient = db.execute("SELECT agent FROM members WHERE gid=? AND active=1 AND (id=? OR name=?)",
                                   (member["gid"], target, target)).fetchone()
            kind = "chat" if recipient and recipient["agent"] in ("human", "host") else "question"
        return self._post(db, member, sid, {"kind": kind,
                          "target": target, "body": body, "request": request}, now)

    @staticmethod
    def _profile(db, gid, mid, data):
        name = public_text(data, "name", 64)
        role = public_text(data, "role", 160, optional=True)
        try:
            changed = db.execute("UPDATE members SET name=?,role=? WHERE id=? AND gid=? AND active=1 AND agent!='host'",
                                 (name, role, mid, gid))
        except sqlite3.IntegrityError:
            raise Problem("群内已有同名成员", 409)
        if not changed.rowcount:
            raise Problem("此成员已离开或不在当前群", 404)
        return {"name": name, "role": role}

    @staticmethod
    def _remove(db, gid, member, now):
        cur = db.execute("UPDATE members SET active=0 WHERE gid=? AND id=? AND active=1 AND agent!='host'", (gid, member))
        if not cur.rowcount:
            raise Problem("项目已移除或不属于本群", 404)
        db.execute("DELETE FROM browser_sessions WHERE member=?", (member,))
        db.execute("""UPDATE deliveries SET status='cancelled',updated=? WHERE status IN
            ('pending','received','uncertain','manual','failed') AND mid IN
            (SELECT id FROM messages WHERE gid=? AND (target=? OR sender=?) AND kind IN ('question','followup'))""",
                   (now, gid, member, member))

    @staticmethod
    def _requeue(db, gid, mid, now):
        cur = db.execute("""UPDATE deliveries SET status='pending',sid=NULL,ticket=NULL,updated=?
            WHERE (mid=? OR mid IN (SELECT question FROM discussion_mail WHERE discussion=?))
            AND status IN ('uncertain','failed') AND mid IN (SELECT id FROM messages WHERE gid=?)""",
                         (now, mid, mid, gid))
        if not cur.rowcount:
            raise Problem("回执已变化，请重新核对消息状态", 409)
        db.execute("""DELETE FROM delivery_errors WHERE mid IN (SELECT mid FROM deliveries WHERE status='pending')
                   AND (mid=? OR mid IN (SELECT question FROM discussion_mail WHERE discussion=?))""", (mid, mid))

    @staticmethod
    def _members(db, gid, now, *, include_host=False):
        return rows(db.execute("""SELECT m.id,m.name,m.agent,m.role,m.joined,m.verified,m.paused,
            coalesce(m.seen>?,0) AS online, coalesce(m.auto_seen>?,0) AS automatic,
            (SELECT count(*) FROM sessions s WHERE s.member=m.id AND s.seen>?) AS sessions,
            (SELECT count(*) FROM deliveries d WHERE d.member=m.id AND status IN
                ('pending','uncertain','manual','failed')) AS pending,
            (SELECT count(*) FROM deliveries d JOIN messages msg ON msg.id=d.mid
                WHERE d.member=m.id AND d.status='received' AND msg.kind IN ('question','followup')) AS processing
            FROM members m WHERE m.gid=? AND m.active=1 AND (m.agent!='host' OR ?) ORDER BY m.joined,m.id""",
                               (now-ONLINE, now-ONLINE, now-ONLINE, gid, include_host)))

    def channel(self, action, data, key=""):
        """Preview or redeem a channel link within one transaction."""
        if action not in ("preview", "join"):
            raise Problem("未知的入群操作", 404)
        mode = data.get("type", "channel")
        if mode not in ("personal", "channel"):
            raise Problem("未知邀请类型")
        code = string(data, "code", 150)
        now = self.clock()
        with transaction(self.path) as db:
            personal = mode == "personal"
            if personal:
                link = db.execute("""SELECT i.*,g.name AS channel,g.purpose FROM invites i JOIN groups g ON g.id=i.gid
                    WHERE code_hash=?""", (digest(code),)).fetchone()
            else:
                link = db.execute("""SELECT l.*,g.name AS channel,g.purpose FROM channel_links l JOIN groups g ON g.id=l.gid
                    WHERE code_hash=?""", (digest(code),)).fetchone()
            if not link or link["expires"] <= now or (personal and link["revoked"]):
                raise Problem("邀请已撤销或过期，请向群主索取新链接", 410)
            gid = link["gid"]
            info = {"group":gid, "group_name":link["channel"], "purpose":link["purpose"], "expires":link["expires"]}
            if personal:
                info.update(name=link["name"], role=link["role"])
                if link["used_by"]:
                    same = action == "join" and db.execute("SELECT id FROM members WHERE id=? AND active=1 AND key_hash=?",
                                                           (link["used_by"], digest(key))).fetchone()
                    if same:
                        return {**info, "member":same[0]}
                    raise Problem("邀请已被使用，请重新领取", 410)
            if action == "preview":
                return {**info, "url": self._invite_url(code, personal=personal)}
            if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", key) or key == code:
                raise Problem("请生成一份独立的随机成员凭据并作为 Bearer 发送", 401)
            agent = string(data, "agent", 32)
            if agent not in WEB_CLIENTS:
                raise Problem("请选择 codex、claude 或 deepseek")
            existing = db.execute("SELECT id,gid,name,active FROM members WHERE key_hash=?", (digest(key),)).fetchone()
            if existing:
                if not existing["active"] or existing["gid"] != gid or (personal and existing["name"] != link["name"]):
                    raise Problem("这份成员凭据不能用于当前邀请", 403)
                mid, name = existing["id"], existing["name"]
            else:
                requested = link["name"] if personal else public_text(data, "name", 64, optional=True)
                role = link["role"] if personal else public_text(data, "role", 160, optional=True)
                name = requested or WEB_CLIENTS[agent]
                if name.casefold() in ("host", "群主", "我", "我 · 群主"):
                    raise Problem("请选择项目或 Agent 的名称")
                index = 1
                while db.execute("SELECT 1 FROM members WHERE gid=? AND name=? AND active=1", (gid, name)).fetchone():
                    if requested:
                        raise Problem("群内已有同名成员，请换一个公开名称", 409)
                    index += 1
                    name = f"{WEB_CLIENTS[agent]} {index}"
                mid = ident()
                start = db.execute("SELECT coalesce(max(id),0) FROM messages WHERE gid=?", (gid,)).fetchone()[0]
                db.execute("""INSERT INTO members(id,gid,name,role,key_hash,joined,start_id,challenge,agent,seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (mid, gid, name, role, digest(key), now, start, token(), agent, now))
            if personal:
                db.execute("UPDATE invites SET used_by=? WHERE id=?", (mid, link["id"]))
            return {**info, "member":mid, "name":name}

    def project(self, action, data, key):
        now = self.clock()
        with transaction(self.path) as db:
            m = db.execute("""SELECT m.*,g.name AS group_name,g.purpose AS group_purpose,g.paused AS group_paused FROM members m
                JOIN groups g ON g.id=m.gid WHERE m.key_hash=? AND m.active=1 AND m.agent NOT IN ('human','host')""",
                           (digest(key),)).fetchone()
            if not m:
                raise Problem("项目授权已失效，请联系群主", 403, "membership_revoked")
            mid, gid = m["id"], m["gid"]
            self._expire(db, now)
            db.execute("UPDATE members SET seen=? WHERE id=?", (now, mid))
            if action == "access":
                return {"member": mid}
            sid = string(data, "session", 100)
            db.execute("""INSERT INTO sessions(member,sid,seen) VALUES (?,?,?)
                ON CONFLICT(member,sid) DO UPDATE SET seen=excluded.seen""", (mid, sid, now))
            if action == "status":
                return {"group": gid, "name": m["group_name"], "member": mid, "project_name": m["name"],
                        "role": m["role"], "agent": m["agent"], "verified": bool(m["verified"]),
                        "challenge": None if m["verified"] else m["challenge"],
                        "paused": bool(m["group_paused"]), "project_paused": bool(m["paused"]),
                        "mode": "automatic" if (m["auto_seen"] or 0) > now-ONLINE else "manual"}
            if action == "check":
                if string(data, "challenge") != m["challenge"]:
                    raise Problem("连接检查编号不匹配", 409)
                db.execute("UPDATE members SET verified=coalesce(verified,?) WHERE id=?", (now, mid))
                return {"verified": True}
            if action == "members":
                return {"members": self._members(db, gid, now)}
            if action == "context":
                members = self._members(db, gid, now)
                return {"group":{"id":gid, "name":m["group_name"], "purpose":m["group_purpose"],
                                 "paused":bool(m["group_paused"]), "owner_name":"群主"},
                        "self":{"id":mid, "name":m["name"], "agent":m["agent"], "role":m["role"], "paused":bool(m["paused"])},
                        "members":members,
                        "recent":self._history(db, gid, m["start_id"], {"limit":12})["messages"]}
            if action == "next":
                paused = bool(m["group_paused"] or m["paused"])
                delivery = None
                if not paused:
                    db.execute("UPDATE members SET auto_seen=? WHERE id=?", (now, mid))
                    awaiting = string(data, "awaiting_ticket", 128, optional=True)
                    row = db.execute("""SELECT d.mid,d.ticket FROM deliveries d JOIN messages x ON x.id=d.mid
                        WHERE d.member=? AND x.id>? AND (d.status='pending' OR
                          (d.sid=? AND d.status='received' AND x.kind IN ('question','followup')))
                        ORDER BY CASE WHEN d.sid=? THEN 0 ELSE 1 END,d.mid LIMIT 1""", (mid, m["start_id"], sid, sid)).fetchone()
                    # Keep the long request asleep while the notified session composes its answer.
                    if row and row["ticket"] != awaiting:
                        delivery = self._receive(db, m, sid, {"message": row["mid"]}, now)
                return {"delivery": delivery, "paused": paused}
            if action in ("history", "inbox", "conclusions"):
                return self._history(db, gid, m["start_id"], data, mid if action == "inbox" else None,
                                     conclusions=action == "conclusions")
            if action == "accept":
                aid = integer(data, "answer")
                changed = db.execute("""UPDATE messages SET accepted=coalesce(accepted,?) WHERE id=? AND id>?
                    AND gid=? AND kind='answer' AND target=?""", (now, aid, m["start_id"], gid, mid))
                if not changed.rowcount:
                    raise Problem("仅提问项目可以采纳发给自己的答复", 409)
                db.execute("UPDATE deliveries SET status='received',sid=?,updated=? WHERE mid=? AND member=?",
                           (sid, now, aid, mid))
                return {"answer": aid, "accepted": True}
            if action == "project_pause":
                db.execute("UPDATE members SET paused=? WHERE id=?", (boolean(data, "paused"), mid))
                return {}
            if action == "profile":
                return self._profile(db, gid, mid, data)
            if action == "leave":
                self._remove(db, gid, mid, now)
                return {}
            if action == "post":
                return self._post(db, m, sid, data, now)
            if action == "fail":
                message = integer(data, "message")
                reason = public_text(data, "reason", 200)
                ticket = string(data, "ticket", 128)
                changed = db.execute("""UPDATE deliveries SET status='failed',updated=? WHERE mid=? AND member=?
                    AND sid=? AND ticket=? AND status IN ('received','failed')""", (now,message,mid,sid,ticket))
                if not changed.rowcount:
                    raise Problem("本次领取已结束，请核对最新回执", 409)
                db.execute("INSERT OR REPLACE INTO delivery_errors(mid,reason) VALUES (?,?)", (message,reason))
                return {"status":"failed"}
            if action == "receive":
                return self._receive(db, m, sid, data, now)
            raise Problem("未知的项目操作", 404)

    @staticmethod
    def _history(db, gid, start, data, inbox=None, *, conclusions=False):
        where = ["msg.gid=?", "msg.id>?"]
        args = [gid, start]
        if conclusions:
            where.append("msg.accepted IS NOT NULL")
        query = string(data, "query", 160, optional=True)
        if query:
            where.append("""instr(lower(msg.body || char(10) || msg.evidence || char(10) ||
                           coalesce(q.body,'') || char(10) || a.name),lower(?))>0""")
            args.append(query)
        if inbox:
            where.append("(msg.sender=? OR msg.target=?) AND msg.kind!='broadcast'")
            args.extend([inbox, inbox])
            if data.get("unread") is True:
                where.append("msg.target=? AND d.status IN ('pending','uncertain','manual','failed')")
                args.append(inbox)
        else:
            where.append("NOT EXISTS (SELECT 1 FROM discussion_mail h WHERE h.question=msg.id)")
        if data.get("topic") is not None:
            topic = integer(data, "topic")
            where.append("(msg.topic=? OR branch.discussion=?)")
            args.extend([topic, topic])
        for name, expr in (("before","msg.id<?"), ("after","msg.id>?")):
            if data.get(name) is not None:
                where.append(expr)
                args.append(integer(data, name, low=0))
        limit = min(100, integer(data, "limit", 40))
        # When replaying after a cursor, start at the oldest unseen ID to avoid gaps.
        forward = data.get("after") is not None
        order = "ASC" if forward else "DESC"
        items = rows(db.execute(f"""SELECT msg.id,msg.kind,msg.body,msg.evidence,coalesce(branch.discussion,msg.topic) AS topic,
            msg.parent,msg.created,msg.sender,a.name AS author,a.agent AS author_agent,msg.target,b.name AS recipient,
            msg.accepted,q.body AS question,coalesce(d.status,'recorded') AS receipt,err.reason AS error,
            EXISTS(SELECT 1 FROM quick_answers qa WHERE qa.mid=msg.id) AS quick_reply FROM messages msg
            JOIN members a ON a.id=msg.sender LEFT JOIN members b ON b.id=msg.target
            LEFT JOIN deliveries d ON d.mid=msg.id
            LEFT JOIN discussion_mail branch ON branch.question=msg.topic
            LEFT JOIN delivery_errors err ON err.mid=d.mid
            LEFT JOIN messages q ON msg.kind='answer' AND q.id=msg.parent AND q.gid=msg.gid AND q.id>?
            WHERE {' AND '.join(where)} ORDER BY msg.id {order} LIMIT ?""", (start, *args, limit + 1)))
        more = len(items) > limit
        items = items[:limit]
        for item in items:
            if item["kind"] == "discussion":
                recipients = rows(db.execute("""SELECT m.name,d.status,e.reason FROM discussion_mail x
                    JOIN deliveries d ON d.mid=x.question JOIN members m ON m.id=d.member
                    LEFT JOIN delivery_errors e ON e.mid=d.mid WHERE x.discussion=? ORDER BY x.question""", (item["id"],)))
                item["recipients"] = recipients
                states = {r["status"] for r in recipients}
                item["receipt"] = next((s for s in ("uncertain","failed","manual","received","pending","replied","cancelled")
                                        if s in states), "recorded")
                item["error"] = "；".join(r["name"] + " · " + r["reason"] for r in recipients if r["reason"])
        if not forward:
            items.reverse()
        return {"messages": items, "more": more}

    def _post(self, db, m, sid, data, now, *, topic=None):
        kind = string(data, "kind")
        if kind not in ("broadcast", "discussion", "question", "answer", "followup", "chat") or (
                kind == "chat" and m["agent"] not in ("host", "human")):
            raise Problem("请选择讨论、广播、提问、答复或追问")
        body = public_text(data, "body", 6000)
        evidence = public_text(data, "evidence", 1200, optional=True)
        request = string(data, "request", 128)
        parent = integer(data, "parent") if kind in ("answer", "followup") else None
        target = string(data, "target") if kind in ("question", "chat") else None
        fingerprint = digest(json.dumps([kind, body, evidence, parent, target], ensure_ascii=False))
        existing = db.execute("SELECT id,topic,fingerprint FROM messages WHERE sender=? AND request=?",
                              (m["id"], request)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise Problem("请求编号已用于其他内容，请换一个编号", 409)
            return {"id": existing["id"], "topic": existing["topic"]}
        if m["group_paused"]:
            raise Problem("群主已暂停讨论，消息已保留", 409)
        human = data.get("human") is True
        if parent:
            previous = db.execute("SELECT * FROM messages WHERE gid=? AND id=? AND id>?",
                                  (m["gid"], parent, m["start_id"])).fetchone()
            if not previous or m["id"] not in (previous["sender"], previous["target"]):
                raise Problem("找不到可参与的原问题", 404)
            topic = previous["topic"]
            if kind == "answer":
                if previous["target"] != m["id"] or previous["kind"] not in ("question", "followup"):
                    raise Problem("请选择发给本项目的问题", 409)
                claim = db.execute("SELECT * FROM deliveries WHERE mid=?", (parent,)).fetchone()
                if (claim["status"] not in ("received","uncertain") or claim["sid"] != sid
                        or claim["ticket"] != string(data, "ticket", 128)):
                    raise Problem("请先在本会话领取原问题并核对回执", 409)
                if (m["paused"] or self._at_cap(db, topic)) and not human:
                    raise Problem("该问题需要开发者确认答复", 409)
                target = previous["sender"]
            else:
                if previous["kind"] != "answer":
                    raise Problem("请引用已收到的答复发起追问", 409)
                target = previous["sender"] if previous["target"] == m["id"] else previous["target"]
        browser_sid = None
        if target:
            target_row = db.execute("SELECT id,agent FROM members WHERE gid=? AND active=1 AND (id=? OR name=?)",
                                    (m["gid"], target, target)).fetchall()
            if len(target_row) != 1 or target_row[0][0] == m["id"]:
                raise Problem("请选择群内另一个有效项目", 404)
            target = target_row[0][0]
            browser_sid = {"host": "host", "human": "browser"}.get(target_row[0]["agent"])
        cursor = db.execute("""INSERT INTO messages(gid,kind,sender,target,sid,body,evidence,parent,topic,
            created,request,fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (m["gid"], kind, m["id"], target, sid, body, evidence, parent, topic, now, request, fingerprint))
        msg = cursor.lastrowid
        if topic is None:
            topic = msg
            db.execute("UPDATE messages SET topic=? WHERE id=?", (topic, msg))
            if kind == "question":
                db.execute("INSERT INTO topics(id) VALUES (?)", (topic,))
        if kind == "answer":
            db.execute("UPDATE deliveries SET status='replied',updated=? WHERE mid=?", (now, parent))
            if not human:
                db.execute("UPDATE topics SET auto_answers=auto_answers+1 WHERE id=?", (topic,))
                if self._at_cap(db, topic):
                    db.execute("""UPDATE deliveries SET status='manual' WHERE status='pending'
                        AND mid IN (SELECT id FROM messages WHERE topic=? AND kind='followup')""", (topic,))
        if target and kind != "chat":
            status = "manual" if kind == "followup" and self._at_cap(db, topic) else "pending"
            db.execute("INSERT INTO deliveries(mid,member,status,updated) VALUES (?,?,?,?)", (msg, target, status, now))
            if browser_sid and kind == "answer":
                db.execute("UPDATE deliveries SET status='received',sid=? WHERE mid=?", (browser_sid, msg))
        if kind == "discussion":
            peers = db.execute("SELECT id FROM members WHERE gid=? AND active=1 AND agent NOT IN ('host','human') AND id!=? ORDER BY joined,id",
                               (m["gid"], m["id"])).fetchall()
            if not peers and m["agent"] != "human" and not db.execute(
                    "SELECT 1 FROM members WHERE gid=? AND active=1 AND agent='human'", (m["gid"],)).fetchone():
                raise Problem("请先添加一位参与讨论的 Agent", 409)
            for peer in peers:
                mail = self._post(db, m, sid, {"kind":"question", "body":body, "evidence":evidence,
                                  "target":peer["id"], "request":f"discussion:{msg}:{peer['id']}"}, now)
                db.execute("INSERT INTO discussion_mail(question,discussion) VALUES (?,?)", (mail["id"], msg))
        return {"id": msg, "topic": topic}

    @staticmethod
    def _at_cap(db, topic):
        row = db.execute("SELECT auto_answers FROM topics WHERE id=?", (topic,)).fetchone()
        return bool(row and row[0] >= AUTO_LIMIT)

    def _receive(self, db, m, sid, data, now):
        msg = integer(data, "message")
        claim = db.execute("""SELECT d.*,x.kind,x.topic,x.body,x.evidence,x.parent,a.name AS author
            FROM deliveries d JOIN messages x ON x.id=d.mid JOIN members a ON a.id=x.sender
            WHERE mid=? AND d.member=? AND x.id>?""", (msg, m["id"], m["start_id"])).fetchone()
        if not claim:
            raise Problem("项目收件箱中没有这条消息", 404)
        if "ticket" in data and data["ticket"] != claim["ticket"]:
            raise Problem("这次投递的票据已经失效，请核对最新回执", 409)
        human = data.get("human") is True
        if m["group_paused"] or (m["paused"] and not human):
            raise Problem("讨论或自动应答已暂停", 409)
        if claim["status"] == "replied":
            raise Problem("该问题已回复", 409)
        if claim["status"] == "cancelled":
            raise Problem("参与项目已退出，该问题已停止投递", 409)
        if claim["status"] == "failed" and "ticket" in data:
            raise Problem("该次自动回复已结束，请重新投递", 409)
        if claim["status"] in ("received","uncertain") and claim["sid"] != sid:
            raise Problem("其他会话已领取，请核对回执", 409)
        if claim["status"] == "manual" and not human:
            raise Problem("已达到连续自动答复上限，请开发者确认接手", 409)
        ticket = claim["ticket"] or token()
        db.execute("DELETE FROM delivery_errors WHERE mid=?", (msg,))
        db.execute("UPDATE deliveries SET status='received',sid=?,ticket=?,updated=? WHERE mid=?",
                   (sid, ticket, now, msg))
        return {"id": msg, "kind": claim["kind"], "body": claim["body"], "evidence": claim["evidence"],
                "author": claim["author"], "topic": claim["topic"], "parent": claim["parent"], "ticket": ticket}
