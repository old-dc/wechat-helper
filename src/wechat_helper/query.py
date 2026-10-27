"""只读查询层：全文搜索（FTS≥3字 / LIKE<3字）、最近、会话、成员、日报。

所有查询都接受 exclude（隐私黑名单的会话名），统一排除。
"""
from __future__ import annotations

import sqlite3

from . import db as dbm


def search(conn: sqlite3.Connection, keyword: str, *, chat: str | None = None,
           limit: int = 50, exclude: list[str] | None = None) -> list[sqlite3.Row]:
    use_fts = dbm.has_fts(conn) and len(keyword) >= 3
    args: list = []
    if use_fts:
        sql = ("SELECT m.chat, m.content, m.sender, m.media_path, m.time_label, m.ts, m.source "
               "FROM messages_fts f JOIN messages m ON m.id = f.rowid "
               "WHERE messages_fts MATCH ?")
        args.append('"' + keyword.replace('"', '""') + '"')
        ex_clause, ex_args = dbm.excluded_sql(exclude or [], col="m.chat")
        if chat:
            sql += " AND m.chat = ?"; args.append(chat)
        if ex_clause:
            sql += f" AND {ex_clause}"; args += ex_args
        sql += " ORDER BY m.ts DESC LIMIT ?"; args.append(limit)
    else:
        sql = "SELECT chat, content, sender, media_path, time_label, ts, source FROM messages WHERE content LIKE ?"
        args.append(f"%{keyword}%")
        ex_clause, ex_args = dbm.excluded_sql(exclude or [], col="chat")
        if chat:
            sql += " AND chat = ?"; args.append(chat)
        if ex_clause:
            sql += f" AND {ex_clause}"; args += ex_args
        sql += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def recent(conn: sqlite3.Connection, *, chat: str | None = None, limit: int = 50,
           exclude: list[str] | None = None) -> list[sqlite3.Row]:
    sql = "SELECT chat, content, sender, media_path, time_label, ts, source FROM messages"
    args: list = []
    conds = []
    if chat:
        conds.append("chat = ?"); args.append(chat)
    ex_clause, ex_args = dbm.excluded_sql(exclude or [], col="chat")
    if ex_clause:
        conds.append(ex_clause); args += ex_args
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def list_chats(conn: sqlite3.Connection, *, limit: int = 200,
               exclude: list[str] | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT name, type, origin, pinned, muted, unread, preview, "
           "last_time_label, msg_count, last_seen FROM chats")
    args: list = []
    ex_clause, ex_args = dbm.excluded_sql(exclude or [], col="name")
    if ex_clause:
        sql += f" WHERE {ex_clause}"; args += ex_args
    sql += " ORDER BY pinned DESC, last_seen DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def contacts(conn: sqlite3.Connection, *, limit: int = 2000) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM contacts ORDER BY name LIMIT ?", (limit,)
    ).fetchall()
    return [r["name"] for r in rows]


def members(conn: sqlite3.Connection, group: str) -> list[str]:
    rows = conn.execute(
        "SELECT member FROM chat_members WHERE group_name=? ORDER BY member", (group,)
    ).fetchall()
    return [r["member"] for r in rows]


def sender_names(conn: sqlite3.Connection, chat: str) -> list[str]:
    """拆多选状态下"发送者 正文"用的已知名字：会话名（私聊对方）、群成员、通讯录，长的在前。"""
    rows = conn.execute("SELECT member FROM chat_members WHERE group_name=? UNION SELECT name FROM contacts",
                        (chat,)).fetchall()
    return sorted({chat, *(r[0] for r in rows)}, key=len, reverse=True)


def digest(conn: sqlite3.Connection, *, limit: int = 50, unread_only: bool = False,
           exclude: list[str] | None = None) -> list[sqlite3.Row]:
    """面向 agent 的会话概览：未读/预览/最近时间/已存消息数。"""
    sql = ("SELECT name, type, unread, pinned, muted, preview, last_time_label, msg_count "
           "FROM chats")
    args: list = []
    conds = []
    if unread_only:
        conds.append("unread > 0")
    ex_clause, ex_args = dbm.excluded_sql(exclude or [], col="name")
    if ex_clause:
        conds.append(ex_clause); args += ex_args
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY unread DESC, pinned DESC, last_seen DESC LIMIT ?"; args.append(limit)
    return conn.execute(sql, args).fetchall()


def last_event_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def events_after(conn: sqlite3.Connection, after_id: int, limit: int = 50) -> list[sqlite3.Row]:
    """watch 写入的新消息事件（按事件 id 递增），带消息正文。供长轮询增量读取。"""
    return conn.execute(
        "SELECT e.id, e.chat, m.content, m.sender, m.media_path, m.ts, m.is_at_me FROM events e "
        "JOIN messages m ON m.id = e.message_id "
        "WHERE e.kind = 'new_message' AND e.id > ? ORDER BY e.id LIMIT ?",
        (after_id, limit),
    ).fetchall()
