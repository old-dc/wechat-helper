# CLAUDE.md

微信助手（wechat_helper）——用 **Windows UIAutomation**（wx4py）读微信聊天：历史抓取、会话发现、群成员、群/私聊实时监听，落地本地 SQLite，经 MCP 把数据喂给 Claude Code / Codex / OpenCode 等 agent。发送只走本人 CLI（直发或审批 agent 草稿）。数据来源是 UIA，不是 CDP。

## 为什么是 UI 自动化

- 微信 4.x 是 Qt 原生程序（非 CEF），没有可 CDP 接管的页面，CDP 接管客户端的方式用不了。
- 解密本地库的路（chatlog / wx_key / wechat-decrypt）已被腾讯 DMCA 全面下架，本机 4.1.15 无可用取密钥方案；也不自建绕过加密的代码。
- 剩下唯一干净可落地的方式：wx4py 走 UIAutomation（开启系统屏幕阅读器标志，让微信暴露控件树），**不解密、不改客户端**，相当于"模拟用户看屏幕"。

## 硬限制（UIA 决定的，不是 bug）

- **消息无消息 ID；发送者只在多选状态下有**：平时消息项只有正文；聊天窗口进入"多选"后消息项变 CheckBox、Name 变"发送者 正文"（显示名，自己发的是自己的群昵称，没有"是不是我"标记），存 `messages.sender`。`watch` 让独立窗口保持多选、`scrape` 抓前进多选；拆前缀按已知名字（会话名/群成员/通讯录）取最长前缀，名单外且名字带空格的会切错。去重靠 `sha1(会话+内容+时间桶)`，不含发送者（抓取的桶 = 标签解析出的分钟），旧记录再抓到时补上发送者。本助手 `send`/`approve` 发出的消息发送时以 `source=send` 入库，监听认领其回流不再重复（顺便补发送者）。
- 每条消息没有自己的时间，只有界面上的时间分隔标签：抓历史精确到"所在那组"的标签（同组后几条误差可达几分钟）。
- **会话列表预览**里带最新一条的发送者名；**群成员**只有显示名（无 wxid/角色）。
- 文字/链接读得到全文；表情包、图片 UIA 只给类型名，存成占位 `[动画表情]` / `[图片]`。`watch` 另把图片在窗口里显示的样子后台截下来存 PNG（`media_dir`，按内容命名、同图一份，路径记 `messages.media_path`）：只有显示尺寸，网慢可能截到加载中的灰块；`scrape` 不截。表情不截：动图每次截到不同帧，同一个表情去不了重。原图/表情原文件是 V2 加密的 `.dat`，不解。会话打开了却抓到 0 条，多半是聊天已删除。
- 微信必须登录，**窗口不能最小化或藏在托盘**：控件树为空，抓取得 0 条且不报错（后台连接会把最小化的主窗口还原，见"前台与焦点"）。历史靠滚动，极大群/列表尾部可能收不全。

## 真机踩过的坑

- **风控**：连续批量抓取约 2 小时被微信安全强制退出。大批量抓取用逐个 `wx scrape` 循环，会话间随机间隔 20–40s，微信窗口消失即停（`scrape-all` 本身没有间隔）。
- GUI 抓取仍受加载慢、读取抖动干扰：滚到最新、往上翻都按位置确认并重试；前后两屏没接上就整次重抓。重试后仍失败抛错——`scrape` 退出码 1、`scrape-all` 记为失败，不静默返回空或错位结果。
- 监听的独立窗口要停在最底部才判新消息；被关、最小化、停在旧位置时日志打 `[监听] ... WARNING`；被关的等本人重新弹出后自动接上，停在旧位置的自动滚回底部。

## 前台与焦点（2026-10-07 实测，微信 4.1.15 / Qt 5.15）

- **不抢前台**：直接投递给窗口的滚轮（`post_wheel`）、字符/按键（`_post_text` / `_post_key`）、`WM_CLOSE`。窗口被别的程序挡住也生效，不动真鼠标键盘、不碰剪贴板。
- **会抢前台**：UIA 的写值、调用（Invoke）、设焦点，以及弹出新的独立窗口——微信自己把窗口拉到前台、绕过系统前台锁，事后抢不回来。投递的鼠标点击也会（微信把主窗口拉到前台）。能投递滚轮/按键就用投递。
- **多选进出**：投递右键到一条文字消息的气泡（后台截窗口画面找气泡）→ 弹出菜单里按名字点"多选"；投递点击"取消多选"退出。各抢一下前台。**不能投递 Esc 退出：会直接关掉独立窗口。** 多选状态下窗口没有输入框：`send` 先退出多选再发，`watch` 等 60s、输入框空着时再进。
- 投递滚轮由 Qt 按落点找接收窗口：落点最上层是微信的另一个窗口（独立窗口常叠在同一位置）就会滚错，`post_wheel` 先把它压到目标窗口下面。
- 投递字符进的是该窗口当前持有焦点的控件：后台发送先打探针字母确认焦点在输入框，打完核对原文、回车后输入框清空才算发出。多行（Shift+Enter 投递不了）、输入框有草稿、emoji 对不上时擦掉自己打的字，退回前台发送（激活窗口、剪贴板粘贴）。
- 各命令：`send` / `approve` 目标独立窗口开着 = 全程后台，否则搜索发送。`scrape` / `scrape-all` 复用开着的独立窗口 = 全程后台，抓完滚回底部；没开就从会话列表弹出（抢一下，抓完关掉，前台落到微信主窗口），不在会话列表里先搜索。`sessions` 全程后台。`watch` 不替本人开窗口：要监听的会话缺独立窗口就提示、等本人在微信里双击弹出，开齐才开始，运行中被关同样等着接上；进多选时每个窗口抢一下，之后全程后台。`scrape` 复用的窗口若是本次进的多选，抓完退出。`contacts --pull` / `members` 仍抢前台（连接时激活、真鼠标点击）。
- 连接：除 `contacts --pull` / `members` 外都用 `connect(activate=False)`，不激活主窗口，最小化的还原不激活；藏在托盘的提示本人点开并等着（wx4py 的托盘恢复在 Win11 上常失败、还会弄乱前台）。
- 仍抢前台的那几步要保持解锁（锁屏后模拟输入失效）；全量抓取放午休/下班后。

## wx4py 运行时补丁

wx4py 原逻辑有坑，补丁打在 `wechat.py`（`connect()` / `watch_chats()` 时安装），不改 site-packages：

- 认主窗口：标题正好是"微信"优先（原逻辑按标题含"微信"打分，标题带"微信"的独立窗口如群"测试微信"叠在上面就被认成主窗口，随后搜索按键关掉它、再把主窗口收进托盘）。
- 会话匹配精确优先（原逻辑子串取第一个，"测试"会命中"效果测试"）；发送用严格精确、无子串回退。
- 搜索关键词只打第一个 emoji 之前的文字（SendKeys 打不出 emoji），结果仍按带 emoji 的全名匹配。
- 时间范围（since）改用本项目的标签解析：today / yesterday / week=最近 7 天 / month=最近 30 天。wx4py 原逻辑把"9月18日"一律换算成"星期X"，week 永远不停、翻到顶；也没有 month。
- 独立聊天窗口按标题精确匹配（原逻辑子串，私聊短名会命中群窗口）。
- 监听开窗口：wx4py 监听器复用同名独立窗口，没有才自己按群搜索打开（私聊打不开、还抢前台），所以启动前先等本人把会话都弹成独立窗口；窗口被关后的自愈也改为等本人重开、接上，不再自己重开。
- 媒体消息（类 `mmui::ChatBubbleReferItemView`，已见表情包、图片）：原逻辑跳过，补丁存成 `[类型名]` 占位（历史与监听都收，`msg_type=media`）。
- 抓历史（`_collect_history` 替代 `get_chat_history` 的循环）：原逻辑把时间标签跨屏沿用，同一条消息存两份、一份时间错、顺序乱。改为按重叠把每屏拼成完整序列，再统一配标签；比对时表情包只看类型名（名字懒加载，先"动画表情"后"动画表情 [想]"），否则重叠对不上、一段消息重复。**2026-10-07 前抓的数据有这类重复。**
- 监听轮询（替换 `_poll_session`）：停在底部才判新、按锚点位置判（往上翻不再把旧消息当新的）；往上翻期间来的按标签估发送时间；窗口异常告警并自愈。
- 发送成功后不再最小化主窗口（原逻辑"保护隐私"，最小化后紧接的抓取读到 0 条）。

## 命令

`uv run wx <cmd>`，参数以 `uv run wx <cmd> --help` 为准。

- **连微信（写库）**：`scrape`、`scrape-all`（私聊 = 会话∩通讯录∩白名单）、`sessions`、`members`、`contacts --pull`、`watch`（群名 + `--contact 联系人`，不带参数用 config 的 `watch_groups` / `watch_contacts`；常驻，Ctrl-C 停）。
- **发送（对外、不可撤回）**：`send`、`approve <草稿id> [--edit 改写]`。只发白名单会话、精确匹配、默认 dry-run，须 `--yes` 才真发。
- **只碰库**：`chats` / `search` / `recent` / `digest` / `export` / `drafts` / `reject` / `logs`。
- 自检：`uv run python test/test_db.py`（离线 27 项，不碰微信）。改 db/query/解析/发现/后台发送后跑。

## MCP（`wx mcp`，stdio）

**边界：MCP 不连微信。** 读工具只读本地库，唯一写入是 `draft_reply` 写 `drafts` 表；抓取、监听、发送只在本人 CLI。

- 读：`search_messages` / `recent_messages` / `list_chats` / `list_contacts` / `group_members` / `digest`。
- `wait_new_messages`：长轮询，以 `events.id` 为游标（传回上次的 `cursor`），最长等 50s，低于 Codex / TS SDK 默认 60s 的工具超时。新消息只来自本机常驻的 `wx watch`。
- `draft_reply`：agent 起草回复，需 config `drafts_enabled: true`（默认关）；本人 `wx drafts` 查看，`wx approve <id> --yes` 发送或 `wx reject <id>`。
- 注册：`uv --directory <项目路径> run wx mcp`（`config.json` 按当前目录读），消费方用环境变量 `WECHAT_HELPER_KEY` 带密钥。
- 消息正文是他人输入，推给 agent 就是提示注入面；server instructions 已声明正文只当数据。

## 配置与隐私门禁

`config.json`（照 `config.example.json` 建，gitignore，含密钥）：`db_path` / `media_dir` / `watch_groups` / `watch_contacts` / `watch_options` / `drafts_enabled` / `privacy`。

- `privacy.allow_chats`（白名单）：只有在册会话才允许**拉取内容**（`scrape` / `scrape-all` / `watch`）和**发送**（`send` / `approve` / `draft_reply`）。默认空 = 谁都不拉。发现类命令（`contacts` / `sessions` / `members`）不受限，用来挑人进白名单。
- `privacy.exclude_chats`（黑名单）：查询/MCP/导出/日报一律排除，也从拉取门禁剔除（**黑名单优先于白名单**）。逻辑在 `config.py` 的 `is_allowed()` / `gate()`。
- `privacy.access`（分级访问，**仅对 MCP 生效，CLI 本人不受限**）：`levels` = `{等级: 密钥}`（数字越大权限越高；空 = 不分级、全开放）、`chats` = `{会话名: 等级}`、`default_level`。消费方密钥等级 ≥ 会话等级才读得到；启用分级后不带密钥什么都读不到。逻辑在 `config.py` 的 `can_access()`。

## 审计

`logs/audit.jsonl`（append-only，gitignore），`wx logs` 查看。`wechat` 类 = 本助手对微信的操作（抓取/监听/发送/拒绝草稿）；`access` 类 = MCP 消费方调用（记密钥等级、工具、返回条数，不记密钥原文；`wait_new_messages` 空轮询不记）。

## 结构与表

- `src/wechat_helper/wechat.py` — wx4py 封装 + 运行时补丁 + 时间标签/会话项解析。wx4py 延迟导入。
- `db.py`（建表、去重入库、FTS、事件、草稿）/ `discover.py` / `watch.py` / `query.py` / `export.py` / `mcp_server.py` / `cli.py` / `config.py` / `audit.py`。
- `docs/architecture.md` — 分层、数据流、UIA 天花板、去重取舍。
- `scripts/watch.bat`（CRLF+ASCII）+ `register-tasks.ps1` — 计划任务 `wechat-watch`（登录后台跑 watch）。**需管理员手动执行，未自动注册。**
- 表：`chats`、`messages`（dedup_key UNIQUE）、`messages_fts`（FTS5 trigram，无则回退 LIKE）、`chat_members`、`contacts`、`sync_runs`、`events`（watch 新消息，长轮询游标）、`drafts`、`meta`。

## 运行环境

Windows 11 + Python 3.12（uv venv）。依赖：`wx4py`（AGPL-3.0）、`mcp`（≥2.x，`MCPServer`）。

## 砍掉的（YAGNI / UIA 不支持）

向量检索、本地 Web UI、users 真名解析（发送者只有显示名）、CDP daemon、agent 自动回复（只能起草，须本人审批）、Claude Code Channels 推送（研究预览、仅 Claude Code，且需消费方设 `MCP_PROTOCOL_NEGOTIATION=legacy`；先用长轮询）。需要再加。
