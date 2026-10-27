"""SQLite 数据层：建表、去重入库、全文索引、台账、成员、事件、隐私过滤。

消息来自 UI 自动化抓取：没有稳定消息 ID；发送者只有聊天窗口在多选状态下抓的才有。所以：
- 去重靠 dedup_key = sha1(chat + content + time_bucket)，不含发送者；之前没记到发送者的，再抓到时补上。
  scrape 用消息自带的时间标签作桶；watch 用观察时间的分钟桶。
- ponytail: 同一时间桶内内容完全相同的消息会被并为一条（无法区分真正重复发送）。
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    name            TEXT PRIMARY KEY,
    type            TEXT,           -- contact | group | unknown
    origin          TEXT,           -- session | scrape | watch（从哪发现的）
    pinned          INTEGER DEFAULT 0,
    muted           INTEGER DEFAULT 0,
    unread          INTEGER DEFAULT 0,
    preview         TEXT,           -- 会话列表里的最新一条预览（含发送者名）
    last_time_label TEXT,
    first_seen      INTEGER,
    last_seen       INTEGER,
    msg_count       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat       TEXT NOT NULL,
    content    TEXT NOT NULL,
    sender     TEXT,               -- 显示名（自己发的是自己的群昵称）；没在多选状态下抓的为空
    media_path TEXT,               -- 图片的截图文件（显示尺寸，监听截的）；没有为空
    msg_type   TEXT,               -- text | link | system
    time_label TEXT,
    ts         INTEGER,
    is_at_me   INTEGER DEFAULT 0,
    source     TEXT,               -- watch | scrape
    dedup_key  TEXT UNIQUE,
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat, ts);

CREATE TABLE IF NOT EXISTS chat_members (
    group_name TEXT NOT NULL,
    member     TEXT NOT NULL,      -- 显示名（群昵称/备注名），无 wxid
    updated_at INTEGER,
    PRIMARY KEY (group_name, member)
);

CREATE TABLE IF NOT EXISTS contacts (
    name       TEXT PRIMARY KEY,   -- 显示名（备注名优先，否则昵称），无 wxid
    origin     TEXT,               -- contacts（通讯录）
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,               -- scrape | watch | discover | members
    target     TEXT,
    items      INTEGER,
    inserted   INTEGER,
    started_at INTEGER,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,               -- new_message
    chat       TEXT,
    message_id INTEGER,
    ts         INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 回复草稿：外部 agent 经 MCP 起草，用户本人 CLI 审批后才发送
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat          TEXT NOT NULL,
    content       TEXT NOT NULL,
    reply_to      INTEGER,          -- 回复的 events.id（wait_new_messages 返回的 id），可空
    key_level     INTEGER,          -- 起草方密钥等级
    status        TEXT DEFAULT 'pending',  -- pending | sending | sent | failed | rejected
    final_content TEXT,             -- 实际发送内容（审批时可改）
    created_at    INTEGER,
    decided_at    INTEGER
);
"""

# FTS5 trigram：外部内容表，触发器自动同步。trigram 对 <3 字查询无效，
# query 层对短词回退 LIKE。建不出来（无 FTS5）时静默跳过，search 全走 LIKE。
_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


FILL_COLS = ("sender", "media_path")  # 后加的列：旧库补列；同一条消息再次入库时补上空着的


def open_db(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # watch 的回调在 wx4py 工作线程里执行，需 check_same_thread=False（调用方自行加锁串行写）
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for col in FILL_COLS:
        if col not in cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")  # 旧库补列
    try:
        conn.executescript(_FTS)
    except sqlite3.OperationalError:
        pass  # 无 FTS5：search 回退 LIKE
    conn.commit()
    return conn


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    return row is not None


def _dedup_key(chat: str, content: str, bucket: str) -> str:
    raw = f"{chat}\x00{content}\x00{bucket}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def upsert_messages(conn: sqlite3.Connection, rows: Iterable[dict], *,
                    emit_event: bool = False) -> int:
    """入库一批消息，返回实际新增条数。

    row: {chat, content, sender?, media_path?, msg_type?, time_label?, ts?, is_at_me?, source,
          bucket, chat_type?, dup_window?}
    ts 显式给 None = 时间未知（如抓到聊天最顶上没标签的），不冒充入库时刻。
    dup_window=(lo, hi)：同会话同内容、另一来源（监听/发送/抓取）在此时间窗内已有记录就跳过——
    同一条消息被不同途径各记一份时，去重键（时间桶算法不同）对不上，靠它兜住。
    已有的那条缺发送者/截图、这次带了，就补上（不算新增）。
    """
    now = int(time.time())
    inserted = 0
    cur = conn.cursor()
    for row in rows:
        chat = row.get("chat")
        content = row.get("content")
        if not chat or not content:
            continue
        bucket = row.get("bucket") or row.get("time_label") or ""
        key = _dedup_key(chat, content, bucket)
        ts = row.get("ts", now)
        ts = int(ts) if ts is not None else None
        fill = {c: row[c] for c in FILL_COLS if row.get(c)}
        fill_sql = (", ".join(f"{c} = COALESCE({c}, ?)" for c in fill)
                    + " WHERE {} = ? AND (" + " OR ".join(f"{c} IS NULL" for c in fill) + ")")
        win = row.get("dup_window")
        if win and (hit := cur.execute(
                "SELECT id FROM messages WHERE chat = ? AND content = ? AND source != ? "
                "AND ts BETWEEN ? AND ? LIMIT 1",
                (chat, content, row.get("source"), win[0], win[1])).fetchone()):
            if fill:
                cur.execute("UPDATE messages SET " + fill_sql.format("id"), (*fill.values(), hit[0]))
            continue
        cur.execute(
            """INSERT OR IGNORE INTO messages
               (chat, content, sender, media_path, msg_type, time_label, ts, is_at_me, source, dedup_key,
                created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (chat, content, row.get("sender"), row.get("media_path"), row.get("msg_type"),
             row.get("time_label"), ts, 1 if row.get("is_at_me") else 0, row.get("source"), key, now),
        )
        if cur.rowcount:
            inserted += 1
            msg_id = cur.lastrowid
            _touch_chat(cur, chat, row.get("chat_type"), ts if ts is not None else 0, None)
            if emit_event:
                cur.execute(
                    "INSERT INTO events (kind, chat, message_id, ts) VALUES ('new_message',?,?,?)",
                    (chat, msg_id, now),
                )
        elif fill:
            cur.execute("UPDATE messages SET " + fill_sql.format("dedup_key"), (*fill.values(), key))
    conn.commit()
    return inserted


def _touch_chat(cur: sqlite3.Cursor, chat: str, chat_type: Optional[str],
                ts: int, origin: Optional[str]) -> None:
    cur.execute(
        """INSERT INTO chats (name, type, origin, first_seen, last_seen, msg_count)
           VALUES (?,?,?,?,?,1)
           ON CONFLICT(name) DO UPDATE SET
             last_seen = max(COALESCE(chats.last_seen,0), excluded.last_seen),
             msg_count = chats.msg_count + 1,
             type      = COALESCE(chats.type, excluded.type),
             origin    = COALESCE(chats.origin, excluded.origin)""",
        (chat, chat_type or "unknown", origin, ts, ts),
    )


def upsert_chat_meta(conn: sqlite3.Connection, name: str, *, type: str | None = None,
                     origin: str | None = None, pinned: int | None = None,
                     muted: int | None = None, unread: int | None = None,
                     preview: str | None = None, last_time_label: str | None = None,
                     last_seen: int | None = None) -> None:
    """发现/更新会话元信息（来自会话列表），不改 msg_count。"""
    conn.execute(
        """INSERT INTO chats (name, type, origin, pinned, muted, unread, preview,
                              last_time_label, first_seen, last_seen, msg_count)
           VALUES (?,?,?,?,?,?,?,?,?,?,0)
           ON CONFLICT(name) DO UPDATE SET
             type            = COALESCE(excluded.type, chats.type),
             origin          = COALESCE(chats.origin, excluded.origin),
             pinned          = COALESCE(excluded.pinned, chats.pinned),
             muted           = COALESCE(excluded.muted, chats.muted),
             unread          = COALESCE(excluded.unread, chats.unread),
             preview         = COALESCE(excluded.preview, chats.preview),
             last_time_label = COALESCE(excluded.last_time_label, chats.last_time_label),
             last_seen       = max(COALESCE(chats.last_seen,0), COALESCE(excluded.last_seen,0))""",
        (name, type, origin, pinned, muted, unread, preview, last_time_label,
         last_seen or int(time.time()), last_seen or int(time.time())),
    )
    conn.commit()


def replace_members(conn: sqlite3.Connection, group: str, members: list[str]) -> int:
    """整体替换某群的成员名单，返回成员数。"""
    now = int(time.time())
    conn.execute("DELETE FROM chat_members WHERE group_name=?", (group,))
    conn.executemany(
        "INSERT OR IGNORE INTO chat_members (group_name, member, updated_at) VALUES (?,?,?)",
        [(group, m, now) for m in members if m],
    )
    conn.commit()
    return len(members)


def upsert_contacts(conn: sqlite3.Connection, names: list[str], origin: str = "contacts") -> int:
    """落通讯录联系人（按显示名）。返回传入条数。"""
    now = int(time.time())
    conn.executemany(
        "INSERT INTO contacts (name, origin, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at, "
        "origin=COALESCE(contacts.origin, excluded.origin)",
        [(n, origin, now) for n in names if n],
    )
    conn.commit()
    return len([n for n in names if n])


def record_sync_run(conn: sqlite3.Connection, kind: str, target: str,
                    items: int, inserted: int, note: str = "") -> None:
    conn.execute(
        "INSERT INTO sync_runs (kind, target, items, inserted, started_at, note) VALUES (?,?,?,?,?,?)",
        (kind, target, items, inserted, int(time.time()), note),
    )
    conn.commit()


def excluded_sql(exclude: list[str], col: str = "chat") -> tuple[str, list]:
    """生成隐私黑名单过滤片段：返回 (SQL条件, 参数)。空名单返回 ('', [])。"""
    names = [n for n in (exclude or []) if n]
    if not names:
        return "", []
    placeholders = ",".join("?" * len(names))
    return f"{col} NOT IN ({placeholders})", names


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def find_echo(conn: sqlite3.Connection, chat: str, content: str, since_ts: int,
              claimed=()) -> Optional[int]:
    """本助手近期发出的同内容消息（source=send）里还没被认领的一条 id。
    监听据此认出"自己发的消息回流"：发送是另一个进程，wx4py 进程内的过滤认不出来。"""
    for (mid,) in conn.execute(
            "SELECT id FROM messages WHERE chat = ? AND content = ? AND source = 'send' AND ts >= ? ORDER BY id",
            (chat, content, since_ts)):
        if mid not in claimed:
            return mid
    return None


def fill_sender(conn: sqlite3.Connection, msg_id: int, sender: Optional[str]) -> None:
    """给没记发送者的消息补上（如本助手发出的消息，监听看到回流时才知道自己在这个群里的显示名）。"""
    if sender:
        conn.execute("UPDATE messages SET sender = ? WHERE id = ? AND sender IS NULL", (sender, msg_id))
        conn.commit()


def add_draft(conn: sqlite3.Connection, chat: str, content: str, *,
              reply_to: Optional[int] = None, key_level: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO drafts (chat, content, reply_to, key_level, created_at) VALUES (?,?,?,?,?)",
        (chat, content, reply_to, key_level, int(time.time())))
    conn.commit()
    return cur.lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT d.*, m.content AS reply_to_content FROM drafts d "
        "LEFT JOIN events e ON e.id = d.reply_to LEFT JOIN messages m ON m.id = e.message_id "
        "WHERE d.id = ?", (draft_id,)).fetchone()


def list_drafts(conn: sqlite3.Connection, status: Optional[str] = "pending",
                limit: int = 50) -> list[sqlite3.Row]:
    sql = ("SELECT d.*, m.content AS reply_to_content FROM drafts d "
           "LEFT JOIN events e ON e.id = d.reply_to LEFT JOIN messages m ON m.id = e.message_id")
    args: list = []
    if status:
        sql += " WHERE d.status = ?"; args.append(status)
    sql += " ORDER BY d.id DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def decide_draft(conn: sqlite3.Connection, draft_id: int, status: str,
                 final_content: Optional[str] = None, *, from_status: str = "pending") -> bool:
    """仅当草稿处于 from_status 时改状态，返回是否改到（防重复审批/重复发送）。"""
    cur = conn.execute(
        "UPDATE drafts SET status = ?, final_content = COALESCE(?, final_content), decided_at = ? "
        "WHERE id = ? AND status = ?",
        (status, final_content, int(time.time()), draft_id, from_status))
    conn.commit()
    return cur.rowcount == 1
