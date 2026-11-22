"""Channel topic summaries derived from the same public threads and live receipts as history."""

from .common import Problem, integer, rows, string


KINDS = ("question", "group_question", "discussion", "chat", "collaboration")
STATES = ("pending", "active", "answered", "attention", "cancelled", "recorded")
ROOT = """root.id=root.topic AND root.kind IN ('question','group_question','discussion','chat')
    AND NOT EXISTS(SELECT 1 FROM discussion_mail copy WHERE copy.question=root.id)"""
VISIBLE = """WITH visible AS (
    SELECT msg.*,coalesce(mail.discussion,msg.topic) AS thread,d.status,
           step.decision AS step
    FROM messages msg LEFT JOIN discussion_mail mail ON mail.question=msg.topic
    LEFT JOIN deliveries d ON d.mid=msg.id
    LEFT JOIN agent_turns step ON step.result=msg.id
    WHERE msg.gid=? AND msg.id>? AND msg.kind!='broadcast'
)"""


def topic_reference(db, gid, start, topic, since):
    row = db.execute(f"""SELECT root.id,substr(root.body,1,180) AS title FROM messages root
        WHERE root.gid=? AND root.id=? AND root.id>? AND root.created>=? AND {ROOT}""",
                     (gid, topic, start, since)).fetchone()
    return dict(row) if row else None


def list_topics(db, gid, start, data, *, since=None):
    category, status = data.get("category", ""), data.get("status", "")
    if category not in ("", *KINDS) or status not in ("", *STATES):
        raise Problem("请选择有效的话题分类和状态")
    limit = min(100, integer(data, "limit", 40))
    where, params = [], [gid, start, start, since]
    for field, value in (("category", category), ("status", status)):
        if value:
            where.append(field + "=?")
            params.append(value)
    query = string(data, "query", 100, optional=True)
    if query:
        where.append("(instr(lower(title),lower(?))>0 OR '#'||id=?)")
        params.extend((query, query))
    if data.get("before") is not None:
        where.append("last_id<?")
        params.append(integer(data, "before"))
    filters = " WHERE " + " AND ".join(where) if where else ""
    topics = rows(db.execute(VISIBLE + f""", totals AS (
        SELECT thread,max(id) last_id,max(created) last_at,
          max(CASE WHEN step IN ('ask','followup') THEN 1 ELSE 0 END) collaborated,
          count(CASE WHEN status IN ('manual','failed','uncertain') THEN 1 END) attention,
          count(CASE WHEN kind IN ('question','followup') AND status='pending' THEN 1 END) pending,
          count(CASE WHEN (kind IN ('question','followup') AND status IN ('received','waiting'))
                          OR (kind='answer' AND status IN ('pending','reviewing')) THEN 1 END) processing,
          count(CASE WHEN status='cancelled' THEN 1 END) cancelled,
          count(CASE WHEN kind='answer' THEN 1 END) answers
        FROM visible GROUP BY thread
    ), directory AS (
        SELECT root.id,substr(root.body,1,180) AS title,a.name AS author,t.last_id,t.last_at,t.answers,
          CASE WHEN t.collaborated THEN 'collaboration' ELSE root.kind END AS category,
          CASE WHEN t.attention>0 THEN 'attention'
               WHEN t.processing>0 OR (t.pending>0 AND t.answers>0) THEN 'active'
               WHEN t.pending>0 THEN 'pending' WHEN t.cancelled>0 THEN 'cancelled'
               WHEN t.answers>0 THEN 'answered'
               ELSE 'recorded' END AS status,
          substr(latest.body,1,160) AS latest,writer.name AS latest_author
        FROM totals t JOIN messages root ON root.id=t.thread JOIN members a ON a.id=root.sender
        JOIN messages latest ON latest.id=t.last_id JOIN members writer ON writer.id=latest.sender
        WHERE root.id>? AND root.created>=coalesce(?,root.created) AND {ROOT}
    ) SELECT * FROM directory""" + filters + " ORDER BY last_id DESC LIMIT ?", (*params,limit+1)))
    more = len(topics)>limit
    topics = topics[:limit]
    by_id = {t["id"]: t for t in topics}
    for t in topics:
        t["participants"] = []
    if topics:
        slots = ",".join("?" for _ in topics)
        people = db.execute(VISIBLE + f""", selected AS (SELECT * FROM visible WHERE thread IN ({slots})),
            people AS (SELECT thread,sender member FROM selected UNION SELECT thread,target FROM selected)
            SELECT p.thread,m.name FROM people p JOIN members m ON m.id=p.member ORDER BY p.thread,m.joined,m.id""",
                            (gid,start,*by_id))
        for thread, name in people:
            by_id[thread]["participants"].append(name)
    return {"topics": topics, "more": more}
