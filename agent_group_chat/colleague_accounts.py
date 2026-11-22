"""Server-owned colleague accounts, sessions, and channel memberships."""
from contextlib import closing, contextmanager
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time

from .common import Problem, digest, ident, rows, string, token, transaction


SESSION_LIFETIME = 30 * 86400
PASSWORD_ROUNDS = 600_000
MIN_PASSWORD_LENGTH = 8
MEMBER_ACTIONS = {"preview", "register", "join", "legacy_status", "upgrade", "claim"}


def _username(data):
    user = string(data,"username",32)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,31}",user):
        raise Problem("账号需要 3–32 个英文字母、数字、点、下划线或短横线，并以字母或数字开头", 400, "invalid_username")
    return user.lower()


def _password(password, *, new=False):
    minimum = MIN_PASSWORD_LENGTH if new else 1
    if (not isinstance(password,str) or not minimum <= len(password) <= 128
            or "\x00" in password or not password.strip()):
        raise Problem(f"请使用 {minimum}–128 个字符的密码" if new else "请填写密码", 400, "invalid_password")
    return password.encode()


def _hash(password, salt, rounds):
    return hashlib.pbkdf2_hmac("sha256", password, salt, rounds)


def create(db, data):
    user,password = _username(data),_password(data.get("password"),new=True)
    if db.execute("SELECT 1 FROM colleague_accounts WHERE username=?",(user,)).fetchone():
        raise Problem("这个账号已存在，请登录或填写另一个账号", 409, "username_taken")
    salt = secrets.token_bytes(16)
    account = {"id":ident(),"username":user}
    db.execute("INSERT INTO colleague_accounts VALUES (?,?,?,?,?)",
               (account["id"],user,salt,_hash(password,salt,PASSWORD_ROUNDS),PASSWORD_ROUNDS))
    return account


class LoginLimit:
    def __init__(self):
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.attempts = {}

    @contextmanager
    def password_check(self, source, username):
        key, now = (source,username), time.monotonic()
        with self.lock:
            if len(self.attempts) >= 1024:
                self.attempts = {k:v for k,v in self.attempts.items() if v[1] > now}
            count, until = self.attempts.get(key,(0,now+60))
            if until <= now:
                count, until = 0, now+60
            if count >= 8 or (key not in self.attempts and len(self.attempts) >= 1024) or not self.slots.acquire(blocking=False):
                raise Problem("登录尝试较多，请稍后再试", 429)
            self.attempts[key] = count+1,until
        try:
            yield
            with self.lock:
                self.attempts.pop(key,None)
        finally:
            self.slots.release()


def authenticate(path, data, limit, source):
    user,password = _username(data),_password(data.get("password"))
    with limit.password_check(source,user):
        with closing(sqlite3.connect(path)) as db:
            stored = db.execute("SELECT id,salt,password_hash,rounds FROM colleague_accounts WHERE username=?",(user,)).fetchone()
        account,salt,expected,rounds = stored or (None,bytes(16),bytes(32),PASSWORD_ROUNDS)
        valid = hmac.compare_digest(_hash(password,salt,rounds),expected)
        if not valid or account is None:
            raise Problem("账号或密码不正确", 401, "invalid_credentials")
    return {"id":account,"username":user,"password_hash":expected}


def require_unchanged(db, verified):
    # Password verification runs outside the write transaction.
    if not db.execute("SELECT 1 FROM colleague_accounts WHERE id=? AND username=? AND password_hash=?",
                      (verified["id"],verified["username"],verified["password_hash"])).fetchone():
        raise Problem("账号登录信息已更新，请刷新页面后重试",409,"account_changed")


def current(db, cookie, now):
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}",cookie):
        return None
    return db.execute("""SELECT a.id,a.username FROM colleague_sessions s
        JOIN colleague_accounts a ON a.id=s.account WHERE s.key_hash=? AND s.expires>?""",(digest(cookie),now)).fetchone()


def update(path, data, cookie, now, limit, source):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        account = current(db,cookie,now)
    if not account:
        raise Problem("请先登录同事账号",401)
    if data.get("current_username") != account["username"]:
        raise Problem("账号登录信息已更新，请刷新页面后重试",409,"account_changed")

    username = _username(data)
    new_value = data.get("new_password","")
    new_password = _password(new_value,new=True) if new_value != "" else None
    if username == account["username"] and new_password is None:
        raise Problem("请修改登录账号名或填写新密码",400,"no_changes")

    try:
        verified = authenticate(path,{"username":account["username"],"password":data.get("current_password")},limit,source)
    except Problem as error:
        if error.code == "invalid_credentials":
            raise Problem("当前密码不正确",401,"invalid_current_password") from error
        if error.code == "invalid_password":
            raise Problem("请填写当前密码",400,"invalid_current_password") from error
        raise
    if new_password is not None:
        salt = secrets.token_bytes(16)
        password_hash = _hash(new_password,salt,PASSWORD_ROUNDS)

    with transaction(path) as db:
        active = current(db,cookie,now)
        if not active or active["id"] != verified["id"]:
            raise Problem("请先登录同事账号",401)
        require_unchanged(db,verified)
        if username != active["username"] and db.execute("SELECT 1 FROM colleague_accounts WHERE username=?",(username,)).fetchone():
            raise Problem("这个账号名已被使用，请换一个",409,"username_taken")
        if new_password is None:
            db.execute("UPDATE colleague_accounts SET username=? WHERE id=?",(username,active["id"]))
        else:
            db.execute("UPDATE colleague_accounts SET username=?,salt=?,password_hash=?,rounds=? WHERE id=?",
                       (username,salt,password_hash,PASSWORD_ROUNDS,active["id"]))
            db.execute("DELETE FROM colleague_sessions WHERE account=? AND key_hash<>?",(active["id"],digest(cookie)))
    return {"username":username}


def sign_out(db, cookie):
    db.execute("DELETE FROM colleague_sessions WHERE key_hash=?",(digest(cookie),))


def sign_in(db, account, previous, now):
    sign_out(db,previous)
    cookie = token()
    db.execute("INSERT INTO colleague_sessions VALUES (?,?,?)",(digest(cookie),account["id"],now+SESSION_LIFETIME))
    return cookie


def member(db, account, gid):
    return db.execute("""SELECT m.*,g.paused AS group_paused FROM colleague_memberships c
        JOIN members m ON m.id=c.member AND m.gid=c.gid JOIN groups g ON g.id=c.gid
        WHERE c.account=? AND c.gid=? AND m.active=1 AND m.agent='human'""",(account,gid)).fetchone()


def link(db, account, gid, member_id):
    if db.execute("SELECT 1 FROM colleague_memberships WHERE account=? AND gid=?",(account,gid)).fetchone():
        raise Problem("这个账号在本频道已有成员身份", 409, "account_has_membership")
    db.execute("INSERT INTO colleague_memberships VALUES (?,?,?)",(account,gid,member_id))
    db.execute("DELETE FROM browser_sessions WHERE member=?",(member_id,))
    db.execute("DELETE FROM member_recovery WHERE member=?",(member_id,))


def groups(db, account):
    return rows(db.execute("""SELECT g.id,g.name,g.purpose,g.paused,g.created,
        EXISTS(SELECT 1 FROM channel_admins a WHERE a.member=m.id) AS can_manage,
        (SELECT count(*) FROM members WHERE gid=g.id AND active=1) AS members
        FROM colleague_memberships c JOIN groups g ON g.id=c.gid JOIN members m ON m.id=c.member
        WHERE c.account=? AND m.active=1 AND NOT EXISTS (SELECT 1 FROM closed_groups x WHERE x.gid=g.id)
        ORDER BY g.name,g.id""", (account,)))
