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

### 2. 输出与反馈 (Output)

核心方法会返回以下信息，请根据内容转达给用户：

- **数字**: 代表本轮成功处理的博主总数。
- **`🚨 【环境未初始化】...`**: 引导用户开启调试模式进行首次登录。
- **`📭 【任务表为空】...`**: 引导用户去数据库 `blogger_tasks` 插入 UID。
- **`⚠️ 缺少配置文件...`**: 引导用户检查 `.env` 文件。

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

### 第一步：下发任务

在数据库 `blogger_tasks` 表中添加你想要监控的雪球博主。

**示例:**
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

## 📁 目录结构说明

| 文件/目录 | 说明 |
|-----------|------|
| `xueqiu_random_trigger.py` | 入口文件。负责锁文件管理、随机延迟、静默期判定。 |
| `xueqiu_crawl_skill.py` | 核心逻辑。负责 Playwright 操作、Fetch 数据、数据库读写。 |
| `logs/` | 日志目录。按天生成 `.log` 文件，并自动清理 7 天前的记录。 |
| `xueqiu_user_data/` | 浏览器环境目录。保存登录后的 Cookies 和 Session。 |
| `manifest.json` | Skill 插件声明文件。 |
| `SKILL.md` | 给 Agent 提供的深度逻辑自述文档。 |

---

## 🚨 异常排查

| 异常 | 排查方法 |
|------|----------|
| **飞书告警** | 若收到告警及截图路径，请前往 `logs/` 查看对应的 `.png` 图片。 |
| **锁文件占用** | 若提示"环境占用"，请检查是否存在 `xueqiu_run.lock`。若确定无进程运行，可手动删除该文件。 |
