# 🚀 雪球舆情自动化采集专家 (Xueqiu Smart Collector)

本项目是一个基于 **Playwright** 和 **小龙虾 (OpenClaw) Skill 规范** 开发的工业级舆情爬虫。它采用"触发器+核心逻辑"的架构，具备随机行为模拟、静默期闭环、断点续传及飞书实时告警功能。

---

## 🤖 给 AI Assistant 的操作指南

如果你是操作此 Skill 的 AI，请务必掌握以下逻辑：

### 1. 输入参数 (Input Parameters)

通过 `manifest.json` 传递，你在调用时可以设置：

| 参数 | 类型 | 说明 |
|------|------|------|
| `run_minutes` | `int` | 本次任务抓取的最大时长（分钟）。 |
| `debug_mode` | `bool` | `False` (默认): 无头模式运行，模拟真人随机延迟，检查静默期。<br>`True`: 有头模式运行，跳过一切延迟和静默检查，立即弹出窗口供用户操作。 |

### 2. 用户自然语言输入（自动添加博主）

当用户消息命中以下任一模式时，应当**主动**调用 `xueqiu_add_blogger.add_blogger_from_user_input()`，**不要让用户手动写 SQL**：

- 含雪球链接：例如 `https://xueqiu.com/u/5992135535` 或 `https://xueqiu.com/5992135535/389370514`
- 含『抓取 / 添加 / 监控』等动词搭配博主昵称：例如 "帮我抓取白酒的帖子"、"添加博主：九航笔记"

处理多结果时：搜索接口可能返回多名匹配博主，应**先把候选列表展示给用户**，等用户回复编号后再二次调用 `add_blogger_by_uid(uid, name)`。详细协议见 `SKILL.md`。

### 3. 输出与反馈 (Output)

核心方法会返回以下信息，请根据内容转达给用户：

- **数字**: 代表本轮成功处理的博主总数。
- **`🚨 【环境未初始化】...`**: 引导用户开启调试模式进行首次登录。
- **`📭 【任务表为空】...`**: 引导用户去数据库 `blogger_tasks` 插入 UID。
- **`⚠️ 缺少配置文件...`**: 引导用户检查 `.env` 文件。
- **抓取回执 `recent_posts`**: 当用户问"博主都说了什么"时，agent 直接基于此字段回答（避免重复查 DB），详见下文"抓取回执与事后查询"。

---

## 🛠️ 环境准备 (Setup)

### 1. 基础依赖

```bash
pip install playwright playwright-stealth pymysql python-dotenv requests
playwright install chromium
```

### 2. 数据库配置

在 MySQL (43306 端口) 中创建 `local` 数据库，并执行 `SKILL.md` 中提供的建表 SQL。

### 3. 配置文件 `.env`

在项目根目录创建 `.env`，参考如下：

```ini
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=hub_user
POSTGRES_PASSWORD=hub_password
POSTGRES_DB=financial_hub

FEISHU_WEBHOOK=飞书机器人链接
BROWSER_HEADLESS=True

# 随机触发配置
QUIET_START=23:15
QUIET_END=07:30
START_DELAY_MIN=1
START_DELAY_MAX=10
RUN_DURATION_MIN=20
RUN_DURATION_MAX=30
```

---

## 📖 使用流程 (Workflow)

作为开发者或 AI，请确保按以下顺序"开荒"：

### 第一步：下发任务（两种方式任选其一）

**方式 A：自动添加（推荐）**

在对话中发送雪球博主链接或博主昵称关键词，agent 会自动添加并立即抓取：

```text
"帮我抓取白酒的帖子"
"https://xueqiu.com/u/5992135535"
"添加博主：九航笔记"
```

详见 `SKILL.md` 与下文"自动添加博主"章节。

**方式 B：手动 SQL（保留给高级用户）**

```sql
INSERT INTO blogger_tasks (user_id, screen_name, priority) VALUES (9483527429, '九航笔记', 10);
```

### 第二步：人工预热 (首次登录)

> ⚠️ AI 必须提醒用户进行此操作。

**指令示例：** "开启调试模式运行雪球采集。"

此时会弹出浏览器，请手动完成手机号登录和滑动验证。成功后，状态会存入 `xueqiu_user_data`。

### 第三步：全自动巡检

此后，小龙虾 Agent 会根据 `xueqiu_random_trigger.py` 的逻辑，在非静默期随机启动，自动完成增量/全量采集。

---

## ✨ 自动添加博主（新功能）

为方便在对话中"自然"地添加博主，skill 提供了 `xueqiu_add_blogger.py`，识别以下两类输入：

| 用户输入 | 行为 |
|---|---|
| 雪球链接（含 `xueqiu.com/u/<UID>` 或 `xueqiu.com/<UID>/<POST_ID>`） | 提取 UID → 写入 `xueqiu_blogger_tasks` → 立即 mini 抓取 |
| 博主昵称关键词（如"白酒"） | 调雪球搜索 API → 单结果直接写表抓取 / 多结果让用户选 |

### 关键词模式的多结果协议

搜索接口可能返回多名匹配博主，此时 skill **不写表**，而是先向用户展示候选：

```text
找到 2 个匹配『白酒』的博主，请选择要抓取哪一个：
1. 白酒医疗都是坑 (UID: 1901205114) — 粉丝 7,054 | 帖子 446
   简介: 这里没有价值
2. 白酒 (UID: 7069982150) — 粉丝 18 | 帖子 2
   简介: （无）
请回复编号 1 / 2 / ... 即可。
```

用户在回复编号前，**不要写表**，避免误添加。

### CLI 用法

```bash
# URL 模式
python xueqiu_add_blogger.py --url "https://xueqiu.com/u/5992135535"

# 关键词模式
python xueqiu_add_blogger.py --keyword "白酒"

# 直接指定 UID + 昵称
python xueqiu_add_blogger.py --uid 5992135535 --name "九航笔记"

# 仅写表，不立即抓取
python xueqiu_add_blogger.py --keyword "白酒" --no-crawl
```

---

## 🔍 抓取回执与事后查询

### 抓取回执自带最近 5 条帖子预览

`crawl_single_blogger()` 完成后，回执字典中会自动带 `recent_posts` 字段：

```json
{
  "new_count": 7,
  "fetched_pages": 2,
  "recent_posts": [
    {
      "id": 389370514,
      "content_preview": "今天白酒板块放量上涨，资金面出现明显分歧...（前 200 字）",
      "comment_time": "2026-06-16 13:45:00",
      "stock_codes": "SH600519,SZ000858",
      "stock_names": "贵州茅台,五粮液"
    },
    ...   // 最多 5 条
  ]
}
```

agent 拿到回执后即可直接回答"博主都说了什么"，**无需再次查 DB**。

### 事后查询（不重新抓取）

若用户**过了一段时间**再追问"再看看他昨天发了什么"，agent 应调用 `query_recent_posts()`，**不**触发新抓取：

```python
from datetime import datetime, timedelta
from xueqiu_add_blogger import query_recent_posts

posts = query_recent_posts(
    uid=5992135535,
    limit=20,
    since=datetime.now() - timedelta(days=1),
)
```

返回 `list[dict]`，按 `comment_time DESC` 排序，每条含 `id / content_preview / comment_time / stock_codes / stock_names / ip_location`。

### 何时用哪种

| 场景 | 用什么 |
|---|---|
| 用户对**刚抓完**的博主追问 | 回执里的 `recent_posts` 字段（零 DB 成本） |
| 用户对**更早时间段**追问 | `query_recent_posts(uid, limit, since)` |
| 用户想看"近 7 天"或"近 30 天"汇总 | `query_recent_posts(uid, limit=N, since=N 天前)` |

### 事后查询 CLI

```bash
# 查 UID=5992135535 最近 10 条
python xueqiu_add_blogger.py --query-uid 5992135535

# 查近 7 天最多 20 条
python xueqiu_add_blogger.py --query-uid 5992135535 --query-limit 20 --query-since-days 7
```

---

## 📁 目录结构说明

| 文件/目录 | 说明 |
|-----------|------|
| `xueqiu_random_trigger.py` | 入口文件。负责锁文件管理、随机延迟、静默期判定。 |
| `xueqiu_crawl_skill.py` | 核心逻辑。负责 Playwright 操作、Fetch 数据、数据库读写。 |
| `xueqiu_add_blogger.py` | 博主自动添加工具。识别雪球链接 / 搜索关键词，写入抓取表并触发 mini 抓取。 |
| `SKILL.md` | OpenClaw skill 自述：触发条件、调用协议、多结果选择流程。 |
| `SKILL_SETUP.md` | 环境初始化与建表 SQL。 |
| `hub_xueqiu_adapter.py` | Hub 控制台适配器（统一调度层）。 |
| `logs/` | 日志目录。按天生成 `.log` 文件，并自动清理 7 天前的记录。 |
| `xueqiu_user_data/` | 浏览器环境目录。保存登录后的 Cookies 和 Session。 |

---

## 🚨 异常排查

| 异常 | 排查方法 |
|------|----------|
| **飞书告警** | 若收到告警及截图路径，请前往 `logs/` 查看对应的 `.png` 图片。 |
| **锁文件占用** | 若提示"环境占用"，请检查是否存在 `xueqiu_run.lock`。若确定无进程运行，可手动删除该文件。 |
