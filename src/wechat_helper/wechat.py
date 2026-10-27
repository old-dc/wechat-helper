"""wx4py 封装：连接微信、历史抓取、新消息监听。

只做读取。wx4py 走 Windows UIAutomation（需开启系统屏幕阅读器标志），
不解密任何数据库、不修改微信客户端。微信需登录、窗口不能最小化（控件树会空）。
滚动、打字、回车尽量直接投递给窗口（不动真鼠标键盘、不抢前台），见"后台操作"。
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

# 时间标签解析：把 "今天 10:23" / "昨天 9:5" / "星期三 10:23" /
# "3月5日 10:23" / "2026年3月5日 10:23" / "3/5 10:23" / "10:23" 尽力转 epoch。
_HM = re.compile(r"(\d{1,2}):(\d{2})")
_MD_CN = re.compile(r"(\d{1,2})月(\d{1,2})日")
_YMD_CN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_MD_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _no_year_date(month: int, day: int, today: date) -> date:
    """不带年份的日期（"12月30日" / "12/30"）：取今年；落在今天之后（或今年没有这天，如 2 月 29 日）
    就是去年的——聊天记录不会来自未来。跨年后抓到上一年年底的消息靠这条兜住。"""
    try:
        d = date(today.year, month, day)
        if d <= today:
            return d
    except ValueError:
        pass
    return date(today.year - 1, month, day)


def parse_time_label(label: str, now: Optional[datetime] = None) -> Optional[int]:
    """把微信的时间标签尽力解析成 epoch 秒；失败返回 None。"""
    if not label:
        return None
    now = now or datetime.now()
    hm = _HM.search(label)
    hour, minute = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)

    day: Optional[date] = None
    try:
        if m := _YMD_CN.search(label):
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m := _MD_CN.search(label) or _MD_SLASH.search(label):
            day = _no_year_date(int(m.group(1)), int(m.group(2)), now.date())
        elif "今天" in label:
            day = now.date()
        elif "昨天" in label:
            day = now.date() - timedelta(days=1)
        elif "星期" in label or "周" in label:
            for ch, idx in _WEEKDAY.items():
                if f"星期{ch}" in label or f"周{ch}" in label:
                    delta = (now.weekday() - idx) % 7 or 7  # 今天的消息只显示时分，同名星期是上周
                    day = now.date() - timedelta(days=delta)
                    break
        elif hm:
            day = now.date()  # 只有 HH:MM 默认今天
        if day is None:
            return None
        return int(datetime(day.year, day.month, day.day, hour, minute).timestamp())
    except ValueError:  # 非法日期/时间（如 13/45、25:99）
        return None


# 发送时置 True：精确匹配不命中就返回 None（不回退子串），防止发错人。
_strict_exact = False


def select_target(results, target, target_type, groups, strict: bool = False):
    """exact-first 选择搜索结果：先按会话名精确匹配，再回退子串。

    results: {分组名: [有 .name 的结果对象]}；groups=(FREQUENT, CHATS, CONTACTS, FUNCTIONS)。
    修掉 wx4py 原逻辑 'target in item.name' 取第一个的坑——'测试' 会命中 '效果测试'。
    strict=True：无精确匹配直接返回 None，不做子串回退（发送用）。
    """
    GF, GC, GK, GFN = groups
    order = [GF, GC if target_type == "group" else GK]
    if target_type == "contact":
        order.append(GFN)
    tgt = (target or "").strip()
    for grp in order:  # 第一轮：精确名匹配
        for item in results.get(grp, []):
            if (getattr(item, "name", "") or "").strip() == tgt:
                return item
    if strict:
        return None
    for grp in order:  # 回退：子串（保持原行为兜底）
        for item in results.get(grp, []):
            if tgt in (getattr(item, "name", "") or ""):
                return item
    return None


_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")


def search_keyword(name: str) -> str:
    """搜索框关键词：SendKeys 打不出 emoji，取第一个 emoji 之前的部分；emoji 开头则原样。"""
    head = _EMOJI.split(name, 1)[0].strip()
    return head or name


def _install_exact_match_patch():
    """把 wx4py 的会话匹配换成精确优先。幂等。strict 由模块标志 _strict_exact 控制。
    搜索只打 emoji 前的文字，结果仍按带 emoji 的全名精确匹配。"""
    from wx4py.features import chat as _chat

    if getattr(_chat.ChatWindow, "_exact_patched", False):
        return
    groups = (_chat.GROUP_FREQUENT, _chat.GROUP_CHATS, _chat.GROUP_CONTACTS, _chat.GROUP_FUNCTIONS)
    _chat.ChatWindow._find_target_result = (
        lambda self, results, target, target_type:
            select_target(results, target, target_type, groups, strict=_strict_exact))
    _search = _chat.ChatWindow.search
    _chat.ChatWindow.search = lambda self, keyword: _search(self, search_keyword(keyword))
    _chat.ChatWindow._exact_patched = True


def since_bounds(since: str, now: Optional[datetime] = None):
    """since → (起, 止) epoch，止为 None 表示到现在；'all' → None（不限）。
    today=今天；yesterday=昨天；week=最近 7 天（含今天）；month=最近 30 天。"""
    now = now or datetime.now()
    midnight = datetime(now.year, now.month, now.day).timestamp()
    return {
        "today": (midnight, None),
        "yesterday": (midnight - 86400, midnight),
        "week": (midnight - 6 * 86400, None),
        "month": (now.timestamp() - 30 * 86400, None),
    }.get(since)


def _install_range_patch():
    """时间范围判定改用本项目的 parse_time_label，幂等。wx4py 原逻辑把"9月18日"这类日期一律换算成
    "星期X"，于是 since=week 永远在范围内、一直翻到聊天最顶上；它也没有 month。
    标签解析不了按范围内处理（继续翻）。"""
    from wx4py.features import chat as _chat

    cw = _chat.ChatWindow
    if getattr(cw, "_range_patched", False):
        return
    _range = cw._get_chat_history_range

    def get_range(self, since):
        self._since_bounds = since_bounds(since)
        return _range(self, "all")

    def get_state(self, ts, history_range, today, yesterday):
        bounds = getattr(self, "_since_bounds", None)
        t = parse_time_label(ts)
        if bounds is None or t is None:
            return "in_range"
        lo, hi = bounds
        if t < lo:
            return "too_old"
        return "too_new" if hi is not None and t >= hi else "in_range"

    cw._get_chat_history_range, cw._get_history_timestamp_state = get_range, get_state
    cw._range_patched = True


# 表情包/图片等媒体消息：UIA 只给类型名（"动画表情" / "图片"），没有内容。wx4py 原逻辑直接跳过，
# 这里存成占位 "[动画表情]" / "[图片]"，保住对话上下文（谁在哪回了个表情）。
MEDIA_CLASS = "mmui::ChatBubbleReferItemView"
_TIME_CLASS = "mmui::ChatItemView"
_TEXT_CLASSES = {"mmui::ChatTextItemView": "text", "mmui::ChatBubbleItemView": "link"}
_TS_LABEL = re.compile(r"^(今天|昨天|星期[一二三四五六日]|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}|\d{4}年|\d{1,2}:\d{2})")


def media_label(name: str) -> str:
    return f"[{name}]"


def classify_chat_item(cls: str, name: str):
    """消息列表子项 → (kind, 文本)；不认识的类返回 None。历史抓取用，与 wx4py 原逻辑一致外加媒体。"""
    if cls == _TIME_CLASS:
        return ("time" if _TS_LABEL.match(name) else "system"), name
    if cls in _TEXT_CLASSES:
        return _TEXT_CLASSES[cls], name
    if cls == MEDIA_CLASS and name:
        return "media", media_label(name)
    return None


# 发送者：实测（2026-10-07，微信 4.1.15）消息项平时 Name 只有正文；聊天窗口进入"多选"后消息项变成 CheckBox，
# Name 变成"发送者 正文"（自己发的是自己的群昵称），时间标签、系统提示不带。窗口不在前台也一直保持多选。
def split_sender(name: str, names=()) -> tuple[Optional[str], str]:
    """多选状态下的消息项 Name → (发送者, 正文)。names：已知名字（群成员/通讯录/会话名），按长度降序；
    先按它取最长前缀（名字里带空格也切得对），没对上就按第一个空格切。"""
    for n in names:
        if name.startswith(n + " "):
            return n, name[len(n) + 1:]
    # ponytail: 名单外、名字里带空格的人会切错（发送者只剩第一段、其余进正文），先 `wx members` 拉群成员
    head, sep, rest = name.partition(" ")
    return (head, rest) if sep and head else (None, name)


def _control_type(ctrl) -> str:
    try:
        return ctrl.ControlTypeName or ""
    except Exception:
        return ""


def read_chat_items(msg_list, names=()) -> list[tuple]:
    """历史抓取读可见项 → [(kind, 文本, 发送者)]，从旧到新；发送者只有多选状态下才有，否则 None。"""
    out = []
    try:
        for c in msg_list.GetChildren():
            name, sender = c.Name or "", None
            if _control_type(c) == "CheckBoxControl":
                sender, name = split_sender(name, names)
            if got := classify_chat_item(c.ClassName or "", name):
                out.append((*got, sender))
    except Exception:
        return []
    return out


def read_live_items(msg_list, names=()):
    """监听读可见项 → (项列表, 各项发送者)。多选状态下拆出发送者，项的 name 只留正文——锚点、去重都按正文，
    进出多选不影响；媒体存占位。"""
    import dataclasses

    from wx4py.features.messaging import listener as _lst

    items, senders = [], []
    for it in _lst._raw_read_visible_items(msg_list):
        sender, name = None, it.name
        if it.kind == "message" and _control_type(it.control) == "CheckBoxControl":
            sender, name = split_sender(name, names)
        if it.class_name == MEDIA_CLASS:
            name = media_label(name)
        items.append(dataclasses.replace(it, name=name) if name != it.name else it)
        senders.append(sender)
    return items, senders


def _install_media_patch():
    """历史抓取与实时监听都收媒体消息（多选状态下先拆掉发送者前缀）。幂等。"""
    from wx4py.features import chat as _chat
    from wx4py.features.messaging import listener as _lst

    cw = _chat.ChatWindow
    if getattr(cw, "_media_patched", False):
        return
    _lst._raw_read_visible_items = _lst._read_visible_items
    _lst.MESSAGE_CLASSES.add(MEDIA_CLASS)
    _lst._read_visible_items = lambda msg_list: read_live_items(msg_list)[0]
    cw._read_visible_chat_items = lambda self, msg_list: [it[:2] for it in read_chat_items(msg_list)]
    cw._media_patched = True


# ---------------- 后台操作：直接投递窗口消息 ----------------
# 实测（2026-10-07，微信 4.1.15 / Qt 5.15）：UIA 的写值、调用、设焦点、弹窗都会被微信拉到前台；
# 直接投递给窗口的滚轮、字符、按键、关闭不会，也不动真鼠标键盘和剪贴板。窗口被别的程序挡住也照样生效。
VK_BACK, VK_RETURN = 0x08, 0x0D


def _wait(cond, timeout: float, step: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while not cond():
        if time.time() > deadline:
            return False
        time.sleep(step)
    return True


def _lparam(x: int, y: int) -> int:
    """坐标打包成消息参数；负坐标（主屏左边/上边的副屏）按 16 位截断。"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def post_wheel(hwnd: int, ctrl, delta: int, steps: int = 1, step_delay: float = 0.05) -> None:
    """往控件中心投递滚轮（delta>0 往上翻）。
    Qt 按落点找接收窗口：落点上最上层是微信的另一个窗口（独立聊天窗口常叠在同一位置）就会滚到它那里，
    所以先把盖在落点上的微信窗口压到目标窗口下面（不激活，不动别的程序的窗口）。"""
    import ctypes
    import win32con
    import win32gui
    import win32process

    ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))  # 物理像素，与 UIA/Qt 坐标一致
    r = ctrl.BoundingRectangle
    x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    for _ in range(5):
        top = win32gui.ChildWindowFromPointEx(win32gui.GetDesktopWindow(), (x, y), 1)  # CWP_SKIPINVISIBLE，同 Qt
        if not top or top == hwnd or win32process.GetWindowThreadProcessId(top)[1] != pid:
            break
        win32gui.SetWindowPos(top, hwnd, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
    for _ in range(steps):
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, _lparam(x, y))
        time.sleep(step_delay)


def _post_text(hwnd: int, text: str) -> None:
    """逐个 UTF-16 码元投递 WM_CHAR（emoji 是代理对，Qt 自己拼回去）。进的是该窗口里当前持有焦点的控件。"""
    import win32con
    import win32gui

    for unit in memoryview(text.encode("utf-16-le")).cast("H"):
        win32gui.PostMessage(hwnd, win32con.WM_CHAR, unit, 0)


def _post_key(hwnd: int, vk: int, n: int = 1) -> None:
    import win32api
    import win32con
    import win32gui

    lp = 1 | win32api.MapVirtualKey(vk, 0) << 16
    for _ in range(n):
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, lp)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, lp | 0xC0000000)


def _post_click(hwnd: int, x: int, y: int, right: bool = False) -> None:
    """往窗口投递一次鼠标点击（屏幕坐标）。不动真鼠标、窗口被挡住也点得到；但微信收到点击会把主窗口拉到前台。"""
    import win32con
    import win32gui

    lp = _lparam(*win32gui.ScreenToClient(hwnd, (x, y)))
    down, up, mk = ((win32con.WM_RBUTTONDOWN, win32con.WM_RBUTTONUP, win32con.MK_RBUTTON) if right
                    else (win32con.WM_LBUTTONDOWN, win32con.WM_LBUTTONUP, win32con.MK_LBUTTON))
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
    time.sleep(0.1)
    win32gui.PostMessage(hwnd, down, mk, lp)
    time.sleep(0.05)
    win32gui.PostMessage(hwnd, up, 0, lp)


def _grab(hwnd: int):
    """后台截窗口画面（被挡住也行）→ (BGRX 像素, 窗口左, 窗口上, 宽)，自上而下。"""
    import ctypes
    import win32gui
    import win32ui

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    try:
        ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        return bmp.GetBitmapBits(True), l, t, w
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)


def _gray(shot):
    """截图 → 取灰度的函数 px(屏幕x, 屏幕y)。"""
    bits, l, t, w = shot

    def px(x: int, y: int) -> int:
        i = ((y - t) * w + (x - l)) * 4
        return (bits[i] + bits[i + 1] + bits[i + 2]) // 3
    return px


def _snapshot(hwnd: int):
    return _gray(_grab(hwnd))


def to_png(shot, box) -> bytes:
    """截图里的一块 (左, 上, 右, 下)（屏幕坐标）→ PNG 字节。标准库编码，不加依赖。"""
    import struct
    import zlib

    bits, l, t, w = shot
    x0, y0, x1, y1 = box
    raw = bytearray()
    for y in range(y0, y1):
        i = ((y - t) * w + (x0 - l)) * 4
        row = bits[i:i + (x1 - x0) * 4]
        rgb = bytearray(len(row) // 4 * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]  # BGRX → RGB
        raw += b"\x00" + rgb

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", x1 - x0, y1 - y0, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def _runs(points: list, gap: int) -> list:
    """升序坐标 → 连续段 [(起, 止)]（止不含），相隔不超过 gap 的并成一段。"""
    out = []
    for p in points:
        if out and p - out[-1][1] <= gap:
            out[-1][1] = p + 1
        else:
            out.append([p, p + 1])
    return out


def media_box(px, lr, r):
    """消息行 r 里图片/表情显示的位置 (左, 上, 右, 下)；找不到 → None。lr：消息列表的位置。
    按列找非背景色：最宽的一段列是图片（头像、多选的勾选框窄）；再在这段列里按行取最高的一段（去掉上方的群昵称）。
    背景色是纯色，阈值压得很低，截图类白底图片的白也算图（与背景差几级灰度）。"""
    bg = px((lr.left + lr.right) // 2, r.top + 1)  # 行顶留白是背景
    ink = lambda x, y: abs(px(x, y) - bg) > 2
    ys = range(r.top + 2, r.bottom - 1, 2)
    xr = _runs([x for x in range(lr.left + 2, lr.right - 2) if any(ink(x, y) for y in ys)], 8)
    if not xr:
        return None
    x0, x1 = max(xr, key=lambda s: s[1] - s[0])
    yr = _runs([y for y in range(r.top + 2, r.bottom - 1) if any(ink(x, y) for x in range(x0, x1, 2))], 4)
    y0, y1 = max(yr, key=lambda s: s[1] - s[0])
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    return x0, y0, x1, y1


MEDIA_SETTLE = 1.0  # 秒：图片、表情刚出现时可能还在加载，等一下再截


def capture_media(hwnd: int, msg_list, ctrl) -> Optional[bytes]:
    """把一条图片/表情消息在窗口里显示的样子截下来 → PNG 字节（显示尺寸；动图是截到的那一帧）。
    没完整露出（被新消息顶出可视区）、找不到图 → None。"""
    import ctypes

    time.sleep(MEDIA_SETTLE)  # ponytail: 定长等待，网慢时可能截到加载中的灰块；要更稳就等到两次截图一致
    ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))
    try:
        lr, r = msg_list.BoundingRectangle, ctrl.BoundingRectangle
        if r.top < lr.top or r.bottom > lr.bottom:
            return None
        shot = _grab(hwnd)
        box = media_box(_gray(shot), lr, r)
        return to_png(shot, box) if box else None
    except Exception:
        return None


# ---------------- 多选状态（读发送者） ----------------
# 进出多选都靠投递点击：右键一条文字消息的气泡 → 菜单里点"多选"；点"取消多选"退出。会抢一下前台（见 _post_click）。
# 不能投递 Esc 退出：Esc 会直接关掉独立聊天窗口（实测）。
MENU_TRIES = 3  # 右键几条消息找"多选"（右在引用块等处弹出的菜单可能没有）


def in_multiselect(msg_list) -> bool:
    try:
        return any(_control_type(c) == "CheckBoxControl" for c in msg_list.GetChildren())
    except Exception:
        return False


def bubble_points(px, msg_list):
    """可见的完整文字消息（从新到旧）里各取气泡上的一点 → (项, 项的位置, (x, y))。气泡与背景颜色不同：
    在消息行偏下处横扫一行，最长的一段非背景色就是气泡（头像窄、在行的上部）。"""
    lr = msg_list.BoundingRectangle
    for item in reversed(msg_list.GetChildren()):
        r = item.BoundingRectangle
        if (item.ClassName or "") != "mmui::ChatTextItemView" or r.top < lr.top or r.bottom > lr.bottom:
            continue
        y = r.top + (r.bottom - r.top) * 3 // 5
        bg = px((lr.left + lr.right) // 2, r.top + 1)  # 行顶留白是背景
        best, start = (0, 0), None
        for x in range(lr.left + 2, lr.right - 1):
            if abs(px(x, y) - bg) > 6:
                start = x if start is None else start
            elif start is not None:
                best, start = max(best, (x - start, start)), None
        if start is not None:
            best = max(best, (lr.right - 1 - start, start))
        if best[0] >= 30:
            yield item, (r.left, r.top, r.right, r.bottom), (best[1] + best[0] // 2, y)


def _process_windows(pid: int) -> set:
    import win32gui
    import win32process

    out = set()

    def add(h, _):
        if win32gui.IsWindowVisible(h) and win32process.GetWindowThreadProcessId(h)[1] == pid:
            out.add(h)
    win32gui.EnumWindows(add, None)
    return out


def enter_multiselect(hwnd: int, msg_list) -> bool:
    """让聊天窗口进入多选（消息项带上发送者）。只点名字是"多选"的菜单项（菜单里还有删除、撤回），
    菜单里没有就关掉菜单换一条消息。已在多选 → True；屏幕上没有能右键的文字消息、或都没弹出"多选" → False。"""
    import ctypes
    import win32con
    import win32gui
    import win32process
    from wx4py.core import uiautomation as uia

    if in_multiselect(msg_list):
        return True
    ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))  # 物理像素，与 UIA 坐标一致
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    for tries, (item, rect, (x, y)) in enumerate(bubble_points(_snapshot(hwnd), msg_list)):
        r = item.BoundingRectangle
        if tries >= MENU_TRIES or (r.left, r.top, r.right, r.bottom) != rect:
            break  # 列表动了（来了新消息），截图作废，下轮再试
        before = _process_windows(pid)
        _post_click(hwnd, x, y, right=True)
        found = []

        def menu_item():
            for h in _process_windows(pid) - before:
                c = uia.ControlFromHandle(h).MenuItemControl(Name="多选")
                if c.Exists(0, 0):
                    found.append((h, c))
                    return True
            return False
        if _wait(menu_item, 2):
            h, c = found[0]
            m = c.BoundingRectangle
            _post_click(h, (m.left + m.right) // 2, (m.top + m.bottom) // 2)
            return _wait(lambda: in_multiselect(msg_list), 3)
        for h in _process_windows(pid) - before:  # 弹出的菜单里没有"多选"：关掉
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.3)
    return False


def exit_multiselect(hwnd: int) -> bool:
    """点"取消多选"退出多选。不在多选 → True。"""
    import ctypes
    from wx4py.core import uiautomation as uia

    ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))
    btn = uia.ControlFromHandle(hwnd).ButtonControl(Name="取消多选")
    if not btn.Exists(0.5):
        return True
    r = btn.BoundingRectangle
    _post_click(hwnd, (r.left + r.right) // 2, (r.top + r.bottom) // 2)
    return _wait(lambda: not btn.Exists(0, 0), 3)


def _send_in_background(hwnd: int, edit, message: str) -> Optional[bool]:
    """在独立聊天窗口里后台发送：投递字符 + 回车，回车后输入框清空 = 已发出 → True。
    先打一个探针字母确认焦点在输入框（焦点在按钮上时，空格/回车会按下它；字母按钮不理）。
    做不了 → 擦掉自己打的字、返回 None，交给前台发送：输入框里有本人没发的草稿、多行（Shift+Enter 投递不了）、
    焦点不在输入框、打进去的字对不上（如 emoji 显示成别的）、回车没发出（设成了 Ctrl+Enter 发送）。"""
    vp = edit.GetValuePattern()
    value = lambda: vp.Value or ""
    if "\n" in message or "\r" in message or value():
        return None
    _post_text(hwnd, "x")
    if not _wait(lambda: value() == "x", 1.5):
        return None  # 探针落到了别的控件
    _post_key(hwnd, VK_BACK)
    _post_text(hwnd, message)
    if _wait(lambda: value() == message, 3):
        _post_key(hwnd, VK_RETURN)
        if _wait(lambda: value() == "", 5):
            return True
    _post_key(hwnd, VK_BACK, len(message) + 2)  # 输入框原本是空的，里面全是自己打的
    return None


def _send_in_open_window(wx, target: str, message: str) -> Optional[bool]:
    """目标的独立聊天窗口（监听弹出的）开着就直接在里面发，不回主窗口搜索：先后台发，
    后台发不了再把窗口拉到前台、点输入框粘贴发送。窗口没开 / 拉不到前台 / 找不到输入框 → None，
    交给搜索发送；已试发 → 是否成功。"""
    import win32gui
    from wx4py.core import uiautomation as uia
    from wx4py.core.win32 import bring_window_to_front
    from wx4py.features.chat import ChatWindow
    from wx4py.features.messaging import listener as _lst

    hwnd = pick_window(_lst._find_wechat_windows(), target, wx.window.hwnd)
    if not hwnd:
        return None
    find_input = lambda: _lst.WeChatGroupListener._find_chat_input(uia.ControlFromHandle(hwnd))
    edit = find_input()
    if not edit and exit_multiselect(hwnd):  # 多选状态（监听为读发送者进的）没有输入框：先退出，监听过一阵再进
        edit = find_input()
    if not edit:
        return None
    if _send_in_background(hwnd, edit, message):
        _lst.logger.info(f"后台发送: {target}")
        return True
    if not bring_window_to_front(hwnd):
        return None
    deadline = time.time() + 1.5  # 激活是异步的，实测约 0.2s 才切到前台
    while win32gui.GetForegroundWindow() != hwnd:
        if time.time() > deadline:
            return None  # 不在前台就不点：输入框的点击会落到挡在上面的别的窗口
        time.sleep(0.1)
    _lst.logger.info(f"复用独立窗口发送: {target}")
    return ChatWindow.send_text_via_input(edit, message)


def _install_no_minimize_patch():
    """wx4py 发送成功后会把微信主窗口最小化（"保护隐私"）；主窗口一最小化控件树就空了，
    紧接着的抓取/会话读取静默读到 0 条。改为发完不动窗口。"""
    from wx4py.features.base import BasePage

    BasePage._minimize_window = lambda self: False


def send_message(wx, target: str, message: str, target_type: str = "contact") -> bool:
    """发送消息。精确匹配目标（无命中即失败，绝不发错人）。返回是否成功。"""
    global _strict_exact
    sent = _send_in_open_window(wx, target, message)
    if sent is not None:
        return sent
    _strict_exact = True
    try:
        wx.window.activate()  # 搜索发送要前台（后台连接时没激活）
        return bool(wx.chat_window.send_to(target, message, target_type=target_type))
    finally:
        _strict_exact = False


def pick_main_window(windows):
    """[(hwnd, 标题, 类名)] 里的微信主窗口 = 标题正好是"微信"的 Qt 窗口；没有 → None。"""
    return next((h for h, t, c in windows if (t or "").strip() == "微信" and (c or "").startswith("Qt")), None)


def _install_main_window_patch():
    """wx4py 给窗口打分找主窗口，标题含"微信"加分：标题带"微信"的独立聊天窗口（如群"测试微信"）与主窗口同分，
    叠在上面就被当成主窗口，之后的搜索按键会关掉它、再把主窗口收进托盘（2026-10-07 实测）。
    改为先找标题正好是"微信"的窗口（藏在托盘的也算，交给 wx4py 恢复），没有再走原逻辑。幂等。"""
    import os

    import win32gui
    import win32process
    from wx4py.core import win32 as _w32
    from wx4py.core import window as _win

    if getattr(_win, "_main_patched", False):
        return
    orig = _win.find_wechat_window

    def find():
        wins = []

        def add(h, _):
            try:
                if win32gui.GetWindowText(h) == "微信" and os.path.basename(_w32._get_process_image_name(
                        win32process.GetWindowThreadProcessId(h)[1])).lower() in ("weixin.exe", "wechat.exe"):
                    wins.append((h, "微信", win32gui.GetClassName(h)))
            except Exception:
                pass
        win32gui.EnumWindows(add, None)
        return pick_main_window(wins) or orig()

    _win.find_wechat_window = find
    _win._main_patched = True


def connect(activate: bool = True):
    """连接微信，返回 WeChatClient（已 connect）。wx4py 连接时会把主窗口拉到前台；
    activate=False 不拉：窗口可见就不动，最小化的还原但不激活，藏在托盘的提示本人点开并等着。"""
    from wx4py import WeChatClient  # 延迟导入：离线自检无需 wx4py
    from wx4py.core import window as _win
    from wx4py.core.window import WeChatWindow

    _install_main_window_patch()
    _install_exact_match_patch()
    _install_range_patch()
    _install_media_patch()
    _install_no_minimize_patch()
    if activate:
        return WeChatClient(auto_connect=True)

    import win32con
    import win32gui

    orig = WeChatWindow._activate_hwnd

    def quiet(self, hwnd):
        if not win32gui.IsWindowVisible(hwnd):  # 藏在托盘：wx4py 的托盘恢复在 Win11 上常失败、还会弄乱前台，改为等本人点开
            _win.logger.warning("微信主窗口藏在托盘里，读不到。请点托盘图标打开（别最小化，可放在其它窗口后面），"
                                "打开后自动继续。")
            while not win32gui.IsWindowVisible(hwnd):
                time.sleep(2)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        return True

    WeChatWindow._activate_hwnd = quiet
    try:
        return WeChatClient(auto_connect=True)
    finally:
        WeChatWindow._activate_hwnd = orig


# ---------------- 会话列表（主窗口左侧那一列） ----------------
# 每个会话项的 Name 是一段多行串：
#   会话名 \n [已置顶 \n] [[N条] [发送者: ]预览…（可能多行） \n] 时间 \n [消息免打扰 \n]
_SESSION_TIME = re.compile(
    r"^(\d{1,2}:\d{2}|\d{1,2}/\d{1,2}|昨天|星期[一二三四五六日天]|"
    r"\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日)$"
)
_UNREAD = re.compile(r"^\[(\d+)条\]\s*")


def parse_session_name(raw: str) -> Optional[dict]:
    """把会话项的多行 Name 解析成结构化字段。"""
    lines = (raw or "").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    name = lines[0].strip()
    body = lines[1:]
    muted = bool(body) and body[-1].strip() == "消息免打扰"
    if muted:
        body = body[:-1]
    time_label = None
    if body and _SESSION_TIME.match(body[-1].strip()):
        time_label = body[-1].strip()
        body = body[:-1]
    pinned = any(l.strip() == "已置顶" for l in body)
    body = [l for l in body if l.strip() != "已置顶"]
    preview = "\n".join(body).strip()
    unread = 0
    if m := _UNREAD.match(preview):
        unread = int(m.group(1))
        preview = preview[m.end():].strip()
    return {"name": name, "pinned": pinned, "muted": muted,
            "unread": unread, "time_label": time_label, "preview": preview}


def _find_session_list(root, uia):
    try:
        c = root.ListControl(AutomationId="session_list")
        if c.Exists(maxSearchSeconds=1):
            return c
    except Exception:
        pass
    try:
        for ctrl, _ in uia.WalkControl(root, includeTop=True, maxDepth=6):
            if ctrl.ControlTypeName == "ListControl" and (
                (getattr(ctrl, "AutomationId", "") or "") == "session_list"
                or (ctrl.Name or "") == "会话"):
                return ctrl
    except Exception:
        return None
    return None


def _scroll_list(hwnd: int, control, steps: int = 3, delta: int = -360) -> None:
    post_wheel(hwnd, control, delta, steps)
    time.sleep(0.25)


def list_sessions(wx, *, scroll: bool = True, max_rounds: int = 60) -> list[dict]:
    """拉取左侧会话列表。scroll=True 时滚动收全（虚拟滚动，只读可见项需边滚边累积）。"""
    from wx4py.core import uiautomation as uia

    sl = _find_session_list(wx.window.uia.root, uia)
    if sl is None:
        return []
    out: dict[str, dict] = {}

    def harvest():
        for it in sl.GetChildren():
            if it.ControlTypeName != "ListItemControl":
                continue
            info = parse_session_name(it.Name or "")
            if info and info["name"] and info["name"] not in out:
                out[info["name"]] = info

    harvest()
    if scroll:
        stable = 0
        for _ in range(max_rounds):
            before = len(out)
            _scroll_list(wx.window.hwnd, sl)
            harvest()
            if len(out) == before:
                stable += 1
                if stable >= 3:  # ponytail: 连续 3 轮无新增视为到底
                    break
            else:
                stable = 0
    return list(out.values())


# ---------------- 通讯录（全部联系人） ----------------
# 分组头（非真实联系人）：新的朋友 / 群聊 / 标签 / 公众号、服务号N / 企业微信联系人N / 联系人N
_CONTACT_HEADER = re.compile(r"^(新的朋友|群聊|标签|公众号、服务号|企业微信联系人|联系人)\d*$")


def is_contact_header(name: str) -> bool:
    return bool(_CONTACT_HEADER.match((name or "").strip()))


def _find_contact_list(root, uia):
    try:
        c = root.ListControl(AutomationId="primary_table_.contact_list")
        if c.Exists(maxSearchSeconds=1):
            return c
    except Exception:
        pass
    for ctrl, _ in uia.WalkControl(root, includeTop=True, maxDepth=12):
        if ctrl.ControlTypeName == "ListControl" and (
                getattr(ctrl, "AutomationId", "") or "").endswith("contact_list"):
            return ctrl
    return None


def list_contacts(wx, *, scroll: bool = True, max_rounds: int = 200) -> list[str]:
    """切到通讯录、滚动收全个人联系人显示名（备注/昵称），过滤分组头。无 wxid。"""
    from wx4py.core import uiautomation as uia

    root = wx.window.uia.root
    for ctrl, _ in uia.WalkControl(root, includeTop=True, maxDepth=8):
        if ctrl.ControlTypeName == "ButtonControl" and (ctrl.Name or "") == "通讯录":
            try:
                ctrl.Click(simulateMove=False)
            except Exception:
                pass
            break
    time.sleep(1.0)
    cl = _find_contact_list(root, uia)
    if cl is None:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def harvest():
        for it in cl.GetChildren():
            if it.ControlTypeName != "ListItemControl":
                continue
            name = (it.Name or "").strip()
            if not name or is_contact_header(name) or name in seen:
                continue
            seen.add(name)
            out.append(name)

    harvest()
    if scroll:
        stable = 0
        for _ in range(max_rounds):
            before = len(out)
            _scroll_list(wx.window.hwnd, cl)
            harvest()
            if len(out) == before:
                stable += 1
                if stable >= 3:  # 连续 3 轮无新增视为到底
                    break
            else:
                stable = 0
    return out


def list_group_members(wx, group: str) -> list[str]:
    """拉取群成员显示名（群昵称/备注名）。UIA 只给显示名，无 wxid、无群主/管理员角色。"""
    return wx.group_manager.get_group_members(group)


def _same_item(a: tuple, b: tuple) -> bool:
    """两次读到的是不是同一项（项 = (kind, 文本, 发送者)）。表情包的名字是懒加载的：先读到"动画表情"，
    过一会变成"动画表情 [想]"，只比类型名，否则重叠对不上、整屏被当新内容 → 一段消息重复。"""
    if a[0] == b[0] == "media":
        return a[2:] == b[2:] and a[1].strip("[]").split(" ", 1)[0] == b[1].strip("[]").split(" ", 1)[0]
    return a == b


def find_overlap(seq: list, batch: list) -> int:
    """batch 尾与 seq 头的重叠长度（取最长）；0 = 没对上。≥4 项的重叠容许 1 项不同（读取时名字刚好在变）。"""
    for k in range(min(len(batch), len(seq)), 0, -1):
        miss = sum(1 for x, y in zip(batch[-k:], seq[:k]) if not _same_item(x, y))
        if miss <= (1 if k >= 4 else 0):
            return k
    return 0


def stitch_batch(seq: list, batch: list) -> list:
    """往上翻读到的一屏 batch 接到已收集序列 seq（从旧到新）前面。新一屏的下半截就是 seq 的开头：
    只前插重叠之前的新内容；重叠部分用这次读到的（名字加载得更全）。没对上（滚过头）就整屏前插。"""
    return batch + seq[find_overlap(seq, batch):]


def assign_labels(seq: list, state, include_head: bool) -> list[dict]:
    """按完整序列给每条消息配"它上方最近的时间标签"，并按 since 范围筛选。
    state(label) → 'in_range' | 'too_old' | 'too_new'。include_head：序列开头、第一个标签之前的消息要不要
    （翻到聊天最顶上时要，标签留空；条数封顶/翻到过早标签而停时不要——它们的标签没读到）。"""
    nxt, after = [], ""
    for kind, name, *_ in reversed(seq):  # 每项之后的第一个标签
        nxt.append(after)
        if kind == "time":
            after = name
    nxt.reverse()
    out, label, keep = [], "", include_head
    for (kind, name, *sender), next_label in zip(seq, nxt):
        if kind == "time":
            label, keep = name, state(name) == "in_range"
        elif keep:
            out.append({"type": kind, "content": name, "sender": sender[0] if sender else None,
                        "time": label, "next_time": next_label})
    return out


# 抓取在目标的独立聊天窗口里做，滚动全靠投递滚轮（后台）。加载慢、读取抖动仍会有，每步按结果确认、失败重试。
OPEN_TRIES = 3     # 弹出独立窗口
EDGE_TRIES = 3     # 滚到底 / 确认到顶：滚轮没生效就等一下再试
COLLECT_TRIES = 2  # 整次采集：前后两屏没接上就重抓一遍，取接缝最少的结果


def _find_session_item(wx, name: str):
    """在主窗口会话列表里按 AutomationId 精确找会话项 → (列表, 项)，找不到项为 None。
    列表是虚拟滚动、只有可见项在控件树里：先滚到顶，再一屏屏往下找。"""
    from wx4py.core import uiautomation as uia

    main = wx.window.hwnd
    sl = _find_session_list(wx.window.uia.root, uia)
    if sl is None:
        return None, None
    aid = "session_item_" + name
    post_wheel(main, sl, 360, 30, 0.02)
    time.sleep(0.5)
    last, stable = None, 0
    for _ in range(60):
        kids = sl.GetChildren()
        ids = [c.AutomationId or "" for c in kids]
        if aid in ids:
            return sl, kids[ids.index(aid)]
        stable = stable + 1 if ids == last else 0
        if stable >= 3:  # 连续 3 屏没变：到底了
            break
        last = ids
        _scroll_list(main, sl)
    return sl, None


def open_chat_window(wx, target: str, target_type: str) -> tuple[int, bool]:
    """目标的独立聊天窗口 → (句柄, 是否本次新开)。开着就复用（全程后台）；没开就在会话列表里找到它，
    用 UIA 默认动作弹出（等于双击，不动鼠标），不在列表里（或被折叠）先搜索打开（要前台）。
    新弹出的窗口会被微信拉到前台——这是抓取唯一抢前台的一下。"""
    from wx4py.features.messaging import listener as _lst

    main = wx.window.hwnd
    find = lambda: pick_window(_lst._find_wechat_windows(), target, main)
    if hwnd := find():
        return hwnd, False
    sl, item = _find_session_item(wx, target)
    if item is None:
        wx.window.activate()
        if not wx.chat_window.open_chat(target, target_type):
            raise RuntimeError(f"打不开聊天: {target}")
        sl, item = _find_session_item(wx, target)
    for _ in range(OPEN_TRIES):
        if item is None:
            break
        item.GetLegacyIAccessiblePattern().DoDefaultAction()
        if _wait(lambda: find() is not None, 5):
            break
        sl, item = _find_session_item(wx, target)
    if sl is not None:
        post_wheel(main, sl, 360, 30, 0.02)  # 会话列表滚回顶部
    if not (hwnd := find()):
        raise RuntimeError(f"弹不出独立聊天窗口: {target}（已重试 {OPEN_TRIES} 次）")
    return hwnd, True


def _at_edge(msg_list, edge: str) -> bool:
    """列表是否滚到了一端：那一端的项完整露出（与可视区边沿平齐）。滚在中间时两端的项都被截掉一截。"""
    try:
        r = msg_list.BoundingRectangle
        if edge == "bottom":
            return msg_list.GetLastChildControl().BoundingRectangle.bottom <= r.bottom + 2
        return msg_list.GetFirstChildControl().BoundingRectangle.top >= r.top - 2
    except Exception:
        return False


def _wheel(hwnd: int, msg_list, delta: int, steps: int, settle: float) -> None:
    post_wheel(hwnd, msg_list, delta, steps)
    time.sleep(settle)


def _goto_bottom(read, hwnd: int, msg_list, target: str) -> None:
    """滚到最新消息，并按位置确认真到底了。滚轮连续不生效（内容不动又没到底）就等一下重来。
    不点"跳转到最新消息"按钮：UIA 调用会被微信拉到前台。"""
    for _ in range(EDGE_TRIES):
        idle = 0
        for _ in range(500):
            if _at_edge(msg_list, "bottom"):
                return
            before = read()
            _wheel(hwnd, msg_list, -360, 10, 0.3)
            idle = idle + 1 if read() == before else 0
            if idle >= 3:
                break
        time.sleep(1)
    raise RuntimeError(f"滚不到最新消息: {target}（微信无响应？已重试 {EDGE_TRIES} 次）")


def _collect_once(read, hwnd: int, msg_list, state, max_count: int, target: str):
    """滚到底后一屏屏往上翻、拼接。read() 读可见项。返回 (序列, 停止原因, 接缝数)。"""
    _goto_bottom(read, hwnd, msg_list, target)
    seq, prev_top, stuck, stop, gaps, nudges = [], None, 0, "top", 0, 0
    while True:
        batch = read()
        top = batch[0] if batch else None
        stuck = stuck + 1 if top == prev_top else 0
        prev_top = top
        before = len(seq)
        if seq and batch and not find_overlap(seq, batch):
            gaps += 1  # 这一屏和上一屏没接上：滚过头会漏，读取抖动会重复
        seq = stitch_batch(seq, batch)
        if any(it[0] == "time" and state(it[1]) == "too_old" for it in seq[:len(seq) - before]):
            return seq, "too_old", gaps
        if sum(1 for it in seq if it[0] != "time") >= max_count:
            return seq, "max_count", gaps
        if stuck >= 5:  # 翻不动了：真到顶，还是滚轮没生效？按位置确认
            if _at_edge(msg_list, "top"):
                return seq, "top", gaps
            if nudges >= EDGE_TRIES:
                return seq, "stuck", gaps
            nudges, stuck = nudges + 1, 0
            time.sleep(1)
        _wheel(hwnd, msg_list, 360, 5, 0.8)


def _collect_history(wx, target: str, target_type: str, since: str, max_count: int,
                     names=()) -> list[dict]:
    """替代 wx4py get_chat_history 的采集循环。原逻辑的"当前时间标签"跨屏沿用：新翻出的一屏里、
    第一个标签之前的消息会被套上上一屏更新的标签，之后看到真标签再收一次 → 同一条存两份、一份时间错；
    结果顺序也是乱的。这里先把每屏按重叠拼成完整有序序列，最后统一配标签。
    在独立聊天窗口里抓，本次新开的抓完关掉。先进多选读发送者（进不去照抓，发送者留空），
    复用的窗口是本次进的多选就抓完退出。打不开会话 / 滚不到最新消息时重试后抛 RuntimeError，
    不静默返回空或错位的结果。"""
    import win32con
    import win32gui
    from wx4py.core import uiautomation as uia
    from wx4py.features.chat import logger
    from wx4py.features.messaging import listener as _lst

    cw = wx.chat_window
    rng = cw._get_chat_history_range(since)
    today = date.today()
    state = lambda label: cw._get_history_timestamp_state(label, rng, today, today - timedelta(days=1))
    hwnd, opened = open_chat_window(wx, target, target_type)
    msg_list, entered = None, False
    try:
        root = uia.ControlFromHandle(hwnd)
        for _ in range(OPEN_TRIES):
            if msg_list := _lst._find_message_list(root):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"找不到消息列表: {target}")
        if opened:  # 新窗口被拉到了前台、焦点在输入框：挪到消息列表，免得本人此刻打字打进输入框、一回车就发出去
            msg_list.SetFocus()
        if not in_multiselect(msg_list):
            entered = enter_multiselect(hwnd, msg_list)
            if not entered:
                logger.warning(f"collect_history: '{target}' 进不了多选，这次抓不到发送者")
        read = lambda: read_chat_items(msg_list, names)
        best = None
        for attempt in range(1, COLLECT_TRIES + 1):
            got = _collect_once(read, hwnd, msg_list, state, max_count, target)
            if best is None or got[2] < best[2]:
                best = got
            if not got[2]:
                break
            logger.warning(f"collect_history: '{target}' 有 {got[2]} 处前后两屏没对上（第 {attempt} 次），重抓")
    finally:
        if opened:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        elif msg_list:  # 复用的（多半是监听开的）滚回底部，监听才收得到新消息；本次进的多选退出，还原本人的窗口
            post_wheel(hwnd, msg_list, -360, 30, 0.02)
            if entered:
                exit_multiselect(hwnd)
    seq, stop, gaps = best
    out = assign_labels(seq, state, include_head=stop == "top")
    logger.info(f"collect_history: {len(out)} items from '{target}' (since='{since}', stop='{stop}')")
    if gaps:
        logger.warning(f"collect_history: '{target}' 仍有 {gaps} 处没对上，结果可能漏或重复")
    if stop == "stuck":
        logger.warning(f"collect_history: '{target}' 往上翻不动又没到顶（滚轮没生效？），只拿到部分历史")
    return out


def scrape_history(wx, chat: str, *, target_type: str = "group",
                   since: str = "today", max_count: int = 500, names=()) -> list[dict]:
    """抓取单个会话的历史，返回已归一化、可直接入库的 row 列表（从旧到新）。
    names：拆发送者用的已知名字（query.sender_names）。"""
    items = _collect_history(wx, chat, target_type, since, max_count, names)
    now = int(time.time())
    rows = []
    for it in items:
        label = it["time"]
        ts = parse_time_label(label)
        nxt = parse_time_label(it["next_time"]) or now
        rows.append({
            "chat": chat,
            "chat_type": target_type,
            "content": it["content"],
            "sender": it["sender"],
            "msg_type": it["type"],
            "time_label": label,
            "ts": ts,  # 无标签（聊天最顶上）留空，不冒充抓取时刻
            "source": "scrape",
            # 去重按标签解析出的分钟：同一条消息隔天再抓，标签从"昨天"变"星期二"也对得上
            "bucket": str(ts // 60) if ts else label,
            # 跨来源去重窗口：这条消息发于本标签与下一个标签之间，监听/发送已记过就不重复入库
            "dup_window": (ts - 60, nxt + 60) if ts else None,
        })
    return rows


def pick_window(windows, title: str, exclude_hwnd=None):
    """按标题精确找独立聊天窗口。windows: [(hwnd, title, class)]。
    修掉 wx4py 原逻辑 'title in 窗口标题' 的坑——私聊 '李明' 会命中群窗口 '李明、张三'。"""
    for hwnd, t, _cls in windows:
        if hwnd != exclude_hwnd and (t or "").strip() == title.strip():
            return hwnd
    return None


def _install_window_match_patch():
    from wx4py.features.messaging import listener as _lst

    _lst._find_window_by_title = (
        lambda title, exclude_hwnd=None: pick_window(_lst._find_wechat_windows(), title, exclude_hwnd))


ANCHOR_LEN = 4  # 锚点：上次停在底部时的最后几项（含时间标签），够长才不会和更早的同文消息撞上


def find_after_anchor(anchor: list, sig: list):
    """在 sig 里找锚点（最靠后的位置），返回其后第一项的下标；找不到 → None。
    新消息会把锚点开头几项顶出屏幕，所以从整段锚点试到只剩最后 2 项。"""
    for k in range(len(anchor), min(2, len(anchor)) - 1, -1):
        tail = anchor[-k:]
        for j in range(len(sig) - k, -1, -1):
            if sig[j:j + k] == tail:
                return j + k
    return None


def new_message_slice(anchor, sig: list, observed: set):
    """判这一轮哪些可见项是新的。返回 (新项下标列表, 是否更新锚点)。只在列表停在底部时调用。
    - 首次（无锚点）：当前可见作基线，不算新。
    - 锚点可见：其后的项是新的（重复文字也算，按位置判）。
    - 锚点不可见且最后一项没见过：新消息多到把锚点挤出屏幕，收本会话从没出现过的项。
    - 锚点不可见但最后一项见过：多半是往上翻停在了恰好对齐底边的位置，不算新。"""
    if anchor is None:
        return [], True
    start = find_after_anchor(anchor, sig)
    if start is not None:
        return list(range(start, len(sig))), True
    if sig and sig[-1] not in observed:
        return [i for i, s in enumerate(sig) if s not in observed], True
    return [], False


LIVE_GAP = 10  # 秒：上次停在底部距今不超过这么久 = 实时抓到


def estimate_ts(sig: list, i: int, now: float, bottom_at) -> float:
    """新消息 sig[i] 的发送时间。实时抓到就用当前时间；离开底部一阵后才抓到的（往上翻期间来的），
    取它上方最近的时间标签，但不早于离开底部的时刻（消息一定是那之后发的）。"""
    if bottom_at is None or now - bottom_at <= LIVE_GAP:
        return now
    label = next((t for kind, name in reversed(sig[:i])
                  if kind == "time/system" and (t := parse_time_label(name)) is not None), None)
    return min(now, max(bottom_at, label or 0))


REBIND_EVERY = 5   # 秒：独立窗口没了，隔这么久看一次本人有没有重新打开
STRAY_LIMIT = 60   # 秒：窗口不在底部超过这么久就告警，并尝试滚回底部
MULTISELECT_GRACE = 60  # 秒：窗口退出了多选（本人要打字、发送时退的），等这么久再进；进不去也隔这么久再试
_sender_names: dict[str, list[str]] = {}  # 监听的会话 → 拆发送者用的已知名字（watch_chats 设置）


def _install_scroll_safe_poll():
    """替换 wx4py 监听的轮询（幂等）：
    - wx4py 把"没见过的可见项"都当新消息，列表虚拟化、一往上翻旧消息就重复入库。改为列表停在底部才判新，
      新消息按锚点位置判；往上翻期间来的按时间标签估发送时间。
    - wx4py 读不到消息只记 debug，窗口被关/最小化/停在旧位置都静默漏消息。改为状态变化时打 WARNING，
      窗口关了提示本人重新打开、开了自动接上（不替本人重开：要搜索、双击，会抢前台），
      窗口不在底部（新开的窗口常停在上次读到的位置）就尝试滚回底部。
    - 窗口保持在多选状态，消息带发送者（进多选会抢一下前台）。"""
    import win32gui
    from wx4py.features.messaging import listener as _lst

    cls = _lst.WeChatGroupListener
    if getattr(cls, "_scroll_safe", False):
        return

    def at_bottom(msg_list) -> bool:
        # 停在底部时最后一项下沿与可视区下沿平齐；往上翻时最后一项被截在可视区下方
        try:
            return msg_list.GetLastChildControl().BoundingRectangle.bottom <= msg_list.BoundingRectangle.bottom + 2
        except Exception:
            return True

    def health(session, state, msg):
        # 状态变化才打一条：出问题 WARNING，恢复 INFO，避免每轮刷屏
        if getattr(session, "health", "ok") != state:
            session.health = state
            (_lst.logger.info if state == "ok" else _lst.logger.warning)(f"[监听] {session.group}: {msg}")

    def scroll_to_bottom(session) -> bool:
        # 投递滚轮：不动鼠标、不抢前台，窗口被挡住也能滚（UIA 点"跳转到最新消息"按钮会被微信拉到前台，不用）
        try:
            post_wheel(session.hwnd, session.msg_list, -360, 30, 0.02)
            return True
        except Exception:
            return False

    def rebind(self, session):
        # 接上本人重新打开的同名窗口。锚点保留：窗口关着期间来的消息，回来时排在锚点之后，照样补收
        try:
            hwnd = _lst._find_window_by_title(session.group, exclude_hwnd=self.client.window.hwnd)
            if not hwnd:
                return
            root = _lst.uia.ControlFromHandle(hwnd)
            msg_list = _lst._find_message_list(root)
            if msg_list:
                session.hwnd, session.root, session.msg_list = hwnd, root, msg_list
                _lst.logger.info(f"[监听] {session.group}: 已接上重新打开的独立窗口")
        except Exception as exc:
            _lst.logger.warning(f"[监听] {session.group}: 接上窗口失败: {exc}")

    def handle(self, session, item, sender, ts):
        # 同 wx4py _handle_message，只是时间戳用估算的发送时间、raw 给 (可见项（带类名）, 发送者, 图片 PNG)；
        # 本项目监听器 auto_reply=False，不走回复分支
        # 只截图片。表情不截：动图每次截到不同帧，同一个表情去不了重
        is_image = item.class_name == MEDIA_CLASS and item.name.startswith("[图片")
        image = capture_media(session.hwnd, session.msg_list, item.control) if is_image else None
        event = _lst.MessageEvent(
            group=session.group, content=item.name, timestamp=ts,
            group_nickname=self.group_nicknames.get(session.group),
            is_at_me=self._is_at_me(session.group, item.name), raw=(item, sender, image))
        try:
            self.on_message(event)
        except Exception as exc:
            _lst.logger.exception(f"消息回调执行失败: {session.group}: {exc}")

    def keep_multiselect(session, items, senders, now):
        # 没在多选（刚开的窗口、本人或发送时退出了）就进。退出后先等一阵：发送、本人打字都要输入框；
        # 输入框里有字（本人在打）不进
        if not any(it.kind == "message" for it in items):
            return
        if any(senders):
            session.multiselect_at = now + MULTISELECT_GRACE
            return
        if now < getattr(session, "multiselect_at", 0):
            return
        session.multiselect_at = now + MULTISELECT_GRACE
        try:
            edit = _lst.WeChatGroupListener._find_chat_input(session.root)
            if edit is None or edit.GetValuePattern().Value:
                return
        except Exception:
            return
        if enter_multiselect(session.hwnd, session.msg_list):
            _lst.logger.info(f"[监听] {session.group}: 已进入多选，开始记录发送者")
            session.multiselect_warned = False
        elif not getattr(session, "multiselect_warned", False):
            session.multiselect_warned = True
            _lst.logger.warning(f"[监听] {session.group}: 进不了多选，发送者读不到；{MULTISELECT_GRACE}s 后再试"
                                "（也可在窗口里右键一条消息 → 多选）")

    def poll(self, session):
        session.scan_count += 1
        now = time.time()
        opened_at = session.__dict__.setdefault("opened_at", now)
        if not win32gui.IsWindow(session.hwnd):
            health(session, "closed", "独立窗口被关闭，收不到新消息；请在微信里双击该会话重新弹出，打开后自动接上")
            if now - getattr(session, "rebind_at", 0) >= REBIND_EVERY:
                session.rebind_at = now
                rebind(self, session)
            self._update_next_scan(session, 0)
            return
        items, senders = read_live_items(session.msg_list, _sender_names.get(session.group, ()))
        if not items:
            session.fail_count += 1
            health(session, "unreadable", "读不到消息列表（窗口最小化？微信退出或被强制下线？）")
            self._update_next_scan(session, 0)
            return
        sig = [(it.kind, it.name) for it in items]
        observed = session.__dict__.setdefault("observed", set())
        fresh = []
        bottom_at = getattr(session, "bottom_at", None)  # 上次停在底部的时刻
        if at_bottom(session.msg_list):
            fresh, move = new_message_slice(getattr(session, "anchor", None), sig, observed)
            if move:
                session.anchor = sig[-ANCHOR_LEN:]
            session.bottom_at = now
            health(session, "ok", "恢复正常")
        else:
            stray = now - (bottom_at or opened_at)
            first = getattr(session, "anchor", None) is None  # 新开的窗口没在底部：立刻滚
            if first or stray >= STRAY_LIMIT:
                health(session, "stray", "窗口没停在最底部，收不到新消息；尝试滚回底部")
                if now - getattr(session, "scroll_at", 0) >= STRAY_LIMIT:
                    session.scroll_at = now
                    scroll_to_bottom(session)
        observed.update(sig)
        added = 0
        for i in fresh:
            item = items[i]
            if item.kind != "message":
                continue
            if self.ignore_client_sent and self.outgoing_registry.should_ignore(session.group, item.name):
                continue
            added += 1
            session.new_count += 1
            handle(self, session, item, senders[i], estimate_ts(sig, i, now, bottom_at))
        keep_multiselect(session, items, senders, now)
        self._update_next_scan(session, added)

    cls._poll_session = poll
    cls._scroll_safe = True


def _wait_for_windows(wx, names: list[str], poll: float = 2.0) -> None:
    """等本人把要监听的会话都弹成独立窗口再开始。不替本人打开：打开要搜索、双击，会抢前台。
    缺哪些提示哪些（缺的变了才再提示），开齐返回；wx4py 监听器随后直接复用这些同名窗口。Ctrl-C 退出。"""
    from wx4py.features.messaging import listener as _lst

    shown = None
    while True:
        wins = _lst._find_wechat_windows()
        missing = [n for n in names if not pick_window(wins, n, wx.window.hwnd)]
        if not missing:
            return
        if missing != shown:
            _lst.logger.warning(f"[监听] 这些会话的独立窗口没开：{'、'.join(missing)}。请在微信里双击它们弹出独立窗口"
                                "（别最小化，可放在其它窗口后面），开齐后自动开始。")
            shown = missing
        time.sleep(poll)


def watch_chats(wx, groups: list[str], on_message: Callable[[dict], None],
                *, contacts: list[str] = (), block: bool = True, names: Optional[dict] = None, **options):
    """监听群和私聊的新消息。每条消息归一化成 row 后回调 on_message。
    names：{会话: 拆发送者用的已知名字}（query.sender_names）。"""
    from wx4py import CallbackHandler

    contacts = list(contacts)
    _sender_names.update(names or {})
    _install_window_match_patch()
    _install_scroll_safe_poll()
    _wait_for_windows(wx, list(groups) + contacts)

    def _cb(event):
        observed = getattr(event, "timestamp", None) or time.time()
        item, sender, image = event.raw
        on_message({
            "chat": event.group,
            "chat_type": "contact" if event.group in contacts else "group",
            "content": getattr(event, "content", "") or "",
            "sender": sender,
            "image": image,  # 图片的截图 PNG 字节（显示尺寸），存文件由调用方做
            "msg_type": "media" if item.class_name == MEDIA_CLASS else "text",
            "time_label": None,
            "ts": int(observed),
            "is_at_me": bool(getattr(event, "is_at_me", False)),
            "source": "watch",
            "bucket": str(int(observed // 60)),  # 分钟桶：防同分钟重复扫描重入
        })
        return None  # 不自动回复

    return wx.process_groups(list(groups) + contacts, CallbackHandler(_cb), block=block, **options)
