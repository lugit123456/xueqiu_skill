---
name: xueqiu-spider-setup
description: 为雪球舆情采集系统初始化虚拟环境、安装 Playwright 浏览器依赖、配置 PostgreSQL 数据库并初始化数据表。
metadata:
  openclaw:
    requires:
      bins: ["python3"]
---

# 雪球舆情采集系统环境配置 Skill

本 skill 完成雪球舆情采集的虚拟环境创建、Python 依赖安装、Playwright 浏览器核心下载，以及规范化的 PostgreSQL 数据库建表初始化。

**本 skill 不涉及具体的抓取逻辑，仅做环境与数据库结构的初始化。**

## When to use

在以下情况下应当执行本 skill：
- 项目首次部署或从仓库克隆后需要进行初始化。
- 项目根目录下的虚拟环境 `.venv/` 不存在。
- 项目根目录下的 `.env` 配置文件丢失。
- 自动化运行前需要确保 PostgreSQL 数据库中已创建对应的数据表。
- 其他功能 skill 报出"依赖未安装"、"未找到浏览器"或"数据库连接失败"等错误。

## Step 1: Create virtual environment

检查 `{baseDir}/.venv` 是否存在。如果不存在，则执行以下命令创建 Python 虚拟环境：

```bash
python3 -m venv {baseDir}/.venv
```

## Step 2: Install Python dependencies & Playwright Browser

激活虚拟环境，升级包管理工具并安装项目依赖，同时拉取 Playwright 所需的 Chromium 内核：

```bash
{baseDir}/.venv/bin/pip install -r {baseDir}/requirements.txt
{baseDir}/.venv/bin/playwright install chromium
```

核心依赖说明 (defined in requirements.txt):

- **playwright** — 提供自动化浏览器接管与登录。
- **playwright-stealth** — 绕过 WAF 防护检测。
- **psycopg2-binary** — 规范连接统一的投研数据库。
- **python-dotenv** — 加载项目环境变量。
- **requests** — HTTP 请求与会话管理。

## Step 3: Configure environment variables

检查 `{baseDir}/.env` 是否存在。如果不存在，从模板生成：

```bash
if [ ! -f "{baseDir}/.env" ]; then
  cat > "{baseDir}/.env" << 'EOF'
# PostgreSQL 数据库配置 (金融数据中心规范)
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=hub_user
POSTGRES_PASSWORD=hub_password
POSTGRES_DB=financial_hub

# 采集配置
XUEQIU_USER_DATA_DIR=./xueqiu_user_data
CRAWL_RUN_MINUTES=20
BROWSER_HEADLESS=True

# 调度随机性配置
START_DELAY_MIN=0
START_DELAY_MAX=2
RUN_DURATION_MIN=11
RUN_DURATION_MAX=17

# 静默期配置
QUIET_START=23:30
QUIET_END=08:00

# 飞书 Webhook
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/******
EOF
fi
```

统一数据库变量规范对照表：

| Variable | Description | Default |
|----------|-------------|---------|
| POSTGRES_HOST | PostgreSQL 服务器地址 | 127.0.0.1 |
| POSTGRES_PORT | PostgreSQL 服务器端口 | 5432 |
| POSTGRES_USER | 数据库用户（读写权限） | hub_user |
| POSTGRES_PASSWORD | 数据库用户密码 | hub_password |
| POSTGRES_DB | 数据库名称（所有爬虫汇聚于此） | financial_hub |

## Step 4: Verify setup & Initialize database table

运行以下内联脚本。该脚本将验证 .env 配置的正确性，连接到 PostgreSQL 实例，并在 financial_hub 数据库下自动创建数据表：

```bash
{baseDir}/.venv/bin/python -c "
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv('{baseDir}/.env')

conn = psycopg2.connect(
    host=os.getenv('POSTGRES_HOST', '127.0.0.1'),
    port=int(os.getenv('POSTGRES_PORT', '5432')),
    user=os.getenv('POSTGRES_USER'),
    password=os.getenv('POSTGRES_PASSWORD'),
    dbname=os.getenv('POSTGRES_DB')
)
cursor = conn.cursor()

# 创建博主任务表
cursor.execute('''
CREATE TABLE IF NOT EXISTS xueqiu_blogger_tasks (
    user_id BIGINT PRIMARY KEY,
    screen_name VARCHAR(100),
    priority INTEGER DEFAULT 0,
    last_crawl_time TIMESTAMP,
    status SMALLINT DEFAULT 0,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    checkpoint_page INTEGER DEFAULT 0,
    total_posts_count INTEGER DEFAULT 0
)
''')

# 创建帖子内容表
cursor.execute('''
CREATE TABLE IF NOT EXISTS xueqiu_blogger_posts (
    id BIGINT PRIMARY KEY,
    user_id BIGINT,
    screen_name VARCHAR(100),
    content TEXT,
    stock_codes VARCHAR(255),
    stock_names VARCHAR(255),
    comment_time TIMESTAMP,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ip_location VARCHAR(50),
    raw_json JSONB,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
)
''')

conn.commit()
print('✓ Database connection successful!')
print('✓ Tables initialized successfully.')

cursor.close()
conn.close()
"
```

如果上述命令成功输出 `✓ Database connection successful!`，则代表该 Skill 的运行环境与底层数据表全部就绪。

## 使用方法

### 1. 下发任务

在数据库 `xueqiu_blogger_tasks` 表中添加想要监控的雪球博主：

```sql
INSERT INTO xueqiu_blogger_tasks (user_id, screen_name, priority) VALUES (9483527429, '九航笔记', 10);
```

### 2. 首次登录（必须）

首次使用需要手动登录雪球账号：

```bash
source .venv/bin/activate
python xueqiu_random_trigger.py --debug
```

在弹出的浏览器中完成手机号登录和滑动验证。成功后状态会保存到 `xueqiu_user_data` 目录，后续即可全自动运行。

### 3. 正常运行

```bash
source .venv/bin/activate
python xueqiu_random_trigger.py
```

### 数据库表说明

| 表名 | 说明 |
|------|------|
| `xueqiu_blogger_tasks` | 博主任务表（UID、名称、优先级、状态、断点页码） |
| `xueqiu_blogger_posts` | 帖子内容表（内容、股票代码、发布时间、IP属地、原始JSON） |

### 调度优先级

1. `status` 升序（先处理未完成的博主）
2. `priority` 降序（优先级数字越大越优先）
3. `last_crawl_time` 升序（最久未刷新排前面）

### 状态流转

- `status = 0`：待处理
- `status = 1`：抓取中
- `status = 2`：全量已完成，后续进入增量刷新模式

### .env 变量说明

| 变量名 | 是否必填 | 说明 |
|--------|---------|------|
| `POSTGRES_HOST` | ✅ 必填 | PostgreSQL 服务器地址 |
| `POSTGRES_PORT` | ✅ 必填 | PostgreSQL 服务器端口 |
| `POSTGRES_USER` | ✅ 必填 | 数据库用户 |
| `POSTGRES_PASSWORD` | ✅ 必填 | 数据库用户密码 |
| `POSTGRES_DB` | ✅ 必填 | 数据库名称 |
| `XUEQIU_USER_DATA_DIR` | 可选 | 浏览器数据目录（默认 `./xueqiu_user_data`） |
| `CRAWL_RUN_MINUTES` | 可选 | 单次任务运行时长（分钟） |
| `BROWSER_HEADLESS` | 可选 | 是否无头模式运行 |
| `QUIET_START` / `QUIET_END` | 可选 | 静默期时间段 |
| `FEISHU_WEBHOOK` | 可选 | 飞书机器人通知 |