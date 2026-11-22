"""Channel-local delegation from an authenticated Agent to a human member."""
import re

from .common import Problem, digest, string, token


OFFER_LIFETIME = 30 * 60
BROWSER = {"agent-bind-code", "agent-unbind"}
PROJECT = {"binding-preview", "bind-human", "unbind-human"}
CONTROLLED = """SELECT m.id FROM agent_bindings b JOIN members m ON m.id=b.agent
    WHERE b.human=:human AND m.gid=:gid AND m.active=1"""


def mine(db, human, gid):
    return [row[0] for row in db.execute(CONTROLLED + " ORDER BY m.id", {"human":human,"gid":gid})]


def browser(db, actor, action, data, now):
    agent = string(data, "agent")
    human, gid = actor["id"], actor["gid"]
    target = db.execute("""SELECT m.name,b.human FROM members m LEFT JOIN agent_bindings b ON b.agent=m.id
        WHERE m.id=? AND m.gid=? AND m.active=1 AND m.agent NOT IN ('host','human')""", (agent,gid)).fetchone()
    if not target:
        raise Problem("请选择本频道的 Agent", 404)
    if action == "agent-unbind":
        if target["human"] not in (None, human):
            raise Problem("只能解除绑定给自己的 Agent", 403)
        _unlink(db, agent, human, gid)
        return {}
    if target["human"] is not None:
        raise Problem("这个 Agent 已绑定，原绑定解除后可以重新绑定", 409)
    code, expires = token(), now + OFFER_LIFETIME
    db.execute("""INSERT INTO agent_binding_offers(agent,human,code_hash,expires) VALUES (?,?,?,?)
        ON CONFLICT(agent,human) DO UPDATE SET code_hash=excluded.code_hash,expires=excluded.expires,used=0""",
               (agent,human,digest(code),expires))
    return {"code":code,"expires":expires,"agent":target["name"],"human":actor["name"]}


def project(db, actor, action, data, now):
    agent, gid = actor["id"], actor["gid"]
    if action == "unbind-human":
        binding = db.execute("SELECT human FROM agent_bindings WHERE agent=?", (agent,)).fetchone()
        if binding:
            _unlink(db, agent, binding["human"], gid)
        return {}
    code = string(data, "code", 100)
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
        raise Problem("请使用完整的 Agent 绑定链接", 410)
    key = digest(code)
    offer = db.execute("""SELECT h.id,h.name,c.expires,c.used,b.human FROM agent_binding_offers c
        JOIN members h ON h.id=c.human LEFT JOIN agent_bindings b ON b.agent=c.agent
        WHERE c.agent=? AND c.code_hash=? AND c.expires>? AND h.active=1 AND h.gid=?
          AND h.agent IN ('host','human')""", (agent,key,now,gid)).fetchone()
    if not offer or (offer["used"] and offer["human"] != offer["id"]):
        raise Problem("此绑定链接已过期或不适用于当前 Agent", 410)
    if offer["human"] is not None and offer["human"] != offer["id"]:
        raise Problem("请先解除当前绑定", 409)
    result = {"human":{"id":offer["id"],"name":offer["name"]},"expires":offer["expires"]}
    if action == "bind-human" and offer["human"] is None:
        db.execute("INSERT INTO agent_bindings VALUES (?,?,?)", (agent,offer["id"],now))
        db.execute("DELETE FROM agent_binding_offers WHERE agent=? AND code_hash!=?", (agent,key))
        db.execute("UPDATE agent_binding_offers SET used=1 WHERE code_hash=?", (key,))
        _changed(db, gid)
    return result


def _unlink(db, agent, human, gid):
    if db.execute("DELETE FROM agent_bindings WHERE agent=? AND human=?", (agent,human)).rowcount:
        db.execute("DELETE FROM agent_binding_offers WHERE agent=?", (agent,))
        _changed(db, gid)
    else:
        db.execute("DELETE FROM agent_binding_offers WHERE agent=? AND human=?", (agent,human))


def member_left(db, gid, member):
    if db.execute("DELETE FROM agent_bindings WHERE agent=? OR human=?", (member,member)).rowcount:
        _changed(db, gid)
    db.execute("DELETE FROM agent_binding_offers WHERE agent=? OR human=?", (member,member))


def _changed(db, gid):
    db.execute("""INSERT INTO channel_changes VALUES (?,1)
        ON CONFLICT(gid) DO UPDATE SET revision=revision+1""", (gid,))
