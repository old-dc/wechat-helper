"""离线自检：数据层/查询/解析/发现，全部不碰微信。

运行：uv run python test/test_db.py
"""
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from wechat_helper import db as dbm
from wechat_helper import query as q
from wechat_helper import discover
from wechat_helper import config as cfgm
from wechat_helper import audit
from wechat_helper.wechat import (parse_time_label, parse_session_name, select_target,
                                  is_contact_header)


class _R:
    def __init__(self, name):
        self.name = name


@contextmanager
def tmpdb():
    with tempfile.TemporaryDirectory() as d:
        conn = dbm.open_db(Path(d) / "t.db")
        try:
            yield conn
        finally:
            conn.close()


def test_upsert_dedup():
    with tmpdb() as conn:
        rows = [
            {"chat": "群1", "content": "hello", "bucket": "10:00", "source": "scrape", "ts": 100},
            {"chat": "群1", "content": "hello", "bucket": "10:00", "source": "scrape", "ts": 100},  # dup
            {"chat": "群1", "content": "hello", "bucket": "10:01", "source": "scrape", "ts": 160},  # 不同桶
            {"chat": "群1", "content": "", "bucket": "x"},  # 空内容跳过
        ]
        assert dbm.upsert_messages(conn, rows) == 2
        assert dbm.upsert_messages(conn, [rows[0]]) == 0
        chats = q.list_chats(conn)
        assert len(chats) == 1 and chats[0]["name"] == "群1" and chats[0]["msg_count"] == 2


def test_cross_thread_write():
    # watch 回调在工作线程写库：check_same_thread=False 下跨线程写应成功
    import threading
    with tempfile.TemporaryDirectory() as d:
        conn = dbm.open_db(Path(d) / "t.db", check_same_thread=False)
        try:
            err = []
            def worker():
                try:
                    dbm.upsert_messages(conn, [{"chat": "测试", "content": "wx验证001",
                                                "bucket": "1", "source": "watch"}], emit_event=True)
                except Exception as e:
                    err.append(e)
            t = threading.Thread(target=worker); t.start(); t.join()
            assert not err, err
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        finally:
            conn.close()


def test_event_emission():
    with tmpdb() as conn:
        dbm.upsert_messages(conn, [{"chat": "g", "content": "a", "bucket": "1",
                                    "source": "watch"}], emit_event=True)
        ev = conn.execute("SELECT kind, chat FROM events").fetchall()
        assert len(ev) == 1 and ev[0]["kind"] == "new_message" and ev[0]["chat"] == "g"


def test_search_fts_and_like():
    with tmpdb() as conn:
        assert dbm.has_fts(conn)  # 本机 sqlite 有 FTS5
        dbm.upsert_messages(conn, [
            {"chat": "g1", "content": "今天下午要开会讨论项目", "bucket": "a", "ts": 10, "source": "scrape"},
            {"chat": "g2", "content": "订单已发货", "bucket": "b", "ts": 20, "source": "scrape"},
        ])
        # >=3 字走 FTS
        hits = q.search(conn, "开会讨论")
        assert len(hits) == 1 and hits[0]["chat"] == "g1", hits
        # <3 字走 LIKE
        hits2 = q.search(conn, "发货")
        assert len(hits2) == 1 and hits2[0]["chat"] == "g2", hits2
        # chat 限定
        assert q.search(conn, "项目", chat="g2") == []


def test_exclude_filter():
    with tmpdb() as conn:
        dbm.upsert_messages(conn, [
            {"chat": "私密群", "content": "秘密发货单", "bucket": "a", "ts": 10, "source": "scrape"},
            {"chat": "公开群", "content": "普通发货单", "bucket": "b", "ts": 20, "source": "scrape"},
        ])
        hits = q.search(conn, "发货", exclude=["私密群"])
        assert len(hits) == 1 and hits[0]["chat"] == "公开群"
        rec = q.recent(conn, exclude=["私密群"])
        assert all(r["chat"] != "私密群" for r in rec)
        clause, params = dbm.excluded_sql(["a", "b"], col="chat")
        assert clause == "chat NOT IN (?,?)" and params == ["a", "b"]
        assert dbm.excluded_sql([]) == ("", [])


def test_chat_meta_and_digest():
    with tmpdb() as conn:
        dbm.upsert_chat_meta(conn, "工作群", type="group", origin="session",
                             pinned=1, muted=0, unread=5, preview="张三: 在吗", last_time_label="10:00")
        dbm.upsert_chat_meta(conn, "闲聊群", type="group", origin="session", unread=0)
        d = q.digest(conn, unread_only=True)
        assert len(d) == 1 and d[0]["name"] == "工作群" and d[0]["unread"] == 5
        chats = q.list_chats(conn)
        assert any(c["name"] == "工作群" and c["pinned"] == 1 for c in chats)


def test_contact_header_filter():
    # 分组头应被过滤
    for h in ["新的朋友", "公众号、服务号18", "企业微信联系人2", "联系人347", "群聊", "标签"]:
        assert is_contact_header(h), h
    # 真实联系人名不被误伤
    for n in ["A李紫宇", "伴学-秦杏", "包文新", "AA果町鲜果企业合作可开票", "联系人老王"]:
        assert not is_contact_header(n), n


def test_select_private_targets():
    sessions = ["张三", "测试", "李四", "公众号", "张三", "王五"]  # 张三重复
    contacts = ["张三", "李四", "王五", "赵六"]  # 测试/公众号不是联系人
    out = discover.select_private_targets(sessions, contacts, exclude=["王五"])
    assert out == ["张三", "李四"]  # 去重、排除王五、保持会话顺序、剔除非联系人


def test_contacts_table():
    with tmpdb() as conn:
        dbm.upsert_contacts(conn, ["包文新", "蔡璐", "包文新"])  # 含重复
        assert q.contacts(conn) == ["包文新", "蔡璐"]  # 去重+排序
        dbm.upsert_contacts(conn, ["阿强"])  # 幂等追加
        assert "阿强" in q.contacts(conn)


def test_members():
    with tmpdb() as conn:
        dbm.replace_members(conn, "群A", ["张三", "李四", "王五"])
        assert q.members(conn, "群A") == ["张三", "李四", "王五"]  # 按 Unicode 码点排序
        dbm.replace_members(conn, "群A", ["赵六"])  # 整体替换
        assert q.members(conn, "群A") == ["赵六"]


def test_discover_guess_type():
    assert discover._guess_type({"preview": "张三: 你好"}) == "group"
    assert discover._guess_type({"preview": "你好呀"}) == "unknown"
    assert discover._guess_type({"preview": ""}) == "unknown"


def test_config_merge_privacy():
    cfg = cfgm.load()  # 当前目录可能有 config.json
    assert "db_path" in cfg and isinstance(cfgm.excluded(cfg), list)


def test_audit_log():
    with tempfile.TemporaryDirectory() as d:
        cfg = {"log_path": str(Path(d) / "audit.jsonl")}
        audit.log(cfg, "wechat", "scrape", target="张三", inserted=5)
        audit.log(cfg, "access", "search_messages", key_level=2, returned=3)
        audit.log(cfg, "wechat", "send", target="李四", ok=True)
        allrows = audit.read(cfg, limit=10)
        assert len(allrows) == 3 and allrows[0]["action"] == "send"  # 倒序，最新在前
        acc = audit.read(cfg, category="access")
        assert len(acc) == 1 and acc[0]["action"] == "search_messages" and acc[0]["key_level"] == 2
        wechat_only = audit.read(cfg, category="wechat")
        assert len(wechat_only) == 2
        assert audit.read({"log_path": str(Path(d) / "nope.jsonl")}) == []  # 文件不存在


def test_access_levels():
    cfg = {"privacy": {"access": {
        "levels": {"1": "k1", "2": "k2", "3": "k3"},
        "chats": {"张三": 3, "李四": 2, "测试群": 1},
        "default_level": 1,
    }}}
    # 高等级密钥看得到低等级会话（clearance >= 会话等级）
    assert cfgm.can_access("张三", "k3", cfg) is True    # 3>=3
    assert cfgm.can_access("李四", "k3", cfg) is True    # 3>=2
    assert cfgm.can_access("张三", "k2", cfg) is False   # 2<3
    assert cfgm.can_access("李四", "k2", cfg) is True    # 2>=2
    assert cfgm.can_access("测试群", "k1", cfg) is True  # 1>=1
    assert cfgm.can_access("李四", "k1", cfg) is False   # 1<2
    # 无密钥/错密钥 = 等级0，看不到任何分级会话
    assert cfgm.can_access("测试群", None, cfg) is False
    assert cfgm.can_access("测试群", "乱填", cfg) is False
    # 未分级会话走 default_level=1
    assert cfgm.can_access("路人甲", "k1", cfg) is True
    assert cfgm.can_access("路人甲", None, cfg) is False
    assert cfgm.clearance_of("k3", cfg) == 3 and cfgm.clearance_of("乱", cfg) == 0
    # 未配置 levels = 全开放
    assert cfgm.can_access("谁", None, {"privacy": {"access": {"levels": {}}}}) is True


def test_whitelist_gate():
    # 白名单空 -> 谁都不放行
    cfg0 = {"privacy": {"allow_chats": [], "exclude_chats": []}}
    assert cfgm.gate(["张三", "李四"], cfg0) == []
    assert cfgm.is_allowed("张三", cfg0) is False
    # 白名单生效 + 黑名单优先剔除 + 去重保序
    cfg = {"privacy": {"allow_chats": ["张三", "李四", "王五"], "exclude_chats": ["王五"]}}
    assert cfgm.gate(["李四", "张三", "张三", "王五", "赵六"], cfg) == ["李四", "张三"]
    assert cfgm.is_allowed("张三", cfg) is True
    assert cfgm.is_allowed("王五", cfg) is False   # 在黑名单
    assert cfgm.is_allowed("赵六", cfg) is False   # 不在白名单


def test_parse_session_name():
    a = parse_session_name("最尖端的AI研究组\n已置顶\n[动画表情] \n09/30\n消息免打扰\n")
    assert a["name"] == "最尖端的AI研究组" and a["pinned"] and a["muted"]
    assert a["time_label"] == "09/30" and a["unread"] == 0 and a["preview"] == "[动画表情]"
    b = parse_session_name('批改群\n[3条] "巩捷甫" 撤回了一条消息\n13:43\n消息免打扰\n')
    assert b["unread"] == 3 and not b["pinned"] and b["muted"] and b["preview"] == '"巩捷甫" 撤回了一条消息'
    c = parse_session_name("T30群\n[16条] 千千: 早上好\n在线答疑\n08:00\n消息免打扰\n")
    assert c["unread"] == 16 and c["time_label"] == "08:00" and "在线答疑" in c["preview"]
    assert parse_session_name("") is None


def test_select_target_exact_first():
    G = ("最常使用", "群聊", "联系人", "功能")
    # '测试' 不能被 '效果测试' 抢走：精确匹配优先，即使子串群排在前/在最常使用里
    results = {"最常使用": [_R("效果测试")], "群聊": [_R("ai效果测试讨论"), _R("测试")]}
    assert select_target(results, "测试", "group", G).name == "测试"
    # 没有精确匹配时回退子串（取优先级最高的第一个）
    results2 = {"群聊": [_R("效果测试讨论"), _R("测试专用")]}
    assert select_target(results2, "测试", "group", G).name == "效果测试讨论"
    # 完全没有 -> None
    assert select_target({"群聊": [_R("别的群")]}, "测试", "group", G) is None


def test_select_target_strict_no_fuzzy():
    # 发送用：strict=True 时无精确匹配必须返回 None（绝不回退子串，防发错人）
    G = ("最常使用", "群聊", "联系人", "功能")
    results = {"群聊": [_R("效果测试讨论"), _R("测试专用")]}  # 无精确 "测试"
    assert select_target(results, "测试", "group", G, strict=True) is None
    # 有精确匹配时仍返回精确项
    results2 = {"群聊": [_R("效果测试"), _R("测试")]}
    assert select_target(results2, "测试", "group", G, strict=True).name == "测试"


def test_parse_time_label():
    now = datetime(2026, 10, 6, 15, 0)  # 周二
    assert parse_time_label("今天 10:23", now) == int(datetime(2026, 10, 6, 10, 23).timestamp())
    assert parse_time_label("昨天 9:05", now) == int(datetime(2026, 10, 5, 9, 5).timestamp())
    assert parse_time_label("3月5日 08:00", now) == int(datetime(2026, 3, 5, 8, 0).timestamp())
    assert parse_time_label("2025年12月1日 23:59", now) == int(datetime(2025, 12, 1, 23, 59).timestamp())
    assert parse_time_label("星期一 10:00", now) == int(datetime(2026, 10, 5, 10, 0).timestamp())
    assert parse_time_label("星期二 10:00", now) == int(datetime(2026, 9, 29, 10, 0).timestamp())  # 今天周二：指上周二
    assert parse_time_label("10:23", now) == int(datetime(2026, 10, 6, 10, 23).timestamp())
    # 不带年份的日期跨年：落在今天之后的是去年
    jan = datetime(2027, 1, 5, 12, 0)
    assert parse_time_label("12月30日 08:00", jan) == int(datetime(2026, 12, 30, 8, 0).timestamp())
    assert parse_time_label("12/30 08:00", jan) == int(datetime(2026, 12, 30, 8, 0).timestamp())
    assert parse_time_label("1月3日 09:00", jan) == int(datetime(2027, 1, 3, 9, 0).timestamp())
    assert parse_time_label("2月29日 10:00", datetime(2029, 1, 5)) == int(datetime(2028, 2, 29, 10, 0).timestamp())
    assert parse_time_label("13/45", jan) is None
    assert parse_time_label("系统消息：撤回", now) is None
    assert parse_time_label("", now) is None


def test_send_in_background():
    # 后台发送的判定：只在焦点确实在空输入框、原文打对、回车清空时算发出；否则擦干净交给前台，绝不多发
    from wechat_helper import wechat as w

    class Box:  # 假输入框 + 投递效果：focus=焦点在输入框，enter_sends=回车即发送
        def __init__(self, value="", focus=True, enter_sends=True):
            self.Value, self.focus, self.enter_sends, self.sent = value, focus, enter_sends, []

        def GetValuePattern(self):
            return self

    def run(box, msg):
        def text(_h, t):
            if box.focus:
                box.Value += t

        def key(_h, vk, n=1):
            for _ in range(n if box.focus else 0):
                if vk == w.VK_BACK:
                    box.Value = box.Value[:-1]
                elif box.enter_sends:
                    box.sent.append(box.Value)
                    box.Value = ""
                else:
                    box.Value += "\n"

        saved = w._post_text, w._post_key, w._wait
        w._post_text, w._post_key, w._wait = text, key, lambda cond, timeout, step=0.1: cond()
        try:
            return w._send_in_background(0, box, msg)
        finally:
            w._post_text, w._post_key, w._wait = saved

    b = Box()
    assert run(b, "你好 hi") is True and b.sent == ["你好 hi"] and b.Value == ""
    b = Box(focus=False)  # 焦点不在输入框：探针没进去，什么都不打
    assert run(b, "你好") is None and b.sent == [] and b.Value == ""
    b = Box(value="本人草稿")  # 有没发的草稿：不碰
    assert run(b, "你好") is None and b.Value == "本人草稿" and b.sent == []
    b = Box()  # 多行投递不了
    assert run(b, "a\nb") is None and b.Value == "" and b.sent == []
    b = Box(enter_sends=False)  # Ctrl+Enter 发送模式：回车只换行，擦掉自己打的
    assert run(b, "你好") is None and b.Value == "" and b.sent == []


class _Ctl:  # 假 UIA 控件
    def __init__(self, cls="", name="", ctype="ListItemControl", rect=(0, 0, 0, 0), kids=()):
        self.ClassName, self.Name, self.ControlTypeName, self.kids = cls, name, ctype, list(kids)
        self.BoundingRectangle = _Rect(*rect)

    def GetChildren(self):
        return self.kids


class _Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


def test_split_sender():
    from wechat_helper.wechat import split_sender
    names = sorted(["M 23", "M", "张三"], key=len, reverse=True)
    assert split_sender("M 23 在吗", names) == ("M 23", "在吗")  # 名字带空格：按名单取最长前缀
    assert split_sender("张三 a b", names) == ("张三", "a b")
    assert split_sender("王五 [强]") == ("王五", "[强]")  # 名单外：按第一个空格
    assert split_sender("没有空格") == (None, "没有空格")


def test_read_chat_items_multiselect():
    from wechat_helper.wechat import read_chat_items
    ml = _Ctl(kids=[
        _Ctl("mmui::ChatItemView", "15:52"),  # 时间标签不带发送者
        _Ctl("mmui::ChatTextItemView", "张三-zs🚁 今天天气不错", "CheckBoxControl"),
        _Ctl("mmui::ChatBubbleReferItemView", "李四 动画表情 [囧]", "CheckBoxControl"),
        _Ctl("mmui::ChatTextItemView", "平时没有前缀"),  # 不在多选
    ])
    assert read_chat_items(ml) == [
        ("time", "15:52", None), ("text", "今天天气不错", "张三-zs🚁"),
        ("media", "[动画表情 [囧]]", "李四"), ("text", "平时没有前缀", None)]


def test_stitch_and_labels_with_sender():
    from wechat_helper.wechat import stitch_batch, assign_labels
    seq = [("time", "10:00", None), ("text", "a", "张三"), ("media", "[动画表情 [想]]", "李四")]
    batch = [("time", "09:00", None), ("text", "x", "王五"), ("time", "10:00", None),
             ("text", "a", "张三"), ("media", "[动画表情]", "李四")]  # 表情名懒加载：只比类型名 + 发送者
    seq = stitch_batch(seq, batch)
    assert len(seq) == 5
    assert stitch_batch([("text", "a", "张三")], [("text", "a", "李四")]) == [("text", "a", "李四"), ("text", "a", "张三")]
    out = assign_labels(seq, lambda label: "in_range", include_head=False)
    assert [(o["content"], o["sender"], o["time"]) for o in out] == [
        ("x", "王五", "09:00"), ("a", "张三", "10:00"), ("[动画表情]", "李四", "10:00")]


def test_bubble_points():
    # 列表 0..400；行 100..200：左侧头像 10..50（行上部），气泡 60..180（行中下部）；背景 250
    from wechat_helper.wechat import bubble_points
    row = _Ctl("mmui::ChatTextItemView", rect=(0, 100, 400, 200))
    time_row = _Ctl("mmui::ChatItemView", rect=(0, 200, 400, 230))
    ml = _Ctl(rect=(0, 0, 400, 300), kids=[row, time_row])

    def px(x, y):
        if 10 <= x < 50 and 105 <= y < 145:
            return 30   # 头像
        if 60 <= x < 180 and 130 <= y < 190:
            return 238  # 气泡
        return 250
    (item, rect, (x, y)), = list(bubble_points(px, ml))
    assert item is row and rect == (0, 100, 400, 200) and 60 <= x < 180 and 130 <= y < 190
    assert list(bubble_points(lambda x, y: 250, ml)) == []  # 找不到气泡不乱点


def test_sender_column():
    import sqlite3
    with tempfile.TemporaryDirectory() as d:  # 旧库（没有 sender 列）打开时补列
        old = sqlite3.connect(Path(d) / "old.db")
        old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, chat TEXT NOT NULL, content TEXT NOT NULL, "
                    "msg_type TEXT, time_label TEXT, ts INTEGER, is_at_me INTEGER, source TEXT, "
                    "dedup_key TEXT UNIQUE, created_at INTEGER)")
        old.commit(); old.close()
        conn = dbm.open_db(Path(d) / "old.db")
        assert dbm.upsert_messages(conn, [{"chat": "g", "content": "hi", "sender": "张三", "bucket": "1",
                                           "source": "watch"}]) == 1
        conn.close()
    with tmpdb() as conn:
        row = {"chat": "g", "content": "hi", "bucket": "1", "source": "scrape", "ts": 100}
        assert dbm.upsert_messages(conn, [row]) == 1
        assert dbm.upsert_messages(conn, [{**row, "sender": "张三"}]) == 0  # 重复：不算新增，但补上发送者
        assert dbm.upsert_messages(conn, [{**row, "sender": "李四"}]) == 0  # 已有发送者不覆盖
        assert q.recent(conn)[0]["sender"] == "张三"
        # 另一来源在时间窗内已有（dup_window 跳过）：也补发送者
        dbm.upsert_messages(conn, [{"chat": "g", "content": "yo", "bucket": "s", "source": "send", "ts": 200}])
        assert dbm.upsert_messages(conn, [{"chat": "g", "content": "yo", "sender": "我", "bucket": "2",
                                           "source": "scrape", "ts": 200, "dup_window": (150, 250)}]) == 0
        assert q.search(conn, "yo")[0]["sender"] == "我"
        mid = conn.execute("SELECT id FROM messages WHERE content='hi'").fetchone()[0]
        dbm.fill_sender(conn, mid, "王五")  # 已有不覆盖
        assert q.recent(conn, chat="g")[-1]["sender"] == "张三"


def test_media_box():
    # 行 100..300：头像 10..50（行上部）、群昵称 70..120 × 105..117、图片 70..230 × 125..290，图片中间有整行白（255，背景 250）
    from wechat_helper.wechat import media_box
    lr, r = _Rect(0, 0, 400, 400), _Rect(0, 100, 400, 300)

    def px(x, y):
        if 10 <= x < 50 and 105 <= y < 145:
            return 30
        if 70 <= x < 120 and 105 <= y < 117:
            return 150
        if 70 <= x < 230 and 125 <= y < 290:
            return 255 if 180 <= y < 200 else 120
        return 250
    assert media_box(px, lr, r) == (70, 125, 230, 290)
    assert media_box(lambda x, y: 250, lr, r) is None


def test_to_png():
    import struct
    import zlib
    from wechat_helper.wechat import to_png
    w, l, t = 4, 100, 200  # 4×3 的 BGRX 截图，取中间 2×2
    bits = bytes(b for y in range(3) for x in range(w) for b in (x, y, 7, 0))
    png = to_png((bits, l, t, w), (l + 1, t + 1, l + 3, t + 3))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (2, 2)
    idat = png.index(b"IDAT")
    raw = zlib.decompress(png[idat + 4:idat + 4 + struct.unpack(">I", png[idat - 4:idat])[0]])
    assert raw == b"\x00" + bytes([7, 1, 1, 7, 1, 2]) + b"\x00" + bytes([7, 2, 1, 7, 2, 2])  # RGB = (7, y, x)


def test_save_media():
    from wechat_helper.watch import save_media
    with tempfile.TemporaryDirectory() as d:
        a = {"chat": "g", "content": "[图片]", "bucket": "100", "image": b"png-a"}
        b = {"chat": "g", "content": "[图片]", "bucket": "100", "image": b"png-b"}
        save_media(a, d); save_media(b, d)
        assert "image" not in a and Path(a["media_path"]).read_bytes() == b"png-a"
        assert a["bucket"] != b["bucket"]  # 同一分钟两张不同图片不并成一条
        c = {"chat": "g", "content": "x", "bucket": "1"}
        save_media(c, d)
        assert "media_path" not in c and c["bucket"] == "1"
        with tmpdb() as conn:
            assert dbm.upsert_messages(conn, [a, b]) == 2
            assert dbm.upsert_messages(conn, [{**a, "media_path": None}]) == 0  # 再来一次不带截图：不覆盖
            assert {r["media_path"] for r in q.recent(conn)} == {a["media_path"], b["media_path"]}


def test_pick_main_window():
    # 标题带"微信"的独立聊天窗口（群"测试微信"）叠在主窗口上面：仍要认标题正好是"微信"的
    from wechat_helper.wechat import pick_main_window
    Q = "Qt51514QWindowIcon"
    assert pick_main_window([(1, "测试微信", Q), (2, "微信", Q)]) == 2
    assert pick_main_window([(1, "测试微信", Q)]) is None  # 没有 → 交给 wx4py 原逻辑
    assert pick_main_window([(3, "微信", "Chrome_WidgetWin_1")]) is None


def test_sender_names():
    with tmpdb() as conn:
        dbm.replace_members(conn, "群A", ["M 23", "张三"])
        dbm.upsert_contacts(conn, ["李四", "张三"])
        names = q.sender_names(conn, "群A")
        assert set(names) == {"群A", "M 23", "张三", "李四"} and len(names[0]) >= len(names[-1])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n全部通过（{len(fns)} 项）")
