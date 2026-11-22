"""Channel rules and the last version applied by each Agent."""
from .common import Problem, integer, public_text


GUIDE = "AGENTS.md"


def latest(db, gid):
    row = db.execute("""SELECT version,body,updated FROM announcements
        WHERE gid=? ORDER BY version DESC LIMIT 1""", (gid,)).fetchone()
    return dict(row) if row else None


def receipts(db, gid, version):
    return [row["member"] for row in db.execute("""SELECT p.member FROM announcement_guide_progress p
        JOIN members m ON m.id=p.member WHERE m.gid=? AND m.active=1 AND p.version>=?
        ORDER BY p.member""", (gid, version))]


def publish(db, gid, data, now):
    expected = integer(data, "version", low=0)
    if "body" not in data:
        raise Problem("请提供公告正文")
    body = public_text(data, "body", 6000, optional=True)
    current = latest(db, gid)
    version = current["version"] if current else 0
    if expected <= version and body == (current["body"] if current else ""):
        return current
    if expected != version:
        raise Problem("公告已更新，请读取最新版本后再发布。", 409, "announcement_changed")
    db.execute("INSERT INTO announcements VALUES (?,?,?,?)", (gid, version + 1, body, now))
    return {"version": version + 1, "body": body, "updated": now}


def start_at_current(db, gid, member):
    # New members receive the current version and the changes published after they join.
    db.execute("""INSERT INTO announcement_guide_progress(member,version)
        SELECT ?,version-1 FROM announcements WHERE gid=? AND version>1
        ORDER BY version DESC LIMIT 1""", (member, gid))


def pending(db, member):
    row = db.execute("""SELECT version,body,updated FROM announcements WHERE gid=?
        AND version>coalesce((SELECT version FROM announcement_guide_progress WHERE member=?),0)
        ORDER BY version LIMIT 1""", (member["gid"], member["id"])).fetchone()
    if row:
        return {"kind": "announcement_updated", "file": GUIDE, "group": member["gid"], "member": member["id"],
                "group_name": member["group_name"], **dict(row)}
    return None


def acknowledge(db, member, data):
    if data.get("file") != GUIDE:
        raise Problem("请使用新版监听器同步到 AGENTS.md 后回执。", 409, "announcement_file_changed")
    version = integer(data, "version")
    row = db.execute("SELECT version FROM announcement_guide_progress WHERE member=?", (member["id"],)).fetchone()
    applied = row["version"] if row else 0
    if version > applied:
        next_version = pending(db, member)
        if not next_version or version != next_version["version"]:
            raise Problem("请先同步本项目待接收的上一版公告。", 409)
        db.execute("""INSERT INTO announcement_guide_progress VALUES (?,?)
            ON CONFLICT(member) DO UPDATE SET version=excluded.version""", (member["id"], version))
        db.execute("""INSERT INTO channel_changes VALUES (?,1)
            ON CONFLICT(gid) DO UPDATE SET revision=revision+1""", (member["gid"],))
    return pending(db, member)
