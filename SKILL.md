---
name: xueqiu-add-blogger
description: 当用户发送雪球博主链接（主页或帖子）或博主昵称关键词时，自动将博主加入 xueqiu_blogger_tasks 抓取表，并立即对该博主启动一次 mini 抓取。处理搜索接口返回多条候选时，会让用户在多个博主中选一个。
metadata:
  openclaw:
    requires:
      bins: ["python3"]
      env: [".env"]
---

# 雪球博主自动添加 Skill

本 skill 把用户自然语言中"想抓取某博主"的意图，直接落到 `xueqiu_blogger_tasks` 表，并立即执行一次单博主抓取，**无需再让用户手动执行 SQL**。

## When to use

满足下列任一条件，应当激活本 skill：

- 用户消息中包含雪球链接：
  - `https://xueqiu.com/u/5992135535`（博主主页）
  - `https://xueqiu.com/5992135535/389370514`（帖子详情）
  - 含 query / anchor 的变体
- 用户消息中含『抓取 / 添加 / 监控』等动词，搭配博主昵称或关键词：
  - "帮我抓取白酒的帖子"
  - "添加博主：九航笔记"
  - "我想监控这个博主：xxx"
- 搜索接口返回了多个候选博主，需要用户二次选择时，agent 应当把候选列表展示给用户，等用户回复编号后再调 `add_blogger_by_uid()`。
- 用户**事后**追问博主最近说了什么（例如"再看看他昨天发了什么"），agent 应调用 `query_recent_posts(uid, limit, since)`，**不要**重新触发抓取。
- **回执中 `diagnosis.need_user_login == true`**：agent **必须**告诉用户重新登录，详见"登录态失效协议"。

## 入口调用

```python
from xueqiu_add_blogger import add_blogger_from_user_input

# URL 模式 或 关键词模式 自动分流
result = add_blogger_from_user_input("帮我抓取白酒的帖子")
```

返回的 `result["message"]` 字段是**给用户看的自然语言回执**，agent 应直接转达。

返回结构：
```json
{
  "ok": true,
  "mode": "keyword",                     // "url" 或 "keyword"
  "count": 3,
  "auto_added": false,
  "need_user_choice": true,              // 多结果时为 true
  "candidates": [...],                   // 多结果时附带候选
  "recent_posts": [                      // 抓取完成时附带最近 5 条预览
    {
      "id": 123,
      "content_preview": "...",
      "comment_time": "2026-06-16 13:45:00",
      "stock_codes": "SH600519,SZ000858",
      "stock_names": "贵州茅台,五粮液"
    }
  ],
  "message": "✅ 找到唯一匹配的博主『白酒医疗都是坑』..."
}
```

**agent 拿到回执后：**
- 直接把 `message` 字段复制给用户看（已包含最近一条帖子的摘要）；
- 也可以用 `recent_posts` 字段做更结构化的展示，例如表格或代码块。

## 处理流程

### Step 1：识别用户输入
- 优先用 `parse_xueqiu_url(text)` 提取 `xueqiu.com/(u/)?(\d+)` 中的 UID。
- 提取不到 → 视为搜索关键词。

### Step 2a：URL 模式
1. 拿到 UID 后直接调 `add_blogger_by_uid(uid, "UID:<uid>")`
2. 写表（`upsert_blogger`）
3. 立即调 `XueqiuSmartSkill.crawl_single_blogger()` 跑 mini 抓取
4. 回执：`"✅ 识别到博主链接 UID=xxx，已新增并完成 mini 抓取，新增 N 条动态。"`

### Step 2b：关键词模式
1. 调 `search_user_in_browser(keyword)` —— **在 Playwright 浏览器内 fetch** `/query/v1/search/user.json`，复用 `xueqiu_user_data` 已登录 Cookie，绕过 `md5__1038` 这类反爬签名。
2. 按接口 `list[]` 数量分流：
   - **0 条**：回执 `"未找到匹配博主..."`，建议改用链接。
   - **1 条**：自动写表 + 抓取，回执 `"✅ 找到唯一匹配的博主 XXX(UID=xxx)，已加入抓取表并完成 mini 抓取..."`。
   - **>1 条**：**不写表**，把候选列表展示给用户，等用户回复编号后再次调 `add_blogger_by_uid(uid, screen_name)`。

### Step 3：抓取
`crawl_single_blogger()` 在 `xueqiu_crawl_skill.py` 中：
- 起 Playwright 持久化 Chromium
- 拉 1-3 页（首页无新增则提前结束）
- 保留 8-14s 翻页间隔（避免风控）
- 抓完标记 `status=2`（进入增量模式）
- 用完即关 Playwright，不留后台进程

## 多结果选择协议

```text
agent: 找到 2 个匹配『白酒』的博主，请选择要抓取哪一个：
       1. 白酒医疗都是坑 (UID: 1901205114) — 粉丝 7,054 | 帖子 446
          简介: 这里没有价值
       2. 白酒 (UID: 7069982150) — 粉丝 18 | 帖子 2
          简介: （无）
       请回复编号 1 / 2 / ... 即可。

user: 1
agent: ✅ 已添加『白酒医疗都是坑』(UID=1901205114)，开始 mini 抓取...
```

**注意**：在用户回复编号前，**不要写表**，避免误添加。

## 静默期 / 风控

- 本 skill 是用户主动触发，**不**走 `xueqiu_random_trigger.py` 的静默期检查。
- 但 mini 抓取内部保留 8-14s 翻页间隔，与主链路行为一致，避免触发风控。
- 若 `.env` 缺失或数据库不可达，应回执友好的中文错误（参考 `xueqiu_crawl_skill.py:247-266`）。

## 登录态失效协议

**触发条件**：`diagnosis.need_user_login == true`（由雪球 API 返回 `error_code: 10022` 等"请登录"信号识别）。

**agent 必须**：

1. **明确**告诉用户"需要重新登录雪球"（不要含糊）
2. 给出**具体命令**：`python3 xueqiu_random_trigger.py --debug`（让用户自己跑）
3. **不要**自己重试抓取（依赖用户在有头浏览器里手动输验证码，agent 帮不了）
4. **不要**误判为"风控"、"网络问题"、"博主真无动态"——`diagnosis.need_user_login` 是明确信号
5. 等用户重新发送链接/关键词后再调 `add_blogger_from_user_input()`

**agent 模板回复**：

```text
🔐 检测到登录态失效。请运行：
   `python3 xueqiu_random_trigger.py --debug`
   重新登录后，**重新发送刚才的链接/关键词**给我。
```

（这条 message 由 `_format_diagnosis_line()` 自动生成，agent 直接复制 `result["message"]` 给用户即可。）

## 事后查询（不重新抓取）

抓取完成后，agent **回执中已带 `recent_posts` 字段**（最近 5 条帖子预览），用户立即能问"博主都说了什么"。

但若用户**过了一段时间**再次追问（例如"再看看他昨天发了什么"），不应再次抓取（费时、可能进静默期），而应：

```python
from datetime import datetime, timedelta
from xueqiu_add_blogger import query_recent_posts

# 查博主 UID=5992135535 昨天的帖子
posts = query_recent_posts(
    uid=5992135535,
    limit=20,
    since=datetime.now() - timedelta(days=1),
)
```

返回 `list[dict]`，每条含 `id / content_preview / comment_time / stock_codes / stock_names / ip_location`，按时间倒序。

**判断用哪种：**
- 用户对**刚抓完**的博主追问 → 用回执的 `recent_posts` 字段（避免重复查 DB）。
- 用户对**更早时间段**追问 → 调 `query_recent_posts(uid, limit, since)`。

## 关键文件

| 文件 | 角色 |
|---|---|
| `xueqiu_skill/xueqiu_add_blogger.py` | 入口：URL 解析 / 搜索 / 写表 / 触发抓取 / 事后查询 |
| `xueqiu_skill/xueqiu_crawl_skill.py` | `XueqiuSmartSkill.crawl_single_blogger()` 提供 mini 抓取能力，回执含 recent_posts |
| `xueqiu_skill/hub_xueqiu_adapter.py` | 提供 ON CONFLICT 写入范式（`sync_hub_targets_to_local_bloggers`） |
| `xueqiu_skill/SKILL_SETUP.md` | 首次部署 / 数据库建表参考 |
