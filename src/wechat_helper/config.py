"""配置加载：读当前目录 config.json，与默认值深合并。含隐私黑名单。"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "db_path": "data/wechat.db",
    "media_dir": "data/media",  # 监听截下的图片 PNG
    "log_path": "logs/audit.jsonl",
    "watch_groups": [],
    "watch_options": {"tick": 0.3, "batch_size": 8, "tail_size": 8},
    # MCP 起草回复（draft_reply，只写 drafts 表、不碰微信，需本人 wx approve 才发）。默认关。
    "drafts_enabled": False,
    "privacy": {
        # 白名单：只有在册的会话才允许"拉取消息内容"（scrape/scrape-all/watch）。
        # 默认空 = 谁都不拉，需手动放入。门禁不影响发现类命令（contacts/sessions/members）。
        "allow_chats": [],
        # 黑名单：查询/MCP/导出/日报一律排除；也从拉取门禁里剔除（黑名单优先于白名单）。
        "exclude_chats": [],
        # 分级访问控制（面向多消费方，仅 MCP 读取时生效；CLI 本人不受限）：
        #   levels: {等级: 密钥}，每级一个密钥，等级数字越大权限越高；空=不启用分级(全开放)
        #   chats:  {会话名: 等级}，给白名单会话设敏感等级
        #   default_level: 未分级会话的默认等级
        # 消费方启动 MCP 时用环境变量 WECHAT_HELPER_KEY 表明身份；其密钥对应的等级
        # >= 会话等级才可读。
        "access": {
            "levels": {},
            "chats": {},
            "default_level": 1,
        },
    },
}


def _merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝
    path = Path("config.json")
    if path.exists():
        _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
    return cfg


def excluded(cfg: dict) -> list[str]:
    return list(cfg.get("privacy", {}).get("exclude_chats", []))


def allowed(cfg: dict) -> list[str]:
    return list(cfg.get("privacy", {}).get("allow_chats", []))


def is_allowed(name: str, cfg: dict) -> bool:
    """能否拉取该会话的消息：在白名单内且不在黑名单内。白名单空=一律不允许。"""
    return name in set(allowed(cfg)) and name not in set(excluded(cfg))


def _access(cfg: dict) -> dict:
    return cfg.get("privacy", {}).get("access", {}) or {}


def clearance_of(key, cfg: dict) -> int:
    """密钥对应的权限等级；匹配多个取最高，无匹配/无密钥返回 0。"""
    best = 0
    for lvl, k in _access(cfg).get("levels", {}).items():
        if k == key and key is not None:
            try:
                best = max(best, int(lvl))
            except (TypeError, ValueError):
                pass
    return best


def chat_level(chat: str, cfg: dict) -> int:
    acc = _access(cfg)
    try:
        default = int(acc.get("default_level", 1))
    except (TypeError, ValueError):
        default = 1
    try:
        return int(acc.get("chats", {}).get(chat, default))
    except (TypeError, ValueError):
        return default


def can_access(chat: str, key, cfg: dict) -> bool:
    """消费方(持 key)能否读取该会话：密钥等级 >= 会话等级。
    未配置任何等级 = 不启用分级，默认全开放。"""
    if not _access(cfg).get("levels"):
        return True
    return clearance_of(key, cfg) >= chat_level(chat, cfg)


def gate(names, cfg: dict) -> list[str]:
    """过滤出允许拉取消息的会话（白名单门禁 + 黑名单剔除），保持顺序去重。"""
    allow = set(allowed(cfg))
    ex = set(excluded(cfg))
    seen, out = set(), []
    for n in names:
        if n in allow and n not in ex and n not in seen:
            seen.add(n)
            out.append(n)
    return out
