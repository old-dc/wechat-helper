"""命令行入口。读当前目录 config.json。微信相关命令需微信已登录并前台。

命令分类：
- 要连微信（写库）：scrape / scrape-all / sessions / members / contacts --pull / watch
- 发送（连微信、对外）：send / approve
- 只碰库（不碰微信）：chats / search / recent / digest / export / drafts / reject / logs
- 服务：mcp
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from . import audit
from . import config as cfgm
from . import db as dbm
from . import discover
from . import export as exporter
from . import query as q
from . import watch as watcher


def _fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _who(sender) -> str:
    return f"{sender}: " if sender else ""


def _img(path) -> str:
    return f"  ({path})" if path else ""


# ---------------- 连微信（写库） ----------------

def _send(cfg, target: str, message: str, chat_type: str, **audit_extra) -> bool:
    """真正发送（白名单 + 精确匹配），记台账与审计。调用方负责预览/确认。"""
    from . import wechat
    conn = dbm.open_db(cfg["db_path"])
    wx = wechat.connect(activate=False)
    try:
        ok = wechat.send_message(wx, target, message, target_type=chat_type)
    finally:
        wx.disconnect()
    if ok:  # 记下自己发的：带方向信息；监听据此认出回流，抓取也不再重复记
        now = int(time.time())
        dbm.upsert_messages(conn, [{"chat": target, "chat_type": chat_type, "content": message,
                                    "msg_type": "text", "ts": now, "source": "send",
                                    "bucket": f"send:{now}"}])  # 独立去重空间：对方同一分钟回同样的话不被吞
    dbm.record_sync_run(conn, "send", target, 1, 1 if ok else 0, f"type={chat_type}")
    audit.log(cfg, "wechat", "send", target=target, chat_type=chat_type, message=message, ok=ok,
              **audit_extra)
    print(f"{'已发送' if ok else '发送失败（精确匹配未命中或发送异常）'} → [{chat_type}] {target}")
    return ok


def cmd_send(args, cfg):
    target = args.target
    message = " ".join(args.message).strip()
    if not message:
        print("消息为空", file=sys.stderr)
        return 2
    if not cfgm.is_allowed(target, cfg):
        print(f"'{target}' 不在白名单 privacy.allow_chats，拒绝发送。先把它加进 config.json。", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"[DRY-RUN] 将发送到 [{args.type}] {target}：\n  {message}\n\n确认无误后加 --yes 真正发送。")
        return 0
    return 0 if _send(cfg, target, message, args.type) else 1


def _print_draft(d) -> None:
    print(f"#{d['id']} [{d['status']}] → {d['chat']}  （起草方等级 {d['key_level']}，{_fmt_ts(d['created_at'])}）")
    if d["reply_to_content"]:
        print(f"  回复：{d['reply_to_content']}")
    print(f"  草稿：{d['content']}")
    if d["final_content"] and d["final_content"] != d["content"]:
        print(f"  实发：{d['final_content']}")


def cmd_drafts(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    rows = dbm.list_drafts(conn, None if args.status == "all" else args.status, args.limit)
    for d in rows:
        _print_draft(d)
    print(f"\n共 {len(rows)} 条")


def cmd_approve(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    d = dbm.get_draft(conn, args.id)
    if not d or d["status"] != "pending":
        print(f"草稿 #{args.id} 不存在或不是待审批状态", file=sys.stderr)
        return 2
    message = (args.edit or d["content"]).strip()
    target = d["chat"]
    if not cfgm.is_allowed(target, cfg):
        print(f"'{target}' 不在白名单 privacy.allow_chats，拒绝发送。", file=sys.stderr)
        return 2
    chat_type = args.type or (conn.execute("SELECT type FROM chats WHERE name = ?", (target,)).fetchone() or [None])[0]
    if chat_type not in ("contact", "group"):
        print(f"不知道 '{target}' 是私聊还是群，请加 --type contact|group", file=sys.stderr)
        return 2
    if not args.yes:
        _print_draft(d)
        print(f"\n[DRY-RUN] 将发送到 [{chat_type}] {target}：\n  {message}\n\n确认无误后加 --yes 真正发送。")
        return 0
    if not dbm.decide_draft(conn, args.id, "sending", message):  # 先占位，防重复发送
        print(f"草稿 #{args.id} 已被处理", file=sys.stderr)
        return 2
    ok = _send(cfg, target, message, chat_type, draft_id=args.id)
    dbm.decide_draft(conn, args.id, "sent" if ok else "failed", from_status="sending")
    return 0 if ok else 1


def cmd_reject(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    if not dbm.decide_draft(conn, args.id, "rejected"):
        print(f"草稿 #{args.id} 不存在或不是待审批状态", file=sys.stderr)
        return 2
    audit.log(cfg, "wechat", "draft_rejected", draft_id=args.id)
    print(f"已拒绝草稿 #{args.id}")
    return 0


def cmd_scrape(args, cfg):
    from . import wechat
    if not cfgm.is_allowed(args.chat, cfg):
        print(f"'{args.chat}' 不在白名单 privacy.allow_chats，不拉取。先把它加进 config.json。", file=sys.stderr)
        return 2
    conn = dbm.open_db(cfg["db_path"])
    wx = wechat.connect(activate=False)
    try:
        rows = wechat.scrape_history(wx, args.chat, target_type=args.type, since=args.since,
                                     max_count=args.max, names=q.sender_names(conn, args.chat))
    except RuntimeError as e:  # 重试后仍打不开 / 滚不到最新：明确失败，不入库残缺结果
        audit.log(cfg, "wechat", "scrape", target=args.chat, since=args.since, ok=False, error=str(e))
        print(f"抓取失败: {e}", file=sys.stderr)
        return 1
    finally:
        wx.disconnect()
    n = dbm.upsert_messages(conn, rows)
    dbm.upsert_chat_meta(conn, args.chat, type=args.type, origin="scrape")
    dbm.record_sync_run(conn, "scrape", args.chat, len(rows), n, f"since={args.since}")
    audit.log(cfg, "wechat", "scrape", target=args.chat, chat_type=args.type,
              since=args.since, fetched=len(rows), inserted=n)
    print(f"抓取 {len(rows)} 条，新增 {n} 条 → {cfg['db_path']}")


def cmd_scrape_all(args, cfg):
    from . import wechat
    conn = dbm.open_db(cfg["db_path"])
    exclude = cfgm.excluded(cfg)
    contacts = q.contacts(conn)
    if not contacts:
        print("通讯录为空：先跑 `wx contacts --pull`", file=sys.stderr)
        return 2
    if not cfgm.allowed(cfg):
        print("白名单 privacy.allow_chats 为空：默认不拉取任何人。先把要拉的人加进 config.json。", file=sys.stderr)
        return 2
    wx = wechat.connect(activate=False)
    try:
        sessions = discover.discover_sessions(conn, wx, scroll=not args.no_scroll)
        private = discover.select_private_targets([s["name"] for s in sessions], contacts, exclude)
        targets = cfgm.gate(private, cfg)  # 白名单门禁
        print(f"私聊目标 {len(targets)}/{len(private)} 个（会话∩通讯录∩白名单），since={args.since}，开始抓取")
        done = total_new = 0
        for i, name in enumerate(targets, 1):
            try:
                rows = wechat.scrape_history(wx, name, target_type="contact", since=args.since,
                                             max_count=args.max, names=q.sender_names(conn, name))
                n = dbm.upsert_messages(conn, rows)
                dbm.upsert_chat_meta(conn, name, type="contact", origin="scrape")
                dbm.record_sync_run(conn, "scrape", name, len(rows), n, f"since={args.since}")
                done += 1
                total_new += n
                audit.log(cfg, "wechat", "scrape", target=name, chat_type="contact",
                          since=args.since, fetched=len(rows), inserted=n, batch="scrape-all")
                print(f"[{i}/{len(targets)}] {name}: 抓{len(rows)} 新增{n}", flush=True)
            except Exception as e:
                audit.log(cfg, "wechat", "scrape", target=name, batch="scrape-all", ok=False, error=str(e))
                print(f"[{i}/{len(targets)}] {name}: 失败 {e}", flush=True)
    finally:
        wx.disconnect()
    audit.log(cfg, "wechat", "scrape_all_done", targets=len(targets), succeeded=done, inserted=total_new)
    print(f"完成：成功 {done}/{len(targets)} 个私聊，新增 {total_new} 条")


def cmd_sessions(args, cfg):
    from . import wechat
    conn = dbm.open_db(cfg["db_path"])
    wx = wechat.connect(activate=False)
    try:
        sessions = discover.discover_sessions(conn, wx, scroll=not args.no_scroll)
    finally:
        wx.disconnect()
    audit.log(cfg, "wechat", "sessions", count=len(sessions))
    for s in sessions:
        flags = ("📌" if s["pinned"] else "") + ("🔇" if s["muted"] else "")
        unread = f"[{s['unread']}]" if s["unread"] else ""
        print(f"{s['name'][:22]:<22} {s.get('time_label') or '-':<7} {unread:<5}{flags:<4} {s['preview'][:36]}")
    print(f"\n共 {len(sessions)} 个会话，已落库 → {cfg['db_path']}")


def cmd_contacts(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    if args.pull:
        from . import wechat
        wx = wechat.connect()
        try:
            names = discover.discover_contacts(conn, wx, scroll=not args.no_scroll)
        finally:
            wx.disconnect()
        audit.log(cfg, "wechat", "contacts", count=len(names))
        print(f"通讯录拉取 {len(names)} 名联系人，已落库")
    else:
        names = q.contacts(conn, limit=args.limit)
        for n in names:
            print(n)
        print(f"\n共 {len(names)} 名联系人（读库；加 --pull 从微信重新拉取）")


def cmd_members(args, cfg):
    from . import wechat
    conn = dbm.open_db(cfg["db_path"])
    wx = wechat.connect()
    try:
        members = discover.discover_members(conn, wx, args.group)
    finally:
        wx.disconnect()
    audit.log(cfg, "wechat", "members", group=args.group, count=len(members))
    for m in members:
        print(m)
    print(f"\n共 {len(members)} 名成员，已落库")


def cmd_watch(args, cfg):
    from . import wechat
    if args.groups or args.contact:  # 命令行给了就只用命令行，否则用 config
        req_groups, req_contacts = args.groups, args.contact
    else:
        req_groups, req_contacts = cfg.get("watch_groups") or [], cfg.get("watch_contacts") or []
    if not req_groups and not req_contacts:
        print("没有要监听的会话：用 `wx watch 群名... --contact 联系人` 或在 config.json 配 "
              "watch_groups / watch_contacts", file=sys.stderr)
        return 2
    groups, contacts = cfgm.gate(req_groups, cfg), cfgm.gate(req_contacts, cfg)  # 白名单门禁
    if not groups and not contacts:
        print(f"请求的会话都不在白名单 privacy.allow_chats，不监听：{', '.join(req_groups + req_contacts)}",
              file=sys.stderr)
        return 2
    conn = dbm.open_db(cfg["db_path"], check_same_thread=False)  # 回调在工作线程
    wx = wechat.connect(activate=False)

    captured = {"n": 0}

    def on_new(row):
        captured["n"] += 1
        audit.log(cfg, "wechat", "watch_message", chat=row["chat"],
                  is_at_me=bool(row.get("is_at_me")), content=row["content"])
        flag = "@我 " if row.get("is_at_me") else ""
        print(f"[{datetime.now():%H:%M:%S}] {flag}{row['chat']}: {_who(row.get('sender'))}{row['content']}"
              f"{_img(row.get('media_path'))}")

    print(f"开始监听 {len(groups)} 个群、{len(contacts)} 个私聊：{', '.join(groups + contacts)}"
          "（Ctrl-C 停止；独立窗口别最小化，可放在其它窗口后面）")
    audit.log(cfg, "wechat", "watch_start", groups=groups, contacts=contacts)
    try:
        watcher.run_watch(conn, wx, groups, contacts=contacts, on_new=on_new,
                          discover_first=not args.no_discover, media_dir=cfg["media_dir"],
                          **cfg.get("watch_options", {}))
    except KeyboardInterrupt:
        print("\n停止监听")
    finally:
        wx.disconnect()
        audit.log(cfg, "wechat", "watch_stop", groups=groups, contacts=contacts, captured=captured["n"])
    return 0


# ---------------- 只读库 ----------------

def cmd_chats(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    for r in q.list_chats(conn, limit=args.limit, exclude=cfgm.excluded(cfg)):
        flags = ("📌" if r["pinned"] else "") + ("🔇" if r["muted"] else "")
        unread = f"[{r['unread']}]" if r["unread"] else ""
        print(f"{r['name'][:22]:<22} {r['type'] or '-':<8} {unread:<5}{flags:<4} {r['msg_count']:>5}条  {r['last_time_label'] or _fmt_ts(r['last_seen'])}")


def cmd_search(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    for r in q.search(conn, args.keyword, chat=args.chat, limit=args.limit, exclude=cfgm.excluded(cfg)):
        print(f"[{_fmt_ts(r['ts'])}] {r['chat']}: {_who(r['sender'])}{r['content']}{_img(r['media_path'])}")


def cmd_recent(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    for r in q.recent(conn, chat=args.chat, limit=args.limit, exclude=cfgm.excluded(cfg)):
        print(f"[{_fmt_ts(r['ts'])}] {r['chat']}: {_who(r['sender'])}{r['content']}{_img(r['media_path'])}")


def cmd_digest(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    for r in q.digest(conn, limit=args.limit, unread_only=args.unread, exclude=cfgm.excluded(cfg)):
        unread = f"[{r['unread']}]" if r["unread"] else ""
        print(f"{r['name'][:22]:<22} {unread:<5} {r['preview'][:40] if r['preview'] else ''}")


def cmd_export(args, cfg):
    conn = dbm.open_db(cfg["db_path"])
    out = exporter.export(conn, args.chat, fmt=args.format)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"已导出 → {args.out}")
    else:
        print(out)


def cmd_logs(args, cfg):
    rows = audit.read(cfg, category=args.category, limit=args.limit)
    for r in reversed(rows):  # 正序打印（旧→新）
        extra = {k: v for k, v in r.items() if k not in ("time", "ts", "category", "action")}
        detail = " ".join(f"{k}={v}" for k, v in extra.items())
        print(f"[{r.get('time')}] {r.get('category')}/{r.get('action')}  {detail[:100]}")
    print(f"\n共 {len(rows)} 条（{cfg.get('log_path')}）")


def cmd_mcp(args, cfg):
    from . import mcp_server
    mcp_server.main()


# ---------------- parser ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wx", description="微信助手：UI 自动化只读聊天消息")
    sub = p.add_subparsers(dest="cmd", required=True)

    sd = sub.add_parser("send", help="发送消息（仅白名单会话、精确匹配、默认dry-run需--yes）")
    sd.add_argument("target")
    sd.add_argument("message", nargs="+")
    sd.add_argument("--type", choices=["contact", "group"], default="contact")
    sd.add_argument("--yes", action="store_true", help="真正发送（否则只预览）")
    sd.set_defaults(func=cmd_send)

    dr = sub.add_parser("drafts", help="列出回复草稿（MCP draft_reply 起草，读库）")
    dr.add_argument("--status", choices=["pending", "sending", "sent", "failed", "rejected", "all"],
                    default="pending")
    dr.add_argument("--limit", type=int, default=50)
    dr.set_defaults(func=cmd_drafts)

    ap = sub.add_parser("approve", help="审批并发送草稿（默认dry-run需--yes）")
    ap.add_argument("id", type=int)
    ap.add_argument("--edit", help="改写后再发")
    ap.add_argument("--type", choices=["contact", "group"], help="会话类型（库里未知时必填）")
    ap.add_argument("--yes", action="store_true", help="真正发送（否则只预览）")
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="拒绝草稿")
    rj.add_argument("id", type=int)
    rj.set_defaults(func=cmd_reject)

    s = sub.add_parser("scrape", help="滚动抓取单个会话历史")
    s.add_argument("chat")
    s.add_argument("--type", choices=["contact", "group"], default="group")
    s.add_argument("--since", choices=["today", "yesterday", "week", "month", "all"], default="today")
    s.add_argument("--max", type=int, default=500)
    s.set_defaults(func=cmd_scrape)

    sa = sub.add_parser("scrape-all", help="批量抓私聊历史（会话∩通讯录）")
    sa.add_argument("--since", choices=["today", "yesterday", "week", "all"], default="all")
    sa.add_argument("--max", type=int, default=100000, help="单会话消息硬上限")
    sa.add_argument("--no-scroll", action="store_true", help="会话列表只读可见项")
    sa.set_defaults(func=cmd_scrape_all)

    ss = sub.add_parser("sessions", help="拉取会话列表并落库")
    ss.add_argument("--no-scroll", action="store_true", help="只读可见项不滚动")
    ss.set_defaults(func=cmd_sessions)

    m = sub.add_parser("members", help="拉取群成员并落库")
    m.add_argument("group")
    m.set_defaults(func=cmd_members)

    ct = sub.add_parser("contacts", help="通讯录联系人（--pull 从微信拉取，否则读库）")
    ct.add_argument("--pull", action="store_true", help="从微信通讯录重新拉取并落库")
    ct.add_argument("--no-scroll", action="store_true", help="拉取时只读可见项")
    ct.add_argument("--limit", type=int, default=2000)
    ct.set_defaults(func=cmd_contacts)

    w = sub.add_parser("watch", help="常驻监听群/私聊新消息")
    w.add_argument("groups", nargs="*")
    w.add_argument("--contact", action="append", default=[], help="监听的私聊联系人，可重复")
    w.add_argument("--no-discover", action="store_true", help="启动时不刷会话列表，更快进入监听")
    w.set_defaults(func=cmd_watch)

    c = sub.add_parser("chats", help="列出已记录会话（读库）")
    c.add_argument("--limit", type=int, default=200)
    c.set_defaults(func=cmd_chats)

    se = sub.add_parser("search", help="搜索消息（读库）")
    se.add_argument("keyword")
    se.add_argument("--chat")
    se.add_argument("--limit", type=int, default=50)
    se.set_defaults(func=cmd_search)

    r = sub.add_parser("recent", help="最近消息（读库）")
    r.add_argument("--chat")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(func=cmd_recent)

    d = sub.add_parser("digest", help="会话概览（读库，面向 agent）")
    d.add_argument("--unread", action="store_true", help="只看有未读的")
    d.add_argument("--limit", type=int, default=50)
    d.set_defaults(func=cmd_digest)

    e = sub.add_parser("export", help="导出某会话消息")
    e.add_argument("chat")
    e.add_argument("--format", choices=["json", "csv", "md"], default="json")
    e.add_argument("--out")
    e.set_defaults(func=cmd_export)

    lg = sub.add_parser("logs", help="查看审计日志（本助手拉取/发送 + 外部 MCP 调用）")
    lg.add_argument("--category", choices=["wechat", "access"], help="只看某一类")
    lg.add_argument("--limit", type=int, default=50)
    lg.set_defaults(func=cmd_logs)

    mc = sub.add_parser("mcp", help="启动 MCP 服务（stdio，不连微信；读库 + 起草回复）；消费方用 WECHAT_HELPER_KEY 环境变量表明身份")
    mc.set_defaults(func=cmd_mcp)
    return p


def main(argv=None) -> int:
    # 重定向到文件/管道时：会话名常带 emoji，GBK 编不出来；且默认块缓冲，跟踪日志看不到实时消息
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    args = build_parser().parse_args(argv)
    cfg = cfgm.load()
    return args.func(args, cfg) or 0


if __name__ == "__main__":
    raise SystemExit(main())
