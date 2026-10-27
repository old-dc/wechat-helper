"""发现层：把实时拉取的会话列表 / 群成员落库。

- 会话列表 → chats 表（含未读/置顶/免打扰/预览）
- 群成员   → chat_members 表
把"连微信拉数据"（wechat.py）和"写库"（db.py）黏起来，供 cli/watch 复用。
"""
from __future__ import annotations

import sqlite3

from . import db as dbm
from . import wechat


def _guess_type(session: dict) -> str:
    """从预览启发式猜 group/contact：预览含 '名字: ' 多半是群。不确定给 unknown。"""
    preview = session.get("preview") or ""
    head = preview.split("\n", 1)[0]
    if ": " in head[:24] or "：" in head[:24]:
        return "group"
    return "unknown"


def discover_sessions(conn: sqlite3.Connection, wx, *, scroll: bool = True) -> list[dict]:
    """拉取会话列表并落 chats 表，返回会话列表。"""
    sessions = wechat.list_sessions(wx, scroll=scroll)
    for s in sessions:
        dbm.upsert_chat_meta(
            conn, s["name"],
            type=_guess_type(s), origin="session",
            pinned=1 if s["pinned"] else 0,
            muted=1 if s["muted"] else 0,
            unread=s["unread"],
            preview=s["preview"],
            last_time_label=s.get("time_label"),
        )
    dbm.record_sync_run(conn, "discover", "sessions", len(sessions), len(sessions))
    return sessions


def select_private_targets(session_names: list[str], contact_names, exclude=None) -> list[str]:
    """私聊抓取目标 = 会话列表 ∩ 通讯录 − 隐私黑名单，保持会话列表顺序、去重。"""
    contacts = set(contact_names)
    ex = set(exclude or [])
    seen, out = set(), []
    for n in session_names:
        if n in contacts and n not in ex and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def discover_contacts(conn: sqlite3.Connection, wx, *, scroll: bool = True) -> list[str]:
    """拉取通讯录全部个人联系人并落 contacts 表，返回联系人名列表。"""
    names = wechat.list_contacts(wx, scroll=scroll)
    dbm.upsert_contacts(conn, names)
    dbm.record_sync_run(conn, "contacts", "address_book", len(names), len(names))
    return names


def discover_members(conn: sqlite3.Connection, wx, group: str) -> list[str]:
    """拉取群成员并整体替换落 chat_members 表，返回成员列表。"""
    members = wechat.list_group_members(wx, group)
    dbm.replace_members(conn, group, members)
    dbm.upsert_chat_meta(conn, group, type="group", origin="members")
    dbm.record_sync_run(conn, "members", group, len(members), len(members))
    return members
