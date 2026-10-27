"""监听层：常驻监听群和私聊新消息，写 messages + events。

发送者：监听让独立窗口保持多选状态，消息带发送者显示名（UIA 平时不给）。本助手 wx send / approve
发出的消息在发送时以 source=send 入库，监听看到它回流时认领跳过（顺便补上发送者）；在手机等别处发的照常入库。
图片：截下窗口里显示的样子存成 PNG（media_dir，按内容命名，同图只存一份），路径记在 messages.media_path。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from . import db as dbm
from . import discover
from . import query
from . import wechat

ECHO_WINDOW = 300  # 秒：发出后这么久内看到同内容消息，算自己发的回流


def save_media(row: dict, media_dir) -> None:
    """row 里的截图（image，PNG 字节）存进 media_dir，路径记到 media_path。按内容命名：同一张图只存一份。
    去重桶带上图的指纹：同一分钟里连发的两张不同图片（正文都是"[图片]"）不会被并成一条。"""
    image = row.pop("image", None)
    if not image:
        return
    digest = hashlib.sha1(image).hexdigest()[:16]
    path = Path(media_dir).resolve() / f"{digest}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
    row["media_path"] = str(path)
    row["bucket"] = f"{row.get('bucket', '')}:{digest[:8]}"


def run_watch(conn: sqlite3.Connection, wx, groups: list[str], *,
              contacts: list[str] = (),
              on_new: Optional[Callable[[dict], None]] = None,
              discover_first: bool = True, media_dir="data/media", **options):
    """阻塞式监听。discover_first=True 时先刷一次会话列表落库。Ctrl-C 退出。

    conn 需以 check_same_thread=False 打开：wx4py 在工作线程里回调 handle。
    写库用锁串行化，避免多线程并发写同一连接。
    """
    if discover_first:
        try:
            discover.discover_sessions(conn, wx, scroll=True)
        except Exception:
            pass  # 发现失败不影响监听主链

    counter = {"n": 0}
    lock = threading.Lock()
    claimed: set[int] = set()  # 已认领的发送记录：每条只抵一次回流，对方紧接着发同样的话照收

    def handle(row: dict):
        save_media(row, media_dir)
        with lock:
            echo = dbm.find_echo(conn, row["chat"], row["content"], row["ts"] - ECHO_WINDOW, claimed)
            if echo:  # 自己发的消息回流：发送时已入库（source=send），不再记、不发事件
                claimed.add(echo)
                dbm.fill_sender(conn, echo, row.get("sender"))
                return
            n = dbm.upsert_messages(conn, [row], emit_event=True)
        if n:
            counter["n"] += n
            if on_new:
                on_new(row)

    names = {c: query.sender_names(conn, c) for c in [*groups, *contacts]}
    try:
        wechat.watch_chats(wx, groups, handle, contacts=contacts, block=True, names=names, **options)
    finally:
        dbm.record_sync_run(conn, "watch", ",".join([*groups, *contacts]), counter["n"], counter["n"])
