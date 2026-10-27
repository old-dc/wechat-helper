"""导出某会话已入库的消息为 json / csv / md。"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime


def _rows(conn: sqlite3.Connection, chat: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT chat, content, sender, media_path, msg_type, time_label, ts, source FROM messages "
        "WHERE chat=? ORDER BY ts, id", (chat,)
    ).fetchall()


def export(conn: sqlite3.Connection, chat: str, fmt: str = "json") -> str:
    rows = _rows(conn, chat)
    if fmt == "json":
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["chat", "content", "sender", "media_path", "msg_type", "time_label", "ts", "source"]
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
        return buf.getvalue()
    if fmt == "md":
        lines = [f"# {chat}", ""]
        for r in rows:
            t = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M") if r["ts"] else (r["time_label"] or "-")
            who = f"{r['sender']}: " if r["sender"] else ""
            img = f" ![]({r['media_path']})" if r["media_path"] else ""
            lines.append(f"- **{t}** {who}{r['content']}{img}")
        return "\n".join(lines)
    raise ValueError(f"unknown format: {fmt}")
