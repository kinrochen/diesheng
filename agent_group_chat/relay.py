"""Transactional group membership, public history and delivery receipts."""
import hashlib
import hmac
import json
import re
import sqlite3
import time
from importlib.resources import files

from . import agent_bindings, announcements, colleague_accounts, private_chats
from .common import (Problem, atomic, boolean, digest, ident, integer, origin, private_dir,
                     public_text, rows, string, token, transaction)
from .topic_index import list_topics

ONLINE = 60
RESPONSE_TIMEOUT = 15 * 60
RETENTION = 30 * 86400
AUTO_LIMIT = 3
LINK_LIFETIME = 7 * 86400
BROWSER_LIFETIME = 30 * 86400
RECOVERY_LINK_LIFETIME = 30 * 60
RECOVERY_RETRY = 5 * 60
WEB_CLIENTS = {"codex": "Codex", "claude": "Claude Code", "deepseek": "DeepSeek Harness"}
INBOX_FILTERS = {
    "pending": "d.status IN ('pending','uncertain','manual','failed')",
    "processing": "(d.status IN ('waiting','reviewing') OR d.status='received' AND msg.kind IN ('question','followup'))",
}


class Relay:
    def __init__(self, directory, public_url=None, clock=time.time):
        self.path = private_dir(directory) / "workspace.sqlite3"
        self.public_url = origin(public_url) if public_url else None
        self.clock = clock
        self.login_limit = colleague_accounts.LoginLimit()
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
            db.execute("DELETE FROM browser_sessions WHERE expires<=?", (now,))
            db.execute("DELETE FROM colleague_sessions WHERE expires<=?", (now,))
            db.execute("DELETE FROM member_recovery WHERE used<=? OR (used IS NULL AND expires<=?)",
                       (now - RECOVERY_RETRY, now))
            db.execute("DELETE FROM agent_binding_offers WHERE expires<=?", (now,))
            db.execute("DELETE FROM sessions WHERE seen<?", (now - RETENTION,))
            private_chats.prune(db, now)

    def _expire(self, db, now):
        # A session may have received the message before it disconnected.
        db.execute("""UPDATE deliveries SET status='uncertain', updated=?
            WHERE updated<? AND (status='reviewing' OR status='received' AND mid IN
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
                    WHERE g.owner=? AND NOT EXISTS(SELECT 1 FROM closed_groups c WHERE c.gid=g.id)
                    ORDER BY coalesce(latest.created,g.created) DESC,g.id""", (oid,)))}
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
            closed = db.execute("SELECT 1 FROM closed_groups WHERE gid=?", (gid,)).fetchone()
            if action == "dissolve":
                if not closed:
                    self._dissolve(db, gid, now)
                return {"dissolved": True}
            if closed:
                raise Problem("频道已解散", 410, "channel_dissolved")
            if action == "changes":
                return {"revision": self._revision(db, gid)}
            if action == "admin-role":
                return self._set_admin(db, gid, data)
            if action in private_chats.BROWSER:
                return self._private_browser(db, self._host(db, group, now), action, data, now)
            if action in agent_bindings.BROWSER:
                return self._browser_binding(db, self._host(db, group, now), action, data, now)
            self._expire(db, now)
            if action == "detail":
                return self._browser_detail(db, self._host(db, group, now), data, now, can_manage=True)
            if action == "topics":
                return list_topics(db, gid, 0, data)
            if action == "inbox":
                return self._browser_inbox(db, gid, data, include_host=False)
            if action == "history":
                viewer = self._host(db, group, now)["id"] if data.get("scope") == "mine" else None
                return self._history(db, gid, 0, data, viewer=viewer)
            if action in ("speak", "group_question", "broadcast"):
                return self._speak(db, self._host(db, group, now), action, data, now)
            return self._manage_channel(db, gid, action, data, now)

    @staticmethod
    def _set_admin(db, gid, data):
        member, admin = string(data, "member"), boolean(data, "admin")
        if not db.execute("""SELECT 1 FROM members
            WHERE gid=? AND id=? AND active=1 AND agent='human'""", (gid, member)).fetchone():
            raise Problem("请选择当前频道内的同事", 404)
        if admin:
            changed = db.execute("INSERT OR IGNORE INTO channel_admins VALUES (?)", (member,))
        else:
            changed = db.execute("DELETE FROM channel_admins WHERE member=?", (member,))
        if changed.rowcount:
            db.execute("""INSERT INTO channel_changes VALUES (?,1)
                ON CONFLICT(gid) DO UPDATE SET revision=revision+1""", (gid,))
        return {"admin": admin}

    def _manage_channel(self, db, gid, action, data, now):
        if action == "announcement-publish":
            return {"announcement": announcements.publish(db, gid, data, now)}
        if action == "channel_link":
            link = db.execute("SELECT nonce,expires FROM channel_links WHERE gid=?", (gid,)).fetchone()
            signing_key = db.execute("""SELECT o.key_hash FROM groups g JOIN owners o ON o.id=g.owner
                WHERE g.id=?""", (gid,)).fetchone()[0]
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
        if action == "recovery_link":
            mid = string(data, "member")
            member = db.execute("""SELECT m.name,c.account FROM members m LEFT JOIN colleague_memberships c ON c.member=m.id
                WHERE m.id=? AND m.gid=? AND m.agent='human' AND m.active=1""", (mid,gid)).fetchone()
            if not member:
                raise Problem("请选择本频道仍在群的同事", 404)
            if member["account"]:
                raise Problem("这位同事已设置账号，请使用账号登录", 409)
            expires = now + RECOVERY_LINK_LIFETIME
            code = self._issue_recovery(db, mid, "link", expires)
            return {"name": member["name"], "expires": expires,
                    "url": f"{self.public_url or ''}/room/{gid}#recover={code}"}
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

    @staticmethod
    def _browser_member(db, gid, key_hash, now):
        return db.execute("""SELECT m.*,g.paused AS group_paused FROM browser_sessions s
            JOIN members m ON m.id=s.member JOIN groups g ON g.id=m.gid
            WHERE s.key_hash=? AND s.expires>? AND m.gid=? AND m.active=1 AND m.agent='human'
              AND NOT EXISTS(SELECT 1 FROM colleague_memberships c WHERE c.member=m.id)""",
                          (key_hash, now, gid)).fetchone()

    def account(self, action, data, cookie, source):
        now = self.clock()
        if action == "login":
            account = colleague_accounts.authenticate(self.path,data,self.login_limit,source)
            with transaction(self.path) as db:
                colleague_accounts.require_unchanged(db,account)
                session = colleague_accounts.sign_in(db,account,cookie,now)
                return {"username":account["username"]},session
        if action == "update":
            return colleague_accounts.update(self.path,data,cookie,now,self.login_limit,source),None
        with transaction(self.path) as db:
            if action == "logout":
                colleague_accounts.sign_out(db,cookie)
                return {},""
            if action != "groups":
                raise Problem("未知的同事账号操作", 404)
            account = colleague_accounts.current(db,cookie,now)
            if not account:
                raise Problem("请先登录同事账号",401)
            return {"username":account["username"],"groups":colleague_accounts.groups(db,account["id"])},None

    def member_entry(self, action, gid, data, legacy_cookie, account_cookie):
        if data.get("group",gid)!=gid:
            raise Problem("此成员只能访问受邀频道",403)
        now = self.clock()
        with transaction(self.path) as db:
            if db.execute("SELECT 1 FROM closed_groups WHERE gid=?", (gid,)).fetchone():
                raise Problem("频道已解散", 410, "channel_dissolved")
            account = colleague_accounts.current(db,account_cookie,now)
            if action in ("join","claim") and account and data.get("username",account["username"])!=account["username"]:
                raise Problem("浏览器的登录账号已切换，请重新确认要使用的账号",409,"account_changed")
            legacy = self._browser_member(db,gid,digest(legacy_cookie),now)
            if action in ("legacy_status", "upgrade", "claim"):
                if not legacy:
                    raise Problem("请先恢复这个频道的旧身份",401)
                if action == "legacy_status":
                    group = db.execute("SELECT name FROM groups WHERE id=?",(gid,)).fetchone()
                    return {"member":legacy["id"],"name":legacy["name"],"channel":group["name"],
                            "username":account["username"] if account else None},None
                if action == "claim":
                    if not account:
                        raise Problem("请先登录要关联的同事账号",401)
                    session = None
                else:
                    account = colleague_accounts.create(db,data)
                    session = colleague_accounts.sign_in(db,account,account_cookie,now)
                colleague_accounts.link(db,account["id"],gid,legacy["id"])
                return {"member":legacy["id"],"username":account["username"]},session

            if action == "join" and not account:
                raise Problem("请先登录同事账号，或通过邀请创建账号",401)
            code = string(data,"code",150)
            invite = db.execute("""SELECT h.id,h.expires,h.revoked,h.used_by,g.name,g.purpose
                FROM human_invites h JOIN groups g ON g.id=h.gid WHERE h.gid=? AND h.code_hash=?""",
                                (gid,digest(code))).fetchone()
            member = colleague_accounts.member(db,account["id"],gid) if account else None
            viewer = member or (legacy if action == "preview" else None)
            if (not invite or invite["revoked"] or invite["expires"] <= now or
                    (invite["used_by"] and (not viewer or viewer["id"]!=invite["used_by"]))):
                raise Problem("同事邀请已使用、撤销或过期。已有账号可以直接登录。",410)
            if action == "preview":
                return {"name":invite["name"],"purpose":invite["purpose"],"expires":invite["expires"],
                        "username":account["username"] if account else None,"member":member["id"] if member else None,
                        "legacy":bool(legacy)},None
            if action == "join" and member:
                return {"member":member["id"]},None
            if legacy:
                raise Problem("此浏览器在本频道已有旧身份，请先将它关联到同事账号",409,"legacy_identity")
            session = None
            if action == "register":
                account = colleague_accounts.create(db,data)
                session = colleague_accounts.sign_in(db,account,account_cookie,now)
            mid = self._new_colleague(db,gid,data,now)
            colleague_accounts.link(db,account["id"],gid,mid)
            db.execute("UPDATE human_invites SET used_by=? WHERE id=?",(mid,invite["id"]))
            return {"member":mid,"username":account["username"]},session

    @staticmethod
    def _new_colleague(db, gid, data, now):
        name = public_text(data, "name", 64)
        if name.casefold() in ("host", "群主", "管理员", "我", "我 · 群主"):
            raise Problem("请使用自己的昵称")
        mid = ident()
        try:
            db.execute("""INSERT INTO members(id,gid,agent,name,role,key_hash,joined,start_id,challenge,verified,seen)
                VALUES (?,?,'human',?,'',?,?,0,?,?,?)""", (mid,gid,name,digest(token()),now,token(),now,now))
        except sqlite3.IntegrityError:
            raise Problem("频道里已经有人使用这个昵称，请换一个",409)
        return mid

    @staticmethod
    def _issue_recovery(db, member, kind, expires=None):
        code = token()
        db.execute("""INSERT INTO member_recovery(member,kind,key_hash,expires) VALUES (?,?,?,?)
            ON CONFLICT(member,kind) DO UPDATE SET key_hash=excluded.key_hash,expires=excluded.expires,used=NULL""",
                   (member, kind, digest(code), expires))
        return code

    def recover_member(self, gid, data, cookie, *, preview=False):
        now = self.clock()
        kind = data.get("kind", "link")
        if kind not in ("code", "link") or (preview and kind != "link"):
            raise Problem("请选择恢复码或同事恢复链接")
        code = string(data, "code", 80, optional=True)
        unavailable = Problem("恢复凭证无效、已使用或已过期，请使用新的凭证", 410)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
            raise unavailable
        key_hash = digest(code)
        with transaction(self.path) as db:
            record = db.execute("""SELECT r.member,r.expires,r.used,m.name,g.name AS channel,
                       o.key_hash AS signing_key,s.key_hash AS session_hash,s.expires AS session_expires
                FROM member_recovery r JOIN members m ON m.id=r.member JOIN groups g ON g.id=m.gid
                JOIN owners o ON o.id=g.owner LEFT JOIN browser_sessions s ON s.member=m.id
                WHERE r.key_hash=? AND r.kind=? AND m.gid=? AND m.agent='human' AND m.active=1
                  AND NOT EXISTS(SELECT 1 FROM colleague_memberships c WHERE c.member=m.id)""",
                                (key_hash, kind, gid)).fetchone()
            if not record:
                raise unavailable
            expired = record["expires"] is not None and record["expires"] <= now
            if preview:
                if expired or record["used"] is not None:
                    raise unavailable
                return {"name": record["name"], "channel": record["channel"], "expires": record["expires"]}, None

            request = string(data, "request", 32)
            if not re.fullmatch(r"[0-9a-f]{32}", request):
                raise Problem("恢复请求编号无效，请重试")
            # The same attempt can receive its cookie again if the response was interrupted.
            session = hmac.new(bytes.fromhex(record["signing_key"]),
                               f"member-recovery:{gid}:{key_hash}:{request}".encode(), hashlib.sha256).hexdigest()
            session_hash, member = digest(session), record["member"]
            current = self._browser_member(db, gid, digest(cookie), now)
            if current and current["id"] != member:
                raise Problem("此浏览器已登录本频道的另一位同事，请换一个浏览器恢复", 409)
            if record["used"] is not None:
                if (record["used"] <= now - RECOVERY_RETRY or record["session_hash"] != session_hash
                        or record["session_expires"] <= now):
                    raise unavailable
            else:
                if expired:
                    raise unavailable
                db.execute("""INSERT INTO browser_sessions(key_hash,member,expires) VALUES (?,?,?)
                    ON CONFLICT(member) DO UPDATE SET key_hash=excluded.key_hash,expires=excluded.expires""",
                           (session_hash, member, now + BROWSER_LIFETIME))
                db.execute("UPDATE member_recovery SET used=? WHERE key_hash=?", (now, key_hash))
                db.execute("DELETE FROM member_recovery WHERE member=? AND kind!=?", (member, kind))
            return {"member": member}, session

    def web_member(self, action, gid, data, cookie, account_cookie=""):
        now = self.clock()
        if data.get("group", gid) != gid:
            raise Problem("此成员只能访问受邀频道", 403)
        if action == "logout":
            with transaction(self.path) as db:
                db.execute("""DELETE FROM browser_sessions WHERE key_hash=? AND member IN
                    (SELECT id FROM members WHERE gid=? AND agent='human')""", (digest(cookie), gid))
            return {}
        key_hash = digest(cookie)
        with transaction(self.path) as db:
            if db.execute("SELECT 1 FROM closed_groups WHERE gid=?", (gid,)).fetchone():
                raise Problem("频道已解散", 410, "channel_dissolved")
            account = colleague_accounts.current(db,account_cookie,now)
            member = colleague_accounts.member(db,account["id"],gid) if account else self._browser_member(db,gid,key_hash,now)
            if not member:
                if account:
                    raise Problem("当前账号尚未加入这个频道",403,"not_a_member")
                raise Problem("请登录同事账号，或使用同事邀请加入频道",401)
            if action == "recovery_code":
                if account:
                    raise Problem("这个身份已关联账号，请使用账号登录",409)
                code = self._issue_recovery(db, member["id"], "code")
                return {"code": f"{gid}.{code}"}
            if action == "changes":
                return {"revision": self._revision(db, gid)}
            if action in private_chats.BROWSER:
                return self._private_browser(db, member, action, data, now)
            if action in agent_bindings.BROWSER:
                return self._browser_binding(db, member, action, data, now)
            self._expire(db, now)
            if action == "detail":
                self._host(db, {"id": gid, "paused": member["group_paused"]}, now)
                return self._browser_detail(db, member, data, now, can_manage=self._is_admin(db, member["id"]))
            db.execute("UPDATE members SET seen=? WHERE id=?", (now, member["id"]))
            if action == "groups":
                if account:
                    groups = colleague_accounts.groups(db,account["id"])
                else:
                    groups = rows(db.execute("""SELECT id,name,purpose,paused,created,(SELECT count(*) FROM members
                        WHERE gid=? AND active=1) AS members,
                        EXISTS(SELECT 1 FROM channel_admins WHERE member=?) AS can_manage
                        FROM groups WHERE id=?""", (gid,member["id"],gid)))
                return {"groups":groups,"public_url":self.public_url,
                        "self": {"id": member["id"], "name": member["name"]},
                        "username":account["username"] if account else None}
            if action == "topics":
                return list_topics(db, gid, 0, data)
            if action == "inbox":
                return self._browser_inbox(db, gid, data, include_host=True)
            if action == "history":
                return self._history(db, gid, 0, data, viewer=member["id"])
            if action in ("speak", "group_question", "broadcast"):
                return self._speak(db, member, action, data, now)
            if action == "leave":
                self._remove(db, gid, member["id"], now)
                return {}
            if action == "admin-role":
                raise Problem("只有群主可以设置管理员", 403)
            if action == "dissolve":
                raise Problem("只有群主可以解散频道", 403)
            if self._is_admin(db, member["id"]):
                return self._manage_channel(db, gid, action, data, now)
            raise Problem("此操作需要群主或管理员权限", 403)

    @staticmethod
    def _is_admin(db, member):
        return bool(db.execute("SELECT 1 FROM channel_admins WHERE member=?", (member,)).fetchone())

    def _browser_detail(self, db, viewer, data, now, *, can_manage):
        gid, member = viewer["gid"], viewer["id"]
        is_host = viewer["agent"] == "host"
        db.execute("UPDATE members SET seen=? WHERE id=?", (now, member))
        announcement = announcements.latest(db, gid)
        result = {"revision": self._revision(db, gid), "paused": bool(viewer["group_paused"]),
                  "can_manage": can_manage, "announcement": announcement,
                  "announcement_receipts": announcements.receipts(db, gid, announcement["version"]) if announcement else [],
                  "members": self._members(db, gid, now, include_host=not is_host, viewer=member),
                  **self._web_feed(db, gid, member, data)}
        if is_host:
            result["self"] = member
        if can_manage:
            link = db.execute("SELECT expires FROM channel_links WHERE gid=? AND expires>?", (gid, now)).fetchone()
            result["link_expires"] = link[0] if link else None
            result["invites"] = rows(db.execute("""SELECT id,name,role,created,expires,kind,
                CASE WHEN used_by IS NOT NULL THEN 'used' WHEN revoked=1 THEN 'revoked'
                WHEN expires<=? THEN 'expired' ELSE 'ready' END AS status
                FROM (SELECT id,name,role,created,expires,used_by,revoked,'agent' AS kind FROM invites WHERE gid=?
                  UNION ALL SELECT h.id,coalesce(m.name,'同事邀请'),'',h.created,h.expires,h.used_by,h.revoked,'human'
                  FROM human_invites h LEFT JOIN members m ON m.id=h.used_by WHERE h.gid=?)
                ORDER BY created DESC,id""", (now, gid, gid)))
        return result

    @staticmethod
    def _revision(db, gid):
        row = db.execute("SELECT revision FROM channel_changes WHERE gid=?", (gid,)).fetchone()
        return row[0] if row else 0

    def _browser_binding(self, db, actor, action, data, now):
        result = agent_bindings.browser(db, actor, action, data, now)
        if action == "agent-bind-code":
            code = result.pop("code")
            result["url"] = f"{self.public_url or ''}/agent.md#bind={code}"
        return result

    def _private_browser(self, db, member, action, data, now):
        if action == "private-share":
            private_chats.share_source(db, member, data, now)
            request = "share-" + digest(string(data, "request", 128))
            sid = "host" if member["agent"] == "host" else "browser"
            return self._post(db, member, sid, {"kind":"broadcast", "body":data.get("body"), "request":request}, now)
        return private_chats.participant(db, member, action.removeprefix("private-"), data, now)

    def private_agent(self, action, data, key):
        now = self.clock()
        with transaction(self.path) as db:
            if action == "enable":
                member = db.execute("""SELECT id,gid FROM members WHERE key_hash=? AND active=1
                    AND agent NOT IN ('human','host')""", (digest(key),)).fetchone()
                if (not member or data.get("group",member["gid"]) != member["gid"]
                        or data.get("member",member["id"]) != member["id"]):
                    raise Problem("项目授权已失效", 403)
                return private_chats.enable(db, member["id"], data)
            actor = db.execute("""SELECT m.*,g.paused AS group_paused FROM private_runners r
                JOIN members m ON m.id=r.member JOIN groups g ON g.id=m.gid
                WHERE r.key_hash=? AND m.active=1""", (digest(key),)).fetchone()
            if not actor:
                raise Problem("请使用本项目的私聊凭据", 403)
            if action in private_chats.PARTICIPANT:
                return private_chats.participant(db, actor, action, data, now)
            if action == "next":
                return private_chats.claim(db, actor, data, now)
            if action in ("reply", "fail"):
                return private_chats.complete(db, actor, action, data, now)
            if action == "pulse":
                private_chats.touch(db, actor["id"], now)
                return {"online":True}
            raise Problem("未知的私聊运行器操作", 404)

    def _speak(self, db, member, action, data, now):
        sid = "host" if member["agent"] == "host" else "browser"
        if action in ("group_question", "broadcast"):
            if data.get("parent") is not None:
                raise Problem("答复问题时请使用原问题的答复操作")
            return self._post(db, member, sid, {"kind": action, "body": data.get("body"), "request": data.get("request"),
                              "targets": data.get("targets"), "mentions": data.get("mentions", [])}, now)
        target = string(data, "target", optional=True)
        body = public_text(data, "body", 6000)
        request = string(data, "request", 128)
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
                raise Problem("没有找到点名的成员，请输入 @ 并选择频道成员")
        kind = "discussion"
        if target:
            recipient = db.execute("""SELECT agent FROM members WHERE gid=? AND active=1 AND (id=? OR name=?)
                                   ORDER BY (id=?) DESC LIMIT 1""", (member["gid"], target, target, target)).fetchone()
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

    def _dissolve(self, db, gid, now):
        db.execute("INSERT INTO closed_groups VALUES (?,?)", (gid, now))
        db.execute("DELETE FROM channel_links WHERE gid=?", (gid,))
        db.execute("UPDATE invites SET revoked=1 WHERE gid=?", (gid,))
        db.execute("UPDATE human_invites SET revoked=1 WHERE gid=?", (gid,))
        members = db.execute("SELECT id FROM members WHERE gid=? AND active=1 AND agent!='host'", (gid,)).fetchall()
        for member in members:
            self._remove(db, gid, member["id"], now)
        db.execute("UPDATE members SET active=0 WHERE gid=? AND agent='host'", (gid,))

    @staticmethod
    def _ended_binding(m):
        return {"delivery": None, "paused": False, "notice": {"kind": "membership_ended",
                "group": m["gid"], "member": m["id"], "reason": "dissolved" if m["closed"] is not None else "left"}}

    @staticmethod
    def _remove(db, gid, member, now):
        cur = db.execute("UPDATE members SET active=0 WHERE gid=? AND id=? AND active=1 AND agent!='host'", (gid, member))
        if not cur.rowcount:
            raise Problem("项目已移除或不属于本群", 404)
        db.execute("DELETE FROM channel_admins WHERE member=?", (member,))
        db.execute("DELETE FROM browser_sessions WHERE member=?", (member,))
        db.execute("DELETE FROM member_recovery WHERE member=?", (member,))
        db.execute("DELETE FROM colleague_memberships WHERE member=?", (member,))
        agent_bindings.member_left(db, gid, member)
        private_chats.member_left(db, member)
        db.execute("""UPDATE deliveries SET status='cancelled',updated=? WHERE status IN
            ('pending','received','uncertain','manual','failed','waiting','reviewing') AND mid IN
            (SELECT id FROM messages WHERE gid=? AND (target=? OR sender=?) AND
              (kind IN ('question','followup') OR (kind='answer' AND deliveries.status IN ('pending','reviewing'))))""",
                   (now, gid, member, member))
        # A cancelled assignment may already have asked a third project. Retire its open descendants too.
        db.execute("""WITH RECURSIVE abandoned(mid) AS (
                SELECT d.mid FROM deliveries d JOIN messages x ON x.id=d.mid WHERE x.gid=? AND d.status='cancelled'
                UNION SELECT t.result FROM agent_turns t JOIN abandoned parent ON t.assignment=parent.mid
                      WHERE t.decision IN ('ask','followup') AND t.result IS NOT NULL
                UNION SELECT a.id FROM messages a JOIN abandoned q ON a.parent=q.mid WHERE a.kind='answer'
            ) UPDATE deliveries SET status='cancelled',updated=? WHERE mid IN (SELECT mid FROM abandoned)
              AND (status IN ('pending','waiting','reviewing','uncertain','manual','failed') OR
                   status='received' AND mid IN (SELECT id FROM messages WHERE kind IN ('question','followup')))""",
                   (gid, now))
        # Let a surviving caller choose another peer if the outstanding call was cancelled.
        db.execute("""UPDATE deliveries AS upstream SET status='pending',sid=NULL,ticket=NULL,updated=?
            WHERE upstream.status='waiting' AND (SELECT child.status='cancelled' OR coalesce(review.status='cancelled',0)
              FROM agent_turns t JOIN deliveries child ON child.mid=t.result
              LEFT JOIN messages ans ON ans.parent=child.mid AND ans.kind='answer'
              LEFT JOIN deliveries review ON review.mid=ans.id
              WHERE t.assignment=upstream.mid AND t.decision IN ('ask','followup')
              ORDER BY t.result DESC LIMIT 1)""", (now,))

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
    def _members(db, gid, now, *, include_host=False, viewer=None):
        return rows(db.execute(f"""SELECT m.id,m.name,m.agent,m.role,m.joined,m.verified,m.paused,
            EXISTS(SELECT 1 FROM colleague_memberships c WHERE c.member=m.id) AS has_account,
            EXISTS(SELECT 1 FROM channel_admins a WHERE a.member=m.id) AS admin,
            CASE WHEN m.agent IN ('host','human') OR ? IS NULL THEN NULL
                 WHEN b.human=? THEN 'mine' WHEN b.human IS NULL THEN 'unbound' ELSE 'bound' END AS human_binding,
            coalesce(m.seen>?,0) AS online, coalesce(m.auto_seen>?,0) AS automatic,
            (SELECT count(*) FROM sessions s WHERE s.member=m.id AND s.seen>?) AS sessions,
            (SELECT count(*) FROM deliveries d WHERE d.member=m.id AND {INBOX_FILTERS['pending']}) AS pending,
            (SELECT count(*) FROM deliveries d JOIN messages msg ON msg.id=d.mid
                WHERE d.member=m.id AND {INBOX_FILTERS['processing']}) AS processing
            FROM members m LEFT JOIN agent_bindings b ON b.agent=m.id
            WHERE m.gid=? AND m.active=1 AND (m.agent!='host' OR ?) ORDER BY m.joined,m.id""",
                               (viewer, viewer, now-ONLINE, now-ONLINE, now-ONLINE, gid, include_host)))

    @staticmethod
    def _browser_inbox(db, gid, data, *, include_host):
        status = string(data, "status", 20)
        if status not in INBOX_FILTERS:
            raise Problem("请选择待处理或处理中")
        limit = min(100, integer(data, "limit", 20))
        before = integer(data, "before") if data.get("before") is not None else None
        items = rows(db.execute(f"""SELECT msg.id,msg.kind,substr(msg.body,1,240) AS body,msg.created,
            author.name AS author,recipient.name AS recipient,d.status AS receipt,
            coalesce(branch.discussion,msg.topic) AS topic,
            CASE WHEN branch.question=msg.id THEN branch.discussion ELSE msg.id END AS message
            FROM deliveries d JOIN messages msg ON msg.id=d.mid
            JOIN members author ON author.id=msg.sender JOIN members recipient ON recipient.id=d.member
            LEFT JOIN discussion_mail branch ON branch.question=msg.topic
            WHERE msg.gid=? AND recipient.active=1 AND (? OR recipient.agent!='host')
              AND (? IS NULL OR msg.id<?) AND {INBOX_FILTERS[status]}
            ORDER BY msg.id DESC LIMIT ?""", (gid, include_host, before, before, limit+1)))
        return {"items": items[:limit], "more": len(items)>limit}

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
                announcements.start_at_current(db, gid, mid)
            if personal:
                db.execute("UPDATE invites SET used_by=? WHERE id=?", (mid, link["id"]))
            return {**info, "member":mid, "name":name}

    def project(self, action, data, key):
        now = self.clock()
        with transaction(self.path) as db:
            m = db.execute("""SELECT m.*,g.name AS group_name,g.purpose AS group_purpose,g.paused AS group_paused,c.closed
                FROM members m JOIN groups g ON g.id=m.gid LEFT JOIN closed_groups c ON c.gid=g.id
                WHERE m.key_hash=? AND m.agent NOT IN ('human','host')""",
                           (digest(key),)).fetchone()
            if m and action == "leave" and (data.get("group", m["gid"]) != m["gid"]
                                             or data.get("member", m["id"]) != m["id"]):
                raise Problem("退群请求与当前成员不匹配", 403)
            if not m or not m["active"] or m["closed"] is not None:
                if m and data.get("lifecycle") is True and action in ("access", "next", "context", "leave"):
                    return self._ended_binding(m)
                raise Problem("项目授权已失效，请联系群主", 403, "membership_revoked")
            mid, gid = m["id"], m["gid"]
            self._expire(db, now)
            db.execute("UPDATE members SET seen=? WHERE id=?", (now, mid))
            if action == "access":
                return {"member": mid}
            sid = string(data, "session", 100)
            db.execute("""INSERT INTO sessions(member,sid,seen) VALUES (?,?,?)
                ON CONFLICT(member,sid) DO UPDATE SET seen=excluded.seen""", (mid, sid, now))
            if action in agent_bindings.PROJECT:
                return agent_bindings.project(db, m, action, data, now)
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
            if action == "announcement-ack":
                return {"notice": announcements.acknowledge(db, m, data)}
            if action == "context":
                members = self._members(db, gid, now)
                notice = announcements.pending(db, m) if data.get("announcements") == announcements.GUIDE else None
                return {"group":{"id":gid, "name":m["group_name"], "purpose":m["group_purpose"],
                                 "paused":bool(m["group_paused"]), "owner_name":"群主"},
                        "self":{"id":mid, "name":m["name"], "agent":m["agent"], "role":m["role"], "paused":bool(m["paused"])},
                        "members":members,
                        "announcement":announcements.latest(db, gid),
                        **({"notice":notice} if notice else {}),
                        "recent":self._history(db, gid, m["start_id"], {"limit":12})["messages"]}
            if action == "private-setup-response":
                return private_chats.setup_response(db,mid,sid,data,now)
            if action == "next":
                paused = bool(m["group_paused"] or m["paused"])
                delivery = None
                if not paused:
                    db.execute("UPDATE members SET auto_seen=? WHERE id=?", (now, mid))
                if data.get("announcements") == announcements.GUIDE:
                    notice = announcements.pending(db, m)
                    if notice:
                        return {"delivery": None, "paused": paused, "notice": notice}
                if not paused:
                    if data.get("private_setup") is True:
                        notice = private_chats.setup_notice(db,mid,sid,now)
                        if notice:
                            return {"delivery":None,"paused":False,"notice":notice}
                    awaiting = string(data, "awaiting_ticket", 128, optional=True)
                    collaborate = data.get("collaborate") is True
                    row = db.execute("""SELECT d.mid,d.ticket FROM deliveries d JOIN messages x ON x.id=d.mid
                        WHERE d.member=? AND x.id>? AND (d.status='pending' OR
                          (d.sid=? AND (d.status='received' AND x.kind IN ('question','followup')
                                        OR d.status IN ('reviewing','uncertain') AND ?)))
                        AND (? OR x.kind!='answer' OR NOT EXISTS
                            (SELECT 1 FROM agent_turns t WHERE t.result=x.parent AND t.decision IN ('ask','followup')))
                        ORDER BY CASE WHEN d.sid=? THEN 0 ELSE 1 END,d.mid LIMIT 1""",
                                     (mid, m["start_id"], sid, collaborate, collaborate, sid)).fetchone()
                    # Keep the long request asleep while the notified session composes its answer.
                    if row and row["ticket"] != awaiting:
                        delivery = self._receive(db, m, sid, {"message": row["mid"], "collaborate": collaborate}, now)
                return {"delivery": delivery, "paused": paused}
            # The installed project-chat tools still use the legacy Agent actions.
            if action in ("history", "inbox", "conclusions"):
                return self._history(db, gid, m["start_id"], data, mid if action == "inbox" else None,
                                     viewer=mid, conclusions=action == "conclusions")
            if action == "accept":
                aid = integer(data, "answer")
                if db.execute("""SELECT 1 FROM messages a JOIN agent_turns t ON t.result=a.parent
                    JOIN deliveries origin ON origin.mid=t.assignment WHERE a.id=? AND a.target=?
                    AND origin.status='waiting'""", (aid,mid)).fetchone():
                    raise Problem("请先处理同伴答复，将结果回到上游问题", 409)
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
                return self._ended_binding(m) if data.get("lifecycle") is True else {}
            if action == "post":
                return self._post(db, m, sid, data, now)
            if action == "turn":
                return self._turn(db, m, sid, data, now)
            if action == "fail":
                message = integer(data, "message")
                reason = public_text(data, "reason", 200)
                ticket = string(data, "ticket", 128)
                changed = db.execute("""UPDATE deliveries SET status='failed',updated=? WHERE mid=? AND member=?
                    AND sid=? AND ticket=? AND status IN ('received','reviewing','failed')
                    AND NOT EXISTS (SELECT 1 FROM agent_turns t WHERE t.event=mid AND t.ticket=deliveries.ticket)""",
                                     (now,message,mid,sid,ticket))
                if not changed.rowcount:
                    raise Problem("本次领取已结束，请核对最新回执", 409)
                db.execute("INSERT OR REPLACE INTO delivery_errors(mid,reason) VALUES (?,?)", (message,reason))
                return {"status":"failed"}
            if action == "receive":
                return self._receive(db, m, sid, data, now)
            raise Problem("未知的项目操作", 404)

    def _web_feed(self, db, gid, viewer, data):
        if data.get("directory") is True:
            return {"directory": list_topics(db, gid, 0, data)}
        return {"history": self._history(db, gid, 0, data, viewer=viewer)}

    @staticmethod
    def _history(db, gid, start, data, inbox=None, *, viewer=None, conclusions=False):
        where = ["msg.gid=?", "msg.id>?"]
        args = [gid, start]
        scope = data.get("scope", "all")
        if scope not in ("all", "mine"):
            raise Problem("请选择全部消息或只看与我有关")
        prefix, binding_params, mention = "", (), ""
        if scope == "mine":
            prefix = """WITH viewer(id) AS (VALUES (?)), related(id) AS (
                SELECT id FROM viewer UNION ALL
                SELECT b.agent FROM agent_bindings b JOIN members a ON a.id=b.agent
                WHERE b.human=(SELECT id FROM viewer) AND a.gid=? AND a.active=1)"""
            binding_params = (viewer,gid)
            mention = """, CASE WHEN msg.kind='broadcast' THEN EXISTS(SELECT 1 FROM message_mentions v
                WHERE v.mid=msg.id AND v.member=(SELECT id FROM viewer)) END AS mentions_viewer"""
            where.append("""(msg.sender IN (SELECT id FROM related) OR msg.target IN (SELECT id FROM related) OR
                (msg.kind='group_question' AND EXISTS (SELECT 1 FROM discussion_mail mail
                    JOIN deliveries recipient ON recipient.mid=mail.question
                    WHERE mail.discussion=msg.id AND recipient.member IN (SELECT id FROM related))) OR
                EXISTS (SELECT 1 FROM message_mentions mention WHERE mention.mid=msg.id
                    AND mention.member IN (SELECT id FROM related)))""")
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
        items = rows(db.execute(f"""{prefix} SELECT msg.id,msg.kind,msg.body,msg.evidence,coalesce(branch.discussion,msg.topic) AS topic,
            msg.parent,msg.created,msg.sender,a.name AS author,a.agent AS author_agent,msg.target,b.name AS recipient,
            msg.accepted,q.body AS question,coalesce(d.status,'recorded') AS receipt,err.reason AS error,
            EXISTS(SELECT 1 FROM quick_answers qa WHERE qa.mid=msg.id) AS quick_reply{mention} FROM messages msg
            JOIN members a ON a.id=msg.sender LEFT JOIN members b ON b.id=msg.target
            LEFT JOIN deliveries d ON d.mid=msg.id
            LEFT JOIN discussion_mail branch ON branch.question=msg.topic
            LEFT JOIN delivery_errors err ON err.mid=d.mid
            LEFT JOIN messages q ON msg.kind='answer' AND q.id=msg.parent AND q.gid=msg.gid AND q.id>?
            WHERE {' AND '.join(where)} ORDER BY msg.id {order} LIMIT ?""", (*binding_params,start,*args,limit+1)))
        more = len(items) > limit
        items = items[:limit]
        for item in items:
            if item["kind"] in ("discussion", "group_question"):
                recipients = rows(db.execute("""SELECT m.id AS member,m.name,x.question,d.status,e.reason FROM discussion_mail x
                    JOIN deliveries d ON d.mid=x.question JOIN members m ON m.id=d.member
                    LEFT JOIN delivery_errors e ON e.mid=d.mid WHERE x.discussion=? ORDER BY x.question""", (item["id"],)))
                item["recipients"] = recipients
                states = {r["status"] for r in recipients}
                item["receipt"] = next((s for s in ("uncertain","failed","manual","waiting","reviewing","received","pending","replied","cancelled")
                                        if s in states), "recorded")
                item["error"] = "；".join(r["name"] + " · " + r["reason"] for r in recipients if r["reason"])
        if not forward:
            items.reverse()
        result = {"messages": items, "more": more, "scope": scope}
        if scope == "mine":
            result["my_agents"] = agent_bindings.mine(db,viewer,gid)
        return result

    @staticmethod
    def _member_ids(data, key):
        values = data.get(key, [])
        if not isinstance(values, list) or any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value) for value in values):
            raise Problem("请提供有效的频道成员编号列表")
        return list(dict.fromkeys(values))

    def _post(self, db, m, sid, data, now, *, source=None):
        kind = string(data, "kind")
        if kind not in ("broadcast", "discussion", "group_question", "question", "answer", "followup", "chat") or (
                kind == "chat" and m["agent"] not in ("host", "human")):
            raise Problem("请选择讨论、广播、提问、答复或追问")
        body = public_text(data, "body", 6000)
        evidence = public_text(data, "evidence", 1200, optional=True)
        request = string(data, "request", 128)
        parent = source["id"] if source else (integer(data, "parent") if kind in ("answer", "followup") else None)
        target = string(data, "target") if kind in ("question", "chat") else None
        fields = [kind, body, evidence, parent, target]
        targets = None
        mentions = []
        if kind == "group_question":
            targets = self._member_ids(data, "targets")
            if not targets:
                raise Problem("请提供要点名的频道成员编号列表")
            fields.append(sorted(targets))
        elif kind == "broadcast":
            mentions = self._member_ids(data, "mentions")
            if mentions:
                fields.append(sorted(mentions))
        fingerprint = digest(json.dumps(fields, ensure_ascii=False))
        existing = db.execute("""SELECT msg.id,coalesce(x.discussion,msg.topic) AS topic,msg.fingerprint
                              FROM messages msg LEFT JOIN discussion_mail x ON x.question=msg.topic
                              WHERE msg.sender=? AND msg.request=?""",
                              (m["id"], request)).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise Problem("请求编号已用于其他内容，请换一个编号", 409)
            return {"id": existing["id"], "topic": existing["topic"]}
        if m["group_paused"]:
            raise Problem("群主已暂停讨论，消息已保留", 409)
        human = data.get("human") is True
        topic = source["topic"] if source else None
        public_topic = source["public_topic"] if source else None
        delegated = False
        if kind in ("answer", "followup"):
            previous = db.execute("""SELECT msg.*,x.discussion FROM messages msg
                                  LEFT JOIN discussion_mail x ON x.question=msg.topic
                                  WHERE msg.gid=? AND msg.id=? AND msg.id>?""",
                                  (m["gid"], parent, m["start_id"])).fetchone()
            if not previous or m["id"] not in (previous["sender"], previous["target"]):
                raise Problem("找不到可参与的原问题", 404)
            topic = previous["topic"]
            public_topic = previous["discussion"]
            if kind == "answer":
                if previous["target"] != m["id"] or previous["kind"] not in ("question", "followup"):
                    raise Problem("请选择发给本项目的问题", 409)
                claim = db.execute("SELECT * FROM deliveries WHERE mid=?", (parent,)).fetchone()
                if (claim["status"] not in ("received","uncertain") or claim["sid"] != sid
                        or claim["ticket"] != string(data, "ticket", 128)):
                    raise Problem("请先在本会话领取原问题并核对回执", 409)
                delegated = self._assignment(db, parent) is not None
                if (m["paused"] or (not delegated and self._at_cap(db, topic))) and not human:
                    raise Problem("该问题需要开发者确认答复", 409)
                target = previous["sender"]
            else:
                if previous["kind"] != "answer":
                    raise Problem("请引用已收到的答复发起追问", 409)
                if not source and self._assignment(db, previous["parent"]) is not None:
                    raise Problem("请通过当前协作事件追问，保留返回上游的任务", 409)
                target = previous["sender"] if previous["target"] == m["id"] else previous["target"]
        browser_sid = None
        if target:
            target_row = db.execute("""SELECT id,agent FROM members WHERE gid=? AND active=1 AND (id=? OR name=?)
                                    ORDER BY (id=?) DESC LIMIT 1""", (m["gid"], target, target, target)).fetchone()
            if not target_row or target_row["id"] == m["id"]:
                raise Problem("请选择群内另一个有效项目", 404)
            target = target_row["id"]
            browser_sid = {"host": "host", "human": "browser"}.get(target_row["agent"])
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
            if not human and not delegated:
                db.execute("UPDATE topics SET auto_answers=auto_answers+1 WHERE id=?", (topic,))
                if self._at_cap(db, topic):
                    db.execute("""UPDATE deliveries SET status='manual' WHERE status='pending'
                        AND mid IN (SELECT id FROM messages WHERE topic=? AND kind='followup')""", (topic,))
        if target and kind != "chat":
            status = "manual" if kind == "followup" and self._at_cap(db, topic) else "pending"
            db.execute("INSERT INTO deliveries(mid,member,status,updated) VALUES (?,?,?,?)", (msg, target, status, now))
            if browser_sid and kind == "answer":
                db.execute("UPDATE deliveries SET status='received',sid=? WHERE mid=?", (browser_sid, msg))
        if kind in ("discussion", "group_question"):
            self._fanout(db, m, sid, msg, body, evidence, targets, now)
        if mentions:
            db.executemany("""INSERT INTO message_mentions(mid,member)
                SELECT ?,id FROM members WHERE gid=? AND id=? AND active=1""",
                           ((msg, m["gid"], mentioned) for mentioned in mentions))
        return {"id": msg, "topic": public_topic or topic}

    def _fanout(self, db, m, sid, discussion, body, evidence, targets, now):
        peers = dict(db.execute("""SELECT id,agent FROM members WHERE gid=? AND active=1 AND id!=?
                                ORDER BY joined,id""", (m["gid"], m["id"])))
        if targets is None:
            targets = [mid for mid, agent in peers.items() if agent not in ("human", "host")]
            if not targets and m["agent"] != "human" and "human" not in peers.values():
                raise Problem("请先添加一位参与讨论的 Agent", 409)
        elif any(mid not in peers for mid in targets):
            raise Problem("点名的成员不在当前频道或是你自己，请重新选择", 409)
        for mid in targets:
            mail = self._post(db, m, sid, {"kind": "question", "body": body, "evidence": evidence,
                              "target": mid, "request": f"discussion:{discussion}:{mid}"}, now)
            db.execute("INSERT INTO discussion_mail(question,discussion) VALUES (?,?)", (mail["id"], discussion))

    @staticmethod
    def _at_cap(db, topic):
        row = db.execute("SELECT auto_answers FROM topics WHERE id=?", (topic,)).fetchone()
        return bool(row and row[0] >= AUTO_LIMIT)

    @staticmethod
    def _assignment(db, question):
        row = db.execute("""SELECT assignment FROM agent_turns
            WHERE result=? AND decision IN ('ask','followup')""", (question,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _remaining_calls(db, branch):
        used = db.execute("""SELECT count(*) FROM messages msg JOIN agent_turns t ON t.result=msg.id
            WHERE msg.topic=? AND t.decision IN ('ask','followup')""", (branch,)).fetchone()[0]
        return max(0, AUTO_LIMIT - used)

    def _turn(self, db, m, sid, data, now):
        mid = integer(data, "message")
        ticket = string(data, "ticket", 128)
        decision = string(data, "decision")
        if decision not in ("ask", "followup", "reply", "finish"):
            raise Problem("请选择询问、追问、答复或结束")
        target = string(data, "target") if decision == "ask" else None
        body = public_text(data, "body", 6000, optional=decision == "finish")
        evidence = public_text(data, "evidence", 1200, optional=True)
        if decision == "finish" and (body or evidence):
            raise Problem("结束事件只需提交本次票据")
        fingerprint = digest(json.dumps([decision, target, body, evidence], ensure_ascii=False))
        event = db.execute("""SELECT msg.*,coalesce(branch.discussion,msg.topic) public_topic,d.status,d.sid AS claim_sid,d.ticket
            FROM messages msg JOIN deliveries d ON d.mid=msg.id LEFT JOIN discussion_mail branch ON branch.question=msg.topic
            WHERE msg.id=? AND msg.gid=? AND msg.target=? AND msg.id>?""", (mid, m["gid"], m["id"], m["start_id"])).fetchone()
        if not event or event["claim_sid"] != sid or event["ticket"] != ticket:
            raise Problem("请使用本会话签收的事件和票据", 409)
        prior = db.execute("SELECT result,fingerprint FROM agent_turns WHERE event=? AND ticket=?", (mid, ticket)).fetchone()
        if prior:
            if fingerprint != prior["fingerprint"]:
                raise Problem("本次事件已有其他处理结果", 409)
            return {"decision": decision, "id": prior["result"], "topic": event["public_topic"]}
        if m["paused"] or m["group_paused"]:
            raise Problem("讨论或自动应答已暂停", 409)
        review = event["kind"] == "answer"
        if event["status"] not in ("uncertain", "reviewing" if review else "received"):
            raise Problem("本次事件已结束或正在等候同伴", 409)
        assignment = self._assignment(db, event["parent"]) if review else mid
        if review and assignment:
            origin = db.execute("""SELECT d.sid,d.ticket,d.status FROM deliveries d JOIN messages q ON q.id=d.mid
                WHERE d.mid=? AND q.gid=? AND d.member=? AND q.id>?""", (assignment, m["gid"], m["id"], m["start_id"])).fetchone()
            if not origin or origin["status"] != "waiting":
                raise Problem("原问题已经结束", 409)

        outgoing = {"body": body, "evidence": evidence, "request": "turn-" + digest(ticket)}
        sent = None
        if decision in ("ask", "followup"):
            if not self._remaining_calls(db, event["topic"]) or self._at_cap(db, event["topic"]):
                raise Problem("已达到本分支的自动协作上限，请用现有信息答复或结束", 409)
            if decision == "ask":
                peer = db.execute("""SELECT id FROM members WHERE gid=? AND active=1 AND agent NOT IN ('host','human')
                    AND (id=? OR name=?) ORDER BY (id=?) DESC LIMIT 1""", (m["gid"],target,target,target)).fetchone()
                if not peer or peer[0] == m["id"]:
                    raise Problem("请选择频道内另一位项目 Agent", 409)
                sent = self._post(db, m, sid, {**outgoing, "kind": "question", "target": peer[0]}, now, source=event)
            else:
                if not review:
                    raise Problem("追问需要使用同伴答复事件", 409)
                sent = self._post(db, m, sid, {**outgoing, "kind": "followup", "parent": mid}, now, source=event)
            if not review:
                db.execute("UPDATE deliveries SET status='waiting',updated=? WHERE mid=?", (now, mid))
        elif decision == "reply":
            if not assignment:
                raise Problem("这份答复没有上游问题，请结束或追问", 409)
            answer_sid, answer_ticket = sid, ticket
            if review:
                answer_sid, answer_ticket = origin["sid"], origin["ticket"]
                db.execute("UPDATE deliveries SET status='received' WHERE mid=?", (assignment,))
            sent = self._post(db, m, answer_sid, {**outgoing, "kind": "answer", "parent": assignment,
                                                  "ticket": answer_ticket}, now)
        elif assignment:
            raise Problem("还有上游问题待答复，请把结果回给原提问者", 409)
        if review:
            db.execute("UPDATE deliveries SET status='received',updated=? WHERE mid=?", (now, mid))
        result = sent["id"] if sent else None
        db.execute("INSERT INTO agent_turns(event,ticket,decision,fingerprint,result,assignment) VALUES (?,?,?,?,?,?)",
                   (mid, ticket, decision, fingerprint, result, assignment))
        return {"decision": decision, "id": result, "topic": event["public_topic"]}

    def _receive(self, db, m, sid, data, now):
        msg = integer(data, "message")
        claim = db.execute("""SELECT d.*,x.kind,x.topic AS branch,coalesce(branch.discussion,x.topic) AS topic,x.body,x.evidence,x.parent,a.name AS author
            FROM deliveries d JOIN messages x ON x.id=d.mid JOIN members a ON a.id=x.sender
            LEFT JOIN discussion_mail branch ON branch.question=x.topic
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
        if claim["status"] == "waiting":
            raise Problem("原问题正在等待同伴答复", 409)
        if claim["kind"] == "answer" and data.get("collaborate") is not True and self._assignment(db, claim["parent"]) is not None:
            raise Problem("请用支持自动协作的监听器接续同伴答复", 409)
        if claim["kind"] == "answer" and data.get("collaborate") is True and claim["status"] == "received":
            raise Problem("这份答复已经处理", 409)
        if claim["status"] in ("received","uncertain","reviewing") and claim["sid"] != sid:
            raise Problem("其他会话已领取，请核对回执", 409)
        if claim["status"] == "manual" and not human:
            raise Problem("已达到连续自动答复上限，请开发者确认接手", 409)
        ticket = claim["ticket"] or token()
        db.execute("DELETE FROM delivery_errors WHERE mid=?", (msg,))
        collaborate = data.get("collaborate") is True
        reviewing = claim["kind"] == "answer" and (claim["status"] == "reviewing" or
                                                    collaborate and claim["status"] in ("pending","uncertain"))
        db.execute("UPDATE deliveries SET status=?,sid=?,ticket=?,updated=? WHERE mid=?",
                   ("reviewing" if reviewing else "received", sid, ticket, now, msg))
        event = {"id": msg, "kind": claim["kind"], "body": claim["body"], "evidence": claim["evidence"],
                 "author": claim["author"], "topic": claim["topic"], "parent": claim["parent"], "ticket": ticket}
        if collaborate:
            assignment = self._assignment(db, claim["parent"]) if claim["kind"] == "answer" else msg
            ids = (claim["topic"], claim["parent"], assignment)
            background = rows(db.execute("""SELECT x.id,a.name AS author,substr(x.body,1,1200) AS body
                FROM messages x JOIN members a ON a.id=x.sender WHERE x.gid=? AND x.id>? AND x.id!=?
                AND x.id IN (?,?,?) ORDER BY x.id""", (m["gid"], m["start_id"], msg, *ids)))
            remaining = 0 if self._at_cap(db, claim["branch"]) else self._remaining_calls(db, claim["branch"])
            event.update(assignment=assignment, background=background, remaining_calls=remaining)
        return event
