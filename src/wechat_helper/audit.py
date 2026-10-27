"""审计日志：追加式 JSONL，记录两类事件。

- category="wechat"：本助手对微信的操作（拉取/发送），action=scrape/sessions/members/
  contacts/watch_*/send 等。
- category="access"：外部 MCP 消费方对本服务的调用，记密钥对应的等级(key_level)、
  工具名、参数摘要、返回条数。**不记密钥原文。**

默认写 logs/audit.jsonl（gitignore）。只追加、不覆盖，便于审计与排查。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _path(cfg: dict) -> Path:
    return Path(cfg.get("log_path", "logs/audit.jsonl"))


def log(cfg: dict, category: str, action: str, **fields) -> None:
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": int(time.time()),
           "category": category, "action": action}
    rec.update(fields)
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read(cfg: dict, *, category: str | None = None, limit: int = 50) -> list[dict]:
    """按时间倒序读最近 limit 条，可按 category 过滤。"""
    p = _path(cfg)
    if not p.exists():
        return []
    out = []
    for line in reversed(p.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if category and rec.get("category") != category:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out
