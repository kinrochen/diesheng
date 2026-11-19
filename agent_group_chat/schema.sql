PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS owners (
    id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS logins (
    key_hash TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES owners, until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES owners, name TEXT NOT NULL,
    purpose TEXT NOT NULL, created REAL NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
    UNIQUE(owner, name)
);
CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY, gid TEXT NOT NULL REFERENCES groups, agent TEXT NOT NULL,
    name TEXT NOT NULL, role TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
    joined REAL NOT NULL, start_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    challenge TEXT NOT NULL, verified REAL, seen REAL, auto_seen REAL, paused INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS active_name ON members(gid, name) WHERE active=1;
CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY, gid TEXT NOT NULL REFERENCES groups, code_hash TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL, role TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0, used_by TEXT REFERENCES members
);
CREATE TABLE IF NOT EXISTS channel_links (
    gid TEXT PRIMARY KEY REFERENCES groups, nonce TEXT NOT NULL,
    code_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS human_invites (
    id TEXT PRIMARY KEY, gid TEXT NOT NULL REFERENCES groups, code_hash TEXT UNIQUE NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
    used_by TEXT REFERENCES members
);
CREATE TABLE IF NOT EXISTS browser_sessions (
    key_hash TEXT PRIMARY KEY, member TEXT NOT NULL UNIQUE REFERENCES members, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    member TEXT NOT NULL REFERENCES members, sid TEXT NOT NULL, seen REAL NOT NULL,
    PRIMARY KEY(member,sid)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, gid TEXT NOT NULL REFERENCES groups,
    kind TEXT NOT NULL, sender TEXT NOT NULL REFERENCES members, target TEXT REFERENCES members,
    sid TEXT NOT NULL, body TEXT NOT NULL, evidence TEXT NOT NULL, parent INTEGER,
    topic INTEGER, created REAL NOT NULL, request TEXT NOT NULL, fingerprint TEXT NOT NULL, accepted REAL,
    UNIQUE(sender,request)
);
CREATE INDEX IF NOT EXISTS history ON messages(gid,id);
CREATE INDEX IF NOT EXISTS thread_messages ON messages(gid,topic,id);
CREATE UNIQUE INDEX IF NOT EXISTS one_answer ON messages(parent) WHERE kind='answer';
-- Read-only provenance for replies already sent by earlier releases.
CREATE TABLE IF NOT EXISTS quick_answers (
    mid INTEGER PRIMARY KEY REFERENCES messages ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS deliveries (
    mid INTEGER PRIMARY KEY REFERENCES messages ON DELETE CASCADE, member TEXT NOT NULL REFERENCES members,
    status TEXT NOT NULL, sid TEXT, ticket TEXT, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_queue ON deliveries(member,status,mid);
CREATE TABLE IF NOT EXISTS channel_changes (
    gid TEXT PRIMARY KEY REFERENCES groups, revision INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS channel_message_added AFTER INSERT ON messages BEGIN
    INSERT INTO channel_changes VALUES (NEW.gid,1)
    ON CONFLICT(gid) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER IF NOT EXISTS channel_receipt_changed AFTER UPDATE OF status ON deliveries
WHEN NEW.status!=OLD.status BEGIN
    INSERT INTO channel_changes VALUES ((SELECT gid FROM messages WHERE id=NEW.mid),1)
    ON CONFLICT(gid) DO UPDATE SET revision=revision+1;
END;
-- A group question is shown once. Each recipient still owns an ordinary,
-- independently claimable question.
CREATE TABLE IF NOT EXISTS discussion_mail (
    question INTEGER PRIMARY KEY REFERENCES messages ON DELETE CASCADE,
    discussion INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS mail_by_discussion ON discussion_mail(discussion);
CREATE TABLE IF NOT EXISTS delivery_errors (
    mid INTEGER PRIMARY KEY REFERENCES deliveries ON DELETE CASCADE, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY, auto_answers INTEGER NOT NULL DEFAULT 0
);
