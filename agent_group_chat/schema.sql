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
CREATE TABLE IF NOT EXISTS closed_groups (
    gid TEXT PRIMARY KEY REFERENCES groups, closed REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY, gid TEXT NOT NULL REFERENCES groups, agent TEXT NOT NULL,
    name TEXT NOT NULL, role TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
    joined REAL NOT NULL, start_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    challenge TEXT NOT NULL, verified REAL, seen REAL, auto_seen REAL, paused INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS active_name ON members(gid, name) WHERE active=1;
CREATE TABLE IF NOT EXISTS channel_admins (
    member TEXT PRIMARY KEY REFERENCES members ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS announcements (
    gid TEXT NOT NULL REFERENCES groups, version INTEGER NOT NULL,
    body TEXT NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY(gid,version)
);
CREATE TABLE IF NOT EXISTS announcement_progress (
    member TEXT PRIMARY KEY REFERENCES members ON DELETE CASCADE, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement_guide_progress (
    member TEXT PRIMARY KEY REFERENCES members ON DELETE CASCADE, version INTEGER NOT NULL
);
-- Earlier receipts tracked agent.md. Replay at least the current revision for AGENTS.md.
INSERT OR IGNORE INTO announcement_guide_progress(member,version)
SELECT p.member,min(p.version,max(a.version)-1)
FROM announcement_progress p JOIN members m ON m.id=p.member JOIN announcements a ON a.gid=m.gid
WHERE m.active=1 GROUP BY p.member;
CREATE TABLE IF NOT EXISTS agent_bindings (
    agent TEXT PRIMARY KEY REFERENCES members ON DELETE CASCADE,
    human TEXT NOT NULL REFERENCES members ON DELETE CASCADE,
    created REAL NOT NULL, CHECK(agent!=human)
);
CREATE INDEX IF NOT EXISTS agent_bindings_human ON agent_bindings(human,agent);
CREATE TABLE IF NOT EXISTS agent_binding_offers (
    agent TEXT NOT NULL REFERENCES members ON DELETE CASCADE,
    human TEXT NOT NULL REFERENCES members ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(agent,human)
);
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
CREATE TABLE IF NOT EXISTS colleague_accounts (
    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt BLOB NOT NULL,
    password_hash BLOB NOT NULL, rounds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS colleague_sessions (
    key_hash TEXT PRIMARY KEY, account TEXT NOT NULL REFERENCES colleague_accounts, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS colleague_memberships (
    account TEXT NOT NULL REFERENCES colleague_accounts, gid TEXT NOT NULL REFERENCES groups,
    member TEXT NOT NULL UNIQUE REFERENCES members, PRIMARY KEY(account,gid)
);
CREATE TABLE IF NOT EXISTS member_recovery (
    member TEXT NOT NULL REFERENCES members, kind TEXT NOT NULL CHECK(kind IN ('code','link')),
    key_hash TEXT NOT NULL UNIQUE, expires REAL, used REAL,
    PRIMARY KEY(member,kind)
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
-- Explicit mentions in channel broadcasts contribute to the personal view without a delivery.
CREATE TABLE IF NOT EXISTS message_mentions (
    mid INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
    member TEXT NOT NULL REFERENCES members,
    PRIMARY KEY(mid,member)
);
-- Read-only provenance for replies already sent by earlier releases.
CREATE TABLE IF NOT EXISTS quick_answers (
    mid INTEGER PRIMARY KEY REFERENCES messages ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS deliveries (
    mid INTEGER PRIMARY KEY REFERENCES messages ON DELETE CASCADE, member TEXT NOT NULL REFERENCES members,
    status TEXT NOT NULL, sid TEXT, ticket TEXT, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_queue ON deliveries(member,status,mid);
-- A ticket can make one decision. Child replies use the stored assignment to return up the call chain.
CREATE TABLE IF NOT EXISTS agent_turns (
    event INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
    ticket TEXT NOT NULL, decision TEXT NOT NULL, fingerprint TEXT NOT NULL,
    result INTEGER UNIQUE REFERENCES messages ON DELETE SET NULL,
    assignment INTEGER REFERENCES messages ON DELETE CASCADE,
    PRIMARY KEY(event,ticket)
);
CREATE TABLE IF NOT EXISTS channel_changes (
    gid TEXT PRIMARY KEY REFERENCES groups, revision INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS channel_announcement_added AFTER INSERT ON announcements BEGIN
    INSERT INTO channel_changes VALUES (NEW.gid,1)
    ON CONFLICT(gid) DO UPDATE SET revision=revision+1;
END;
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

-- Private conversations have their own IDs, receipts and read positions.
CREATE TABLE IF NOT EXISTS private_runners (
    member TEXT PRIMARY KEY REFERENCES members, key_hash TEXT UNIQUE NOT NULL, seen REAL
);
CREATE TABLE IF NOT EXISTS private_setup_notices (
    member TEXT PRIMARY KEY REFERENCES members, id TEXT UNIQUE NOT NULL, created REAL NOT NULL,
    sid TEXT, lease_until REAL, notified REAL, deferred REAL
);
CREATE TABLE IF NOT EXISTS private_threads (
    id TEXT PRIMARY KEY, gid TEXT NOT NULL REFERENCES groups,
    starter TEXT NOT NULL REFERENCES members, peer TEXT NOT NULL REFERENCES members,
    title TEXT NOT NULL, created REAL NOT NULL, CHECK(starter!=peer)
);
CREATE INDEX IF NOT EXISTS private_starter ON private_threads(gid,starter);
CREATE INDEX IF NOT EXISTS private_peer ON private_threads(gid,peer);
CREATE TABLE IF NOT EXISTS private_topic_links (
    thread TEXT PRIMARY KEY REFERENCES private_threads ON DELETE CASCADE,
    topic INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS private_links_by_topic ON private_topic_links(topic);
CREATE TABLE IF NOT EXISTS private_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL REFERENCES private_threads ON DELETE CASCADE,
    sender TEXT NOT NULL REFERENCES members, body TEXT NOT NULL, created REAL NOT NULL,
    request TEXT NOT NULL, fingerprint TEXT NOT NULL,
    reply_to INTEGER UNIQUE REFERENCES private_messages ON DELETE SET NULL,
    UNIQUE(sender,request)
);
CREATE INDEX IF NOT EXISTS private_history ON private_messages(thread,id);
CREATE TABLE IF NOT EXISTS private_deliveries (
    message INTEGER PRIMARY KEY REFERENCES private_messages ON DELETE CASCADE,
    recipient TEXT NOT NULL REFERENCES members, status TEXT NOT NULL,
    sid TEXT, ticket_hash TEXT, until REAL
);
CREATE INDEX IF NOT EXISTS private_queue ON private_deliveries(recipient,status,message);
CREATE TABLE IF NOT EXISTS private_reads (
    thread TEXT NOT NULL REFERENCES private_threads ON DELETE CASCADE,
    member TEXT NOT NULL REFERENCES members, last_read INTEGER NOT NULL,
    PRIMARY KEY(thread,member)
);
