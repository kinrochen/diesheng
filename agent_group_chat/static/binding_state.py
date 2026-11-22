"""Restore or retire a project's own saved Diesheng bindings."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit


GUIDES = {"AGENTS.md": ("codex", "deepseek"), "AGENTS.override.md": ("codex",), "CLAUDE.local.md": ("claude",)}
CLIENTS = ("codex", "claude", "deepseek")
ANNOUNCEMENT_GUIDE = "AGENTS.md"
ANNOUNCEMENT_SCOPE = "适用于该频道的协作。以该频道版本号最大的公告为准。".encode()
LEGACY_ANNOUNCEMENT = re.compile(
    rb"(?m)^(?P<header><!-- diesheng-announcement (?P<group>[0-9a-f]{32}) [1-9][0-9]* -->\r?\n"
    + "## 叠声群公告 · ".encode()
    + rb"[^\r\n]+\r?\n)\r?\n(?P<rules>(?:>(?: [^\r\n]*)?(?:\r?\n|$))+)"
)
MANAGED = re.compile(rb"(?ms)^<!-- diesheng-project-v1 -->\r?\n.*?^<!-- /diesheng-project-v1 -->(?:\r?\n|$)")
LEGACY = ("\n<!-- diesheng-collaboration-v2 -->\n新版叠声协作事件先 --read，再由当前模型选择通知中的 "
          "--ask、--followup、--reply 或 --finish。同伴的结果会自动送回。旧通知沿用原操作。\n").encode()
SESSION_FILE = re.compile(r"[0-9a-f-]{36}\.(?:id|wake\.json)")


def project_root():
    try:
        git = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except FileNotFoundError:
        return Path.cwd().resolve()
    return Path(git.stdout.strip()).resolve() if git.returncode == 0 else Path.cwd().resolve()


def project_state(project):
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home()/".local/state")
    directory = (state/"diesheng").resolve()
    if not state.is_absolute() or directory.is_relative_to(project):
        raise ValueError("请使用工作区以外的叠声私有状态目录")
    return directory/hashlib.sha256(os.fsencode(project)).hexdigest()


def read_binding(record, project=None):
    record = record.expanduser()
    if record.name != "channel.json" or record.is_symlink():
        raise ValueError("请选择本机保存的 channel.json")
    record = record.resolve(strict=True)
    data = json.loads(record.read_text())
    root = Path(project or data.get("project") or project_root()).resolve()
    folder = record.parent
    agent = data["agent"]
    if (agent not in CLIENTS or folder.parent != project_state(root)/agent
            or not re.fullmatch(r"[0-9a-f]{64}", folder.name) or folder.stat().st_uid != os.getuid()):
        raise ValueError("绑定与当前项目的私有状态目录不匹配")
    url = urlsplit(data["base"])
    if (url.scheme not in ("http", "https") or not url.hostname or url.path not in ("", "/")
            or url.query or url.fragment or url.username is not None or url.password is not None
            or not all(re.fullmatch(r"[0-9a-f]{32}", data[k]) for k in ("group", "member"))):
        raise ValueError("本机绑定格式无效")
    return record, data, root


def strip_managed(guide):
    if not guide.is_file() or guide.is_symlink():
        return
    previous = guide.read_bytes()
    text = previous.replace(LEGACY, b"")
    note = MANAGED.search(text)
    if note:
        before, after = text[:note.start()], text[note.end():]
        text = before + after if after else before.removesuffix(b"\n\n")
    if text == previous:
        return
    write_document(guide, text, stat.S_IMODE(guide.stat().st_mode))


def write_document(guide, content, mode):
    with tempfile.NamedTemporaryFile(dir=guide.parent, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            os.fchmod(temporary.fileno(), mode)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(pending, guide)
        finally:
            pending.unlink(missing_ok=True)


def read_document(guide, *, writable=False):
    access = os.O_RDWR if writable else os.O_RDONLY
    try:
        descriptor = os.open(guide, access | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return b"", 0o600
    with os.fdopen(descriptor, "rb") as document:
        mode = os.fstat(document.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("公告同步需要普通 Markdown 文件")
        return document.read(), stat.S_IMODE(mode)


def append_announcement_blocks(guide, blocks):
    previous, mode = read_document(guide, writable=True)
    markers = set(previous.splitlines())
    content = bytearray(previous)
    for block in blocks:
        marker = block.splitlines()[0]
        if marker in markers:
            continue
        if content:
            content.extend(b"\n" if content.endswith(b"\n") else b"\n\n")
        content.extend(block)
        markers.add(marker)
    if content != previous:
        write_document(guide, content, mode)


def migrate_announcements(root, group):
    legacy = root/"agent.md"
    if not legacy.is_file() or legacy.is_symlink():
        return
    previous, mode = read_document(legacy)
    matches = [match for match in LEGACY_ANNOUNCEMENT.finditer(previous) if match["group"] == group.encode()]
    if not matches:
        return
    blocks = [match["header"] + b"\n" + ANNOUNCEMENT_SCOPE + b"\n\n" + match["rules"] for match in matches]
    append_announcement_blocks(root/ANNOUNCEMENT_GUIDE, blocks)
    remaining = bytearray()
    start = 0
    for match in matches:
        remaining.extend(previous[start:match.start()])
        start = match.end()
    remaining.extend(previous[start:])
    if remaining.strip():
        write_document(legacy, remaining, mode)
    else:
        legacy.unlink()


def append_announcement(guide, group, name, version, body):
    marker = f"<!-- diesheng-announcement {group} {version} -->"
    title = " ".join(name.split())
    rules = "\n".join("> " + line for line in (body or "公告已撤下。").splitlines())
    heading = f"{marker}\n## 叠声群公告 · {title} · 第 {version} 版\n\n".encode()
    block = heading + ANNOUNCEMENT_SCOPE + b"\n\n" + (rules + "\n").encode()
    append_announcement_blocks(guide, [block])


def sync_announcements(record, notice, acknowledge, project=None):
    """Append the announced revisions before acknowledging each one."""
    if not notice or notice.get("kind") != "announcement_updated":
        return 0
    record, data, root = read_binding(record, project)
    synced, last_version = 0, 0
    while notice is not None:
        version, body, name = (notice.get(key) for key in ("version", "body", "group_name"))
        if (notice.get("kind") != "announcement_updated" or notice.get("file") != ANNOUNCEMENT_GUIDE
                or any(notice.get(key) != data[key] for key in ("group", "member"))
                or type(version) is not int or version <= last_version
                or not isinstance(body, str) or len(body) > 6000 or "\x00" in body
                or not isinstance(name, str) or len(name) > 64):
            raise ValueError("公告与当前频道绑定不匹配")
        with (project_state(root)/"bindings.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not record.is_file():
                raise OSError("本机频道绑定已结束")
            if not synced:
                migrate_announcements(root, data["group"])
            append_announcement(root/ANNOUNCEMENT_GUIDE, data["group"], name, version, body)
        notice = acknowledge(version)["notice"]
        synced, last_version = synced + 1, version
    return synced


def clean_binding(record, notice, project=None):
    if not record.exists():
        return
    record, data, root = read_binding(record, project)
    if (notice.get("kind") != "membership_ended" or notice.get("reason") not in ("left", "dissolved")
            or any(notice.get(k) != data[k] for k in ("group", "member"))):
        raise ValueError("清理事件与当前频道绑定不匹配")
    folder = record.parent
    state = project_state(root)
    with (state/"bindings.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not record.exists():
            return
        current = json.loads(record.read_text())
        if any(current.get(k) != data[k] for k in ("base", "agent", "group", "member")):
            return
        for name, clients in GUIDES.items():
            if data["agent"] not in clients:
                continue
            if not any(other != record for client in clients for other in (state/client).glob("*/channel.json")):
                strip_managed(root/name)
        sessions = folder/"sessions"
        if sessions.is_dir() and not sessions.is_symlink():
            for item in sessions.iterdir():
                if item.name in ("listener.lock", "private-setup-notified") or SESSION_FILE.fullmatch(item.name):
                    item.unlink(missing_ok=True)
            if not any(sessions.iterdir()):
                sessions.rmdir()
        for name in ("private.header", "private.listener.lock", "auth.header", "channel.json"):
            (folder/name).unlink(missing_ok=True)
        if not any(folder.iterdir()):
            folder.rmdir()


def check_binding(record, project=None, *, leave=False, session=None):
    record, data, root = read_binding(record, project)
    auth = record.with_name("auth.header")
    if auth.is_symlink() or not re.fullmatch(r"Authorization: Bearer [A-Za-z0-9_-]{40,100}", auth.read_text().strip()):
        raise ValueError("本机成员凭据格式无效")
    base, sid = data["base"].rstrip("/"), session or secrets.token_hex(12)
    action = "leave" if leave else "context"
    cmd = ["curl", "-sS", "--connect-timeout", "4", "--max-time", "10", "-w", "\n%{http_code}",
           "-H", "@"+str(auth), "-H", "Content-Type: application/json", "--data-binary", "@-"]
    if base.startswith("http://"):
        cmd[1:1] = ["--noproxy", urlsplit(base).hostname]

    def request(action, **fields):
        call = subprocess.run([*cmd, base+"/api/project/"+action],
                              input=json.dumps({"session": sid, **fields}), text=True, capture_output=True)
        content, _, status = call.stdout.rpartition("\n")
        if call.returncode or status != "200":
            return status or "offline", None
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("服务返回了无效的绑定信息")
        return status, result

    fields = {"lifecycle": True}
    if leave:
        fields.update(group=data["group"], member=data["member"])
    else:
        fields["announcements"] = ANNOUNCEMENT_GUIDE
    status, result = request(action, **fields)
    if result is None:
        return {"base":base,"group":data["group"],"status":status}
    notice = result.get("notice")
    if isinstance(notice, dict) and notice.get("kind") == "membership_ended":
        clean_binding(record, notice, root)
        return {"group":data["group"],"status":notice["reason"],"cleaned":True}
    if leave:
        raise ValueError("请升级服务器后再确认并清理已退出的绑定")
    if isinstance(notice, dict) and notice.get("kind") == "announcement_updated":
        def acknowledge(version):
            _, response = request("announcement-ack", version=version, file=ANNOUNCEMENT_GUIDE)
            if response is None:
                raise OSError("公告回执待重试")
            return response

        try:
            result["announcements_synced"] = sync_announcements(record, notice, acknowledge, root)
            result.pop("notice")
        except (OSError, ValueError, KeyError):
            result["announcement_sync"] = "pending"
    return {"base":base,"auth_file":str(auth),"session":sid,"context":result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("restore", "check", "leave"))
    parser.add_argument("--project", type=Path, help="原项目根目录")
    parser.add_argument("--client", choices=CLIENTS, default="codex")
    parser.add_argument("--binding", type=Path, help="原绑定的 channel.json")
    parser.add_argument("--session", help="本轮会话编号，可用于 check")
    args = parser.parse_args()
    if args.session is not None and (args.action != "check" or not 0 < len(args.session) <= 100):
        parser.error("--session 仅用于 check，需填写本轮会话编号")
    os.umask(0o077)
    if args.action == "restore":
        root = (args.project or project_root()).resolve()
        for record in sorted((project_state(root)/args.client).glob("*/channel.json")):
            try:
                print(json.dumps(check_binding(record, root), ensure_ascii=False))
            except (OSError, ValueError, KeyError) as exc:
                print(json.dumps({"status":"local_error","error":type(exc).__name__}, ensure_ascii=False))
        return
    if not args.binding:
        parser.error("请选择当前项目的绑定文件")
    try:
        result = check_binding(args.binding, args.project, leave=args.action == "leave", session=args.session)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"无法确认或清理绑定。{type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False))
    if args.action == "leave" and not result.get("cleaned"):
        parser.exit(1, "未能确认退出，请稍后重试。\n")


if __name__ == "__main__":
    main()
