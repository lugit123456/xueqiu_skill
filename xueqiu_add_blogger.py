"""
xueqiu_add_blogger.py
=====================

为雪球舆情采集 skill 提供"AI 友好的博主添加"能力。

当用户在对话中提供 (a) 雪球链接（博主主页或帖子详情） 或 (b) 博主昵称关键词时，
agent 调用本模块完成：

1. URL 解析 或 搜索 API 调用
2. 写入 xueqiu_blogger_tasks 抓取表
3. 立即对该博主启动一次 mini 抓取（复用 XueqiuSmartSkill.crawl_single_blogger）
4. 把抓取回执（含最近 5 条帖子预览）透传给 agent，agent 可立即回答"博主都说了什么"
5. 用户事后追问时，可调用 query_recent_posts() 直接查 DB 而不重新抓取

入口函数（按调用顺序）：

    add_blogger_from_user_input(user_text)
        └─ parse_xueqiu_url(user_text)
            └─ add_blogger_by_uid(uid, screen_name)
        └─ add_blogger_by_keyword(keyword)
            └─ search_user_in_browser(keyword)
            └─ add_blogger_by_uid(uid, screen_name)

CLI：
    python xueqiu_add_blogger.py --url "https://xueqiu.com/u/5992135535"
    python xueqiu_add_blogger.py --keyword "白酒"
    python xueqiu_add_blogger.py --uid 5992135535 --name "九航笔记"
"""

import os
import re
import sys
import json
import time
import logging
import argparse
import psycopg2
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from xueqiu_crawl_skill import XueqiuSmartSkill

# ────────────────────────────────────────────────────────────────────
# 配置与日志
# ────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOG_FILE = os.path.join(LOG_DIR, f"xueqiu_add_{time.strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [AddBlogger] - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("xueqiu_add_blogger")

# 雪球博主 URL 匹配：
#   https://xueqiu.com/u/5992135535
#   https://xueqiu.com/5992135535/389370514
#   https://xueqiu.com/5992135535/389370514?foo=bar#xx
URL_PATTERN = re.compile(r'xueqiu\.com/(?:u/)?(\d+)(?:/\d+)?')

# 搜索 API
SEARCH_USER_URL = "https://xueqiu.com/query/v1/search/user.json"
DEFAULT_SEARCH_COUNT = 10

# 回执中 recent_posts 的数量上限
RECENT_POSTS_LIMIT = 5

# 事后查询的默认 limit
DEFAULT_QUERY_LIMIT = 10

# 数据库表名（与 SKILL_SETUP.md 中建表 SQL 对齐）
TASKS_TABLE = "xueqiu_blogger_tasks"
POSTS_TABLE = "xueqiu_blogger_posts"


# ────────────────────────────────────────────────────────────────────
# 数据库工具
# ────────────────────────────────────────────────────────────────────

def _load_db_config():
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "user": os.getenv("POSTGRES_USER", "hub_user"),
        "password": os.getenv("POSTGRES_PASSWORD", "hub_password"),
        "database": os.getenv("POSTGRES_DB", "financial_hub"),
    }


def _get_user_data_dir():
    return os.path.join(BASE_DIR, os.getenv("XUEQIU_USER_DATA_DIR", "xueqiu_user_data"))


def upsert_blogger(uid: int, screen_name: str) -> dict:
    """
    写入或更新博主任务表。

    复用 hub_xueqiu_adapter.py:57-63 的 ON CONFLICT 写法：
    - 首次添加：status=0 / priority=5 / checkpoint_page=0 / last_crawl_time=NOW()
    - 已存在：仅刷新 screen_name，**不重置**断点 / 状态 / 上次抓取时间
    """
    cfg = _load_db_config()
    conn = psycopg2.connect(**cfg)
    is_new = False
    try:
        with conn.cursor() as cur:
            # 先检查是否已存在，用于回执展示
            cur.execute(f"SELECT 1 FROM {TASKS_TABLE} WHERE user_id = %s", (int(uid),))
            is_new = cur.fetchone() is None

            sql = f"""
                INSERT INTO {TASKS_TABLE}
                    (user_id, screen_name, status, priority,
                     checkpoint_page, last_crawl_time, total_posts_count)
                VALUES (%s, %s, 0, 5, 0, NOW(), 0)
                ON CONFLICT (user_id) DO UPDATE
                SET screen_name = EXCLUDED.screen_name;
            """
            cur.execute(sql, (int(uid), screen_name))
        conn.commit()
        logger.info(
            f"✅ 博主 {screen_name}(UID={uid}) {'新增' if is_new else '已存在，仅刷新昵称'}")
        return {"uid": int(uid), "screen_name": screen_name, "is_new": is_new}
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ 写入博主失败: {e}")
        raise
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# 触发抓取
# ────────────────────────────────────────────────────────────────────

def trigger_single_crawl(uid: int, screen_name: str, max_pages: int = 3) -> dict:
    """
    立即对刚添加的博主启动一次 mini 抓取。
    返回 {"new_count", "fetched_pages", "recent_posts", "diagnosis"}（dict）。
    兼容旧版返回 int 的情况，回退到 {"new_count": N, "fetched_pages": 0, "recent_posts": [], "diagnosis": {}}。
    抓取抛异常时返回 {"new_count": -1, "diagnosis": {}, "error": str(e)}。
    """
    logger.info(f"🚀 立即启动 mini 抓取: {screen_name}(UID={uid})")
    try:
        skill = XueqiuSmartSkill()
        result = skill.crawl_single_blogger(
            uid=uid, screen_name=screen_name, max_pages=max_pages)
        if isinstance(result, dict):
            return result
        # 旧版 int 返回的兼容路径
        return {
            "new_count": int(result) if result else 0,
            "fetched_pages": 0,
            "recent_posts": [],
            "diagnosis": {},
        }
    except Exception as e:
        logger.error(f"❌ mini 抓取失败: {e}")
        return {
            "new_count": -1,
            "fetched_pages": 0,
            "recent_posts": [],
            "diagnosis": {},
            "error": str(e),
        }


# ────────────────────────────────────────────────────────────────────
# 事后查询（不重新抓取，直接查 xueqiu_blogger_posts）
# ────────────────────────────────────────────────────────────────────

def _truncate_preview(text: str, limit: int = 200) -> str:
    """折叠空白并截断到 limit 字符。"""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def query_recent_posts(uid: int, limit: int = DEFAULT_QUERY_LIMIT,
                       since=None) -> list:
    """
    从 xueqiu_blogger_posts 表直接读取博主最近的 N 条帖子。
    用于"事后追问"场景，不触发新抓取。

    参数:
        uid:   博主 UID
        limit: 最多返回多少条，默认 10
        since: datetime 或 None；只返回 comment_time >= since 的帖子

    返回 list[dict]，每条包含：
        id, content_preview, comment_time, stock_codes, stock_names, ip_location
    按 comment_time DESC 排序。
    """
    cfg = _load_db_config()
    sql = f"""
        SELECT id, content, comment_time, stock_codes, stock_names, ip_location
        FROM {POSTS_TABLE}
        WHERE user_id = %s
    """
    params = [int(uid)]

    if since is not None:
        sql += " AND comment_time >= %s"
        params.append(since)

    sql += " ORDER BY comment_time DESC LIMIT %s"
    params.append(int(limit))

    conn = psycopg2.connect(**cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        logger.info(
            f"📊 [Query] UID={uid} limit={limit} since={since} → {len(rows)} 条")
        results = []
        for r in rows:
            post_id, content, ctime, codes, names, ip_loc = r
            results.append({
                "id": post_id,
                "content_preview": _truncate_preview(content or ""),
                "comment_time": ctime.strftime('%Y-%m-%d %H:%M:%S') if ctime else "",
                "stock_codes": codes or "",
                "stock_names": names or "",
                "ip_location": ip_loc,
            })
        return results
    except Exception as e:
        logger.error(f"❌ query_recent_posts 失败: {e}")
        raise
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# 模式 1：URL 解析
# ────────────────────────────────────────────────────────────────────

def parse_xueqiu_url(text: str):
    """
    从任意用户文本中提取雪球博主 UID。
    命中返回 (uid_int, matched_url) ；未命中返回 (None, None)。
    """
    m = URL_PATTERN.search(text)
    if not m:
        return None, None
    uid = int(m.group(1))
    return uid, m.group(0)


# ────────────────────────────────────────────────────────────────────
# 模式 2：搜索 API
# ────────────────────────────────────────────────────────────────────

def _search_user_script():
    """
    在浏览器内 fetch 雪球搜索 API。
    复用现有 xueqiu_user_data 登录态，避免硬编码 Cookie / 触发 md5__1038 反爬签名。
    """
    return """
    async (args) => {
        const [q, c] = args;
        try {
            const url = `/query/v1/search/user.json?q=${encodeURIComponent(q)}&count=${c}`;
            const r = await fetch(url, { credentials: 'include' });
            if (r.status !== 200) return null;
            return await r.json();
        } catch (e) {
            return null;
        }
    }
    """


def search_user_in_browser(keyword: str, count: int = DEFAULT_SEARCH_COUNT) -> list:
    """
    在已登录的 Playwright 上下文内调用搜索 API。
    返回 list[dict]（可能为空）。失败返回 []。
    """
    user_data_dir = _get_user_data_dir()
    is_headless = os.getenv("BROWSER_HEADLESS", "True").lower() == "true"

    logger.info(f"🔍 [Search] 关键词: {keyword!r} | count: {count} | headless: {is_headless}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=is_headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            page = context.new_page()
            # 引导到一个同源页面，避免 fetch 出现 CORS / 上下文缺失
            try:
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass

            data = page.evaluate(_search_user_script(), [keyword, count])
            if not data:
                logger.warning(f"⚠️ [Search] 接口返回空，可能未登录或被风控。")
                return []

            users = data.get("list", []) or []
            logger.info(f"📊 [Search] 共返回 {len(users)} 条结果（接口 count={data.get('count')}）")
            return users
        finally:
            context.close()


# ────────────────────────────────────────────────────────────────────
# 业务编排
# ────────────────────────────────────────────────────────────────────

def add_blogger_by_uid(uid: int, screen_name: str, auto_crawl: bool = True) -> dict:
    """
    已知 UID + 昵称 → 写表 +（可选）抓取。
    返回回执 dict，agent 直接基于此回执生成自然语言。
    """
    record = upsert_blogger(uid, screen_name)
    record["crawl_result"] = None
    record["recent_posts"] = []
    record["diagnosis"] = {}

    if not auto_crawl:
        record["crawl_result"] = "skipped"
        return record

    crawl = trigger_single_crawl(uid, screen_name)
    record["crawl_result"] = crawl.get("new_count", 0)
    record["fetched_pages"] = crawl.get("fetched_pages", 0)
    record["recent_posts"] = crawl.get("recent_posts", [])
    record["diagnosis"] = crawl.get("diagnosis", {})
    if crawl.get("error"):
        record["error"] = crawl["error"]
    return record


def _format_recent_preview_line(recent_posts: list) -> str:
    """把最近一条帖子格式化为单行中文摘要，附加到 message 末尾。"""
    if not recent_posts:
        return ""
    latest = recent_posts[0]
    preview = latest.get("content_preview", "")
    ctime = latest.get("comment_time", "")
    codes = latest.get("stock_codes", "")
    suffix = f" | 股票: {codes}" if codes else ""
    return f"\n📝 最近一条（{ctime}）{suffix}\n   {preview}"


def _format_diagnosis_line(diagnosis: dict) -> str:
    """把抓取诊断信息格式化为单行中文摘要，附加到 message 末尾。
    只在 diagnosis 非空（抓取失败）时返回非空字符串。
    登录态失效时优先输出明确指引（让 agent 知道让用户重登录）。"""
    if not diagnosis:
        return ""
    # 优先：登录态失效 → 明确指引
    if diagnosis.get("need_user_login"):
        cmd = diagnosis.get("relogin_command",
                            "python3 xueqiu_random_trigger.py --debug")
        return (
            f"\n🔐 检测到登录态失效。请运行：\n"
            f"   `{cmd}`\n"
            f"   重新登录后，**重新发送刚才的链接/关键词**给我。"
        )
    # 普通诊断行
    status = diagnosis.get("fetch_status", "unknown")
    body = diagnosis.get("fetch_body_preview", "")[:80]
    err = diagnosis.get("fetch_error", "")
    cookies = diagnosis.get("cookies", [])
    cookie_names = [c["name"] for c in cookies if "name" in c]

    parts = [f"🩺 诊断: HTTP {status}"]
    if body:
        parts.append(f"body: {body}")
    if err:
        parts.append(f"err: {err}")
    if cookie_names:
        parts.append(f"cookies: {','.join(cookie_names)}")
    return "\n" + " | ".join(parts)


def format_candidate_line(idx: int, u: dict) -> str:
    """把搜索结果格式化为单行展示。"""
    uid = u.get("id", "?")
    name = u.get("screen_name", "?")
    followers = u.get("followers_count", 0)
    posts = u.get("status_count", 0)
    desc = (u.get("description") or "").strip().replace("\n", " ")
    if desc and len(desc) > 60:
        desc = desc[:60] + "..."

    verified = ""
    if u.get("verified"):
        verified = " ✓认证"
        if u.get("verified_description"):
            verified += f"({u.get('verified_description')})"

    return (
        f"{idx}. {name} (UID: {uid}) — 粉丝 {followers:,} | 帖子 {posts:,}{verified}\n"
        f"   简介: {desc or '（无）'}"
    )


def add_blogger_by_keyword(keyword: str, auto_crawl_single: bool = True) -> dict:
    """
    关键词搜索 → 单/多结果分流。
    - 0 个：返回提示
    - 1 个：直接写表 + 抓取
    - >1 个：返回候选列表，**不写表**，等 agent 把列表展示给用户并再次调用 add_blogger_by_uid

    返回 dict，agent 根据 need_user_choice 决定下一步动作。
    """
    users = search_user_in_browser(keyword, count=DEFAULT_SEARCH_COUNT)

    if not users:
        return {
            "ok": False,
            "count": 0,
            "keyword": keyword,
            "message": (
                f"未找到匹配『{keyword}』的博主。"
                "请提供完整昵称，或直接发送雪球博主链接。"
            ),
        }

    if len(users) == 1:
        u = users[0]
        uid, name = int(u["id"]), u.get("screen_name", str(u["id"]))
        record = add_blogger_by_uid(uid, name, auto_crawl=auto_crawl_single)
        new_count = record.get("crawl_result", 0)
        recent_posts = record.get("recent_posts", [])
        diagnosis = record.get("diagnosis", {})
        preview_line = _format_recent_preview_line(recent_posts)
        diag_line = _format_diagnosis_line(diagnosis)
        return {
            "ok": True,
            "count": 1,
            "auto_added": True,
            "uid": uid,
            "screen_name": name,
            "crawl_result": new_count,
            "recent_posts": recent_posts,
            "diagnosis": diagnosis,
            "message": (
                f"✅ 找到唯一匹配的博主『{name}』(UID={uid})，"
                f"已加入抓取表并完成 mini 抓取，新增 {new_count} 条动态。"
                f"{preview_line}{diag_line}"
            ),
        }

    # 多结果：不写表，让用户选
    lines = [f"找到 {len(users)} 个匹配『{keyword}』的博主，请选择要抓取哪一个：\n"]
    for i, u in enumerate(users, 1):
        lines.append(format_candidate_line(i, u))
    lines.append("\n请回复编号 1 / 2 / ... 即可。")

    return {
        "ok": True,
        "count": len(users),
        "auto_added": False,
        "need_user_choice": True,
        "candidates": users,
        "message": "\n".join(lines),
    }


def add_blogger_from_user_input(user_text: str, auto_crawl: bool = True) -> dict:
    """
    Agent 调用的总入口。
    先尝试从文本中识别 URL；识别不到则视为搜索关键词。
    """
    uid, matched = parse_xueqiu_url(user_text or "")
    if uid is not None:
        logger.info(f"🔗 识别到雪球链接: {matched} → UID={uid}")
        record = add_blogger_by_uid(uid, screen_name=f"UID:{uid}", auto_crawl=auto_crawl)
        new_count = record.get("crawl_result", 0)
        recent_posts = record.get("recent_posts", [])
        diagnosis = record.get("diagnosis", {})
        preview_line = _format_recent_preview_line(recent_posts)
        diag_line = _format_diagnosis_line(diagnosis)
        return {
            "ok": True,
            "mode": "url",
            "matched_url": matched,
            "uid": uid,
            "screen_name": record["screen_name"],
            "is_new": record["is_new"],
            "crawl_result": new_count,
            "recent_posts": recent_posts,
            "diagnosis": diagnosis,
            "message": (
                f"✅ 识别到博主链接 UID={uid}，"
                f"{'新增' if record['is_new'] else '已存在'}并完成 mini 抓取，"
                f"新增 {new_count} 条动态。"
                f"{preview_line}{diag_line}"
            ),
        }

    # 不是 URL → 当成搜索关键词
    logger.info(f"🔍 未识别到链接，按关键词搜索: {user_text!r}")
    result = add_blogger_by_keyword(user_text.strip(), auto_crawl_single=auto_crawl)
    result["mode"] = "keyword"
    return result


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description="雪球博主自动添加工具（URL / 关键词 / 事后查询）")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--url", help='雪球链接，如 "https://xueqiu.com/u/5992135535"')
    grp.add_argument("--keyword", help='博主昵称关键词，如 "白酒"')
    grp.add_argument("--uid", type=int, help="直接指定 UID（与 --name 一起使用）")
    grp.add_argument("--query-uid", type=int,
                     help="事后查询某博主最近的帖子（不抓取，直接查 DB）")
    parser.add_argument("--name", help="博主昵称（与 --uid 一起使用）")
    parser.add_argument("--no-crawl", action="store_true",
                        help="只写表，不立即抓取（适合静默期或调试）")
    parser.add_argument("--query-limit", type=int, default=DEFAULT_QUERY_LIMIT,
                        help="事后查询返回的最大条数，默认 10")
    parser.add_argument("--query-since-days", type=int, default=None,
                        help="事后查询的起始天数（仅看近 N 天），如 7")
    args = parser.parse_args()

    if args.uid is not None and not args.name:
        parser.error("--uid 必须配合 --name 使用")

    # 模式 1：事后查询
    if args.query_uid is not None:
        from datetime import datetime, timedelta
        since = None
        if args.query_since_days:
            since = datetime.now() - timedelta(days=args.query_since_days)
        posts = query_recent_posts(
            uid=args.query_uid, limit=args.query_limit, since=since)
        print(f"📊 UID={args.query_uid} 最近 {len(posts)} 条帖子：\n")
        for i, p in enumerate(posts, 1):
            codes = f" | 股票: {p['stock_codes']}" if p['stock_codes'] else ""
            ip = f" | IP: {p['ip_location']}" if p.get('ip_location') else ""
            print(f"{i}. [{p['comment_time']}]{codes}{ip}")
            print(f"   {p['content_preview']}\n")
        return

    auto_crawl = not args.no_crawl

    if args.url:
        uid, matched = parse_xueqiu_url(args.url)
        if not uid:
            print(f"❌ 无法从 URL 中解析出 UID: {args.url}")
            sys.exit(1)
        record = add_blogger_by_uid(uid, screen_name=args.name or f"UID:{uid}",
                                    auto_crawl=auto_crawl)
        print(json.dumps(record, ensure_ascii=False, indent=2))
    elif args.keyword:
        result = add_blogger_by_keyword(args.keyword, auto_crawl_single=auto_crawl)
        print(result["message"])
        if not result.get("auto_added"):
            print("\n(JSON 候选列表 ↓)")
            print(json.dumps(result.get("candidates", []),
                             ensure_ascii=False, indent=2))
    else:
        record = add_blogger_by_uid(args.uid, args.name, auto_crawl=auto_crawl)
        print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
