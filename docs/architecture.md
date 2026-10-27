# 架构

数据来源是 Windows UIAutomation（wx4py），不是 CDP/内部接口。

## 分层

```
采集（wx4py / UIA）  →  数据层（SQLite）  →  查询/服务
   wechat.py              db.py              query.py（只读）
                          discover.py        mcp_server.py（只读 MCP）
                          watch.py           cli.py / export.py
```

- **wechat.py（采集/引擎）**：封装 wx4py。`connect()` / `scrape_history()` / `list_sessions()` / `list_group_members()` / `watch_groups()`，以及时间标签、会话项解析。wx4py 延迟导入——离线自检与读库命令不加载它。
- **db.py（数据层）**：建表、去重入库、FTS、台账、成员、事件、隐私过滤片段。
- **discover.py（发现）**：把会话列表落 `chats`、群成员落 `chat_members`。黏合"连微信拉数据"与"写库"。
- **watch.py（监听）**：常驻监听群新消息 → `messages` + `events`；启动时先刷一次会话列表。
- **query.py（只读查询）**：全文搜索（FTS≥3字 / LIKE<3字）、最近、会话、成员、日报，全部应用隐私黑名单。
- **export.py**：导出某会话消息 json/csv/md。
- **mcp_server.py**：只读 MCP（stdio），把库暴露给 Claude 等 agent 当信源。
- **cli.py**：命令入口，读 `config.json`。

## 数据流

- **历史**：`scrape` → `wechat.scrape_history`（滚动抓当前会话）→ `db.upsert_messages` → 记 `sync_runs`。
- **会话列表**：`sessions` → `wechat.list_sessions`（滚动收全）→ `db.upsert_chat_meta`（未读/置顶/预览）。
- **群成员**：`members` → `wechat.list_group_members` → `db.replace_members`（整体替换）。
- **新消息**：`watch` → `wechat.watch_groups`（每群独立窗口轮询 UIA）→ 回调去重入库 + 写 `events`。

## 表

- `chats`：name PK、type、origin、pinned/muted/unread、preview、last_time_label、msg_count、first/last_seen。
- `messages`：content、sender（显示名，可空）、media_path（图片截图 PNG，可空）、msg_type（text/link/system）、time_label、ts、is_at_me、source（scrape/watch）、dedup_key UNIQUE。
- `messages_fts`：FTS5 trigram 外部内容表，触发器自动同步；无 FTS5 时静默跳过，搜索全走 LIKE。
- `chat_members`：group_name + member（显示名，无 wxid）。
- `sync_runs` / `events` / `meta`：台账 / 监听事件游标 / 键值。

## UIA 天花板（不是 bug）

- **消息无消息 ID**；**发送者只在多选状态下有**（Qt UIA 平时只给正文；聊天窗口进"多选"后消息项 Name 带"发送者 "前缀，只有显示名）。去重靠 `sha1(会话+内容+时间桶)`，不含发送者；没有 users 真名解析。
- **会话列表预览**里带最新一条的发送者名。
- **群成员**只有显示名。
- 微信必须登录且前台；历史靠滚动、拿不全、极大群/列表尾部可能收不全。

## 去重的已知取舍

`ponytail`：同一时间桶内内容完全相同的消息会被并为一条，无法区分真正的重复发送。scrape 用消息时间标签作桶，watch 用观察时间的分钟桶。

## 隐私

`config.json` → `privacy.exclude_chats`（会话名列表）。查询层、MCP、导出、日报统一用 `db.excluded_sql()` 排除。
