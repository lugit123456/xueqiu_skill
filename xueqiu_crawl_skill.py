import os
import re
import json
import time
import random
import logging
import psycopg2
from psycopg2.extras import DictCursor
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)


def _cleanup_old_logs(retention_days=7):
    try:
        now = time.time()
        for filename in os.listdir(LOG_DIR):
            if filename.endswith(".log"):
                file_path = os.path.join(LOG_DIR, filename)
                if os.path.getmtime(file_path) < now - (retention_days * 86400):
                    os.remove(file_path)
    except:
        pass


_cleanup_old_logs(retention_days=7)

log_filename = os.path.join(LOG_DIR, f"xueqiu_{datetime.now().strftime('%Y%m%d')}.log")
logger = logging.getLogger()
logger.setLevel(logging.INFO)

if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    fh = logging.FileHandler(log_filename, encoding='utf-8')
    fh.setFormatter(formatter)
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)


class XueqiuSmartSkill:
    def __init__(self):
        self.db_config = {
            "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "port": int(os.getenv("POSTGRES_PORT", 5432)),
            "user": os.getenv("POSTGRES_USER", "hub_user"),
            "password": os.getenv("POSTGRES_PASSWORD", "hub_password"),
            "database": os.getenv("POSTGRES_DB", "financial_hub")
        }
        self.user_data_dir = os.path.join(os.path.dirname(__file__),
                                          os.getenv("XUEQIU_USER_DATA_DIR", "xueqiu_user_data"))
        self.feishu_webhook = os.getenv("FEISHU_WEBHOOK", "")
        self.processed_uids = set()

    def _get_db_conn(self):
        return psycopg2.connect(**self.db_config)

    def _get_next_task(self):
        conn = self._get_db_conn()
        exclude_sql = ""
        if self.processed_uids:
            uids_str = ",".join([str(int(i)) for i in self.processed_uids])
            exclude_sql = f"AND user_id NOT IN ({uids_str})"

        sql = f"""
            SELECT * FROM xueqiu_blogger_tasks 
            WHERE 1=1 {exclude_sql}
            ORDER BY status ASC, priority DESC, last_crawl_time ASC NULLS FIRST
            LIMIT 1
        """
        try:
            with conn.cursor(cursor_factory=DictCursor) as cur:
                cur.execute(sql)
                return cur.fetchone()
        finally:
            conn.close()

    def _check_remaining_tasks(self):
        conn = self._get_db_conn()
        uids_str = ",".join([str(int(i)) for i in self.processed_uids]) if self.processed_uids else "0"
        sql = f"SELECT COUNT(*) FROM xueqiu_blogger_tasks WHERE user_id NOT IN ({uids_str})"
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                res = cur.fetchone()
                return res[0] > 0 if res else False
        finally:
            conn.close()

    def _update_checkpoint(self, uid, page):
        conn = self._get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE xueqiu_blogger_tasks SET checkpoint_page=%s WHERE user_id=%s",
                            (int(page), int(uid)))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _mark_task_status(self, uid, status, page):
        conn = self._get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE xueqiu_blogger_tasks SET status=%s, checkpoint_page=%s, last_crawl_time=NOW() WHERE user_id=%s",
                    (int(status), int(page), int(uid)))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _update_last_time(self, uid):
        conn = self._get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE xueqiu_blogger_tasks SET last_crawl_time=NOW() WHERE user_id=%s", (int(uid),))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _sync_total_count(self, uid):
        conn = self._get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM xueqiu_blogger_posts WHERE user_id = %s", (int(uid),))
                actual_count = cur.fetchone()[0]
                cur.execute("UPDATE xueqiu_blogger_tasks SET total_posts_count = %s WHERE user_id = %s",
                            (int(actual_count), int(uid)))
                logging.info(f"📊 数据对齐：博主 {uid} 库内当前总计 {actual_count} 条动态")
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _send_feishu_alert(self, title, msg, screenshot_path=None):
        if not self.feishu_webhook: return
        content = [[{"tag": "text", "text": msg}]]
        if screenshot_path:
            content.append([{"tag": "text", "text": f"\n📸 错误截图存放在: {screenshot_path}"}])
        payload = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": f"🚨 {title}", "content": content}}}}
        try:
            requests.post(self.feishu_webhook, json=payload, timeout=10)
        except Exception as e:
            logging.error(f"❌ 飞书告警发送失败: {e}")

    def _fetch_inside_page(self, page, uid, p_num):
        """
        在浏览器内 fetch 雪球动态时间线 API。
        返回结构化 dict，便于调用方定位失败原因：
            成功：       {"ok": True, "data": <statuses array>}
            HTTP 非 200：{"ok": False, "status": <int>, "body_preview": <str 前 200 字>}
            JS 异常：   {"ok": False, "status": -1, "error": <str>}
            Python 异常：{"ok": False, "status": -1, "error": "page.evaluate 异常: ..."}
        """
        script = """
        async (args) => {
            const [u, p] = args;
            try {
                const r = await fetch(`/v4/statuses/user_timeline.json?page=${p}&user_id=${u}`);
                if (r.status === 200) {
                    return { ok: true, data: await r.json() };
                }
                let body = '';
                try { body = (await r.text()).slice(0, 200); } catch(e) {}
                return { ok: false, status: r.status, body_preview: body };
            } catch (e) {
                return { ok: false, status: -1, error: String(e) };
            }
        }
        """
        try:
            result = page.evaluate(script, [str(uid), p_num])
            if not isinstance(result, dict):
                return {"ok": False, "status": -1, "error": "evaluate 返回非 dict"}
            return result
        except Exception as e:
            return {"ok": False, "status": -1, "error": f"page.evaluate 异常: {e}"}

    def _fetch_long_post(self, page, uid, status_id):
        script = """
        async (args) => {
            const [u, sid] = args;
            try {
                const r = await fetch(`/${u}/${sid}`);
                if (r.status === 200) {
                    return await r.text();
                }
            } catch(e) {}
            return null;
        }
        """
        try:
            html = page.evaluate(script, [str(uid), str(status_id)])
            if html:
                match = re.search(r'window\.SNOWMAN_STATUS\s*=\s*(\{.*?\});\s*window\.SNOWMAN_TARGET', html, re.DOTALL)
                if match:
                    data = json.loads(match.group(1))
                    return data.get('text', '')
        except Exception as e:
            logging.error(f"❌ 抓取长文 {status_id} 详情失败: {e}")
        return ""

    def _save_data(self, statuses):
        conn = self._get_db_conn()
        new_count = 0
        try:
            with conn.cursor() as cur:
                for s in statuses:
                    sql = """INSERT INTO xueqiu_blogger_posts 
                             (id, user_id, screen_name, content, stock_codes, stock_names, comment_time, raw_json) 
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (id) DO NOTHING"""

                    clean_content = re.sub(r'<[^>]+>', '', s.get('text', '')).strip()
                    title = s.get('title', '')
                    if title and title not in clean_content:
                        clean_content = f"【{title}】\n{clean_content}"

                    if not clean_content:
                        clean_content = s.get('description', '')

                    codes = ",".join(s.get('stockCorrelation', []))
                    names = ",".join(re.findall(r'\$([^$()]+)\((?:SH|SZ|HK)?\d{5,6}\)\$', s.get('text', '')))
                    time_str = datetime.fromtimestamp(s['created_at'] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                    raw_json_str = json.dumps(s, ensure_ascii=False)

                    cur.execute(sql, (
                        int(s['id']),
                        int(s['user']['id']),
                        s['user']['screen_name'],
                        clean_content,
                        codes,
                        names,
                        time_str,
                        raw_json_str
                    ))
                    new_count += cur.rowcount
            conn.commit()
            return new_count
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def crawl_single_blogger(self, uid, screen_name, max_pages=3, page_wait=(8, 14),
                             recent_posts_limit=5):
        """
        单博主 mini 抓取：只针对一个博主跑 1~N 页，追到最新动态。
        由 xueqiu_add_blogger.py 在用户主动添加博主时调用，避免触发 execute() 扫全表。

        返回值（dict）:
            {
                "new_count": int,          # 本轮新插入数据库的条数
                "fetched_pages": int,      # 实际翻过的页数
                "recent_posts": [           # 本次抓到的帖子（去重，最多 N 条）
                    {
                        "id": int,
                        "content_preview": str,   # 清洗后正文前 200 字
                        "comment_time": str,      # 'YYYY-MM-DD HH:MM:SS'
                        "stock_codes": str,
                        "stock_names": str,
                    },
                    ...
                ],
                "diagnosis": {               # 抓取失败时附带诊断信息；成功时为 {}
                    "fetch_status": int|str,
                    "fetch_body_preview": str,
                    "fetch_error": str,
                    "cookies": [
                        {"name": str, "value_prefix": str, "expires": int},
                        ...
                    ]
                }
            }
        失败时返回字符串（与 execute() 错误风格一致）。
        """
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if not os.path.exists(env_path):
            return f"⚠️ 缺少配置文件：请确保在 {env_path} 路径下存在 .env 文件。"

        load_dotenv(env_path)
        is_headless = os.getenv("BROWSER_HEADLESS", "True").lower() == "true"

        logging.info(
            f"🎯 [SingleBlogger] 启动 mini 抓取: {screen_name}(UID={uid}) | "
            f"最多 {max_pages} 页 | 无头: {is_headless}")

        result = {
            "new_count": 0,
            "fetched_pages": 0,
            "recent_posts": [],
            "diagnosis": {},
        }
        seen_ids = set()
        collected_statuses = []   # 临时缓存本轮抓到的 statuses（去重后用于 recent_posts）

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=is_headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
            page = context.new_page()

            if is_headless and not os.path.exists(os.path.join(self.user_data_dir, "Default")):
                context.close()
                return "🚨 【环境未初始化】检测到你从未登录过。请对我说：『开启调试模式运行雪球采集』，在窗口中完成登录。"

            try:
                try:
                    page.goto(f"https://xueqiu.com/u/{uid}", wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    pass

                total_new = 0
                for curr_page in range(1, max_pages + 1):
                    fetch_res = self._fetch_inside_page(page, uid, curr_page)
                    if not isinstance(fetch_res, dict) or not fetch_res.get("ok"):
                        result["diagnosis"]["fetch_status"] = (fetch_res or {}).get("status", "unknown")
                        result["diagnosis"]["fetch_body_preview"] = (fetch_res or {}).get("body_preview", "")
                        result["diagnosis"]["fetch_error"] = (fetch_res or {}).get("error", "")

                        # 登录态失效检测：雪球 error_code 10022 / 10021 等常见文案
                        body_preview = result["diagnosis"]["fetch_body_preview"] or ""
                        if ("请登录" in body_preview
                                or "未登录" in body_preview
                                or "token" in body_preview.lower()):
                            result["diagnosis"]["need_user_login"] = True
                            result["diagnosis"]["relogin_command"] = (
                                "python3 xueqiu_random_trigger.py --debug"
                            )

                        logging.warning(
                            f"⚠️ [SingleBlogger] {screen_name} 第 {curr_page} 页 fetch 失败 | "
                            f"status={result['diagnosis']['fetch_status']} | "
                            f"err={result['diagnosis']['fetch_error']} | "
                            f"body={result['diagnosis']['fetch_body_preview'][:120]}")
                        break

                    data = fetch_res.get("data") or {}
                    statuses = data.get("statuses", [])
                    if not statuses:
                        logging.info(f"📭 [SingleBlogger] {screen_name} 第 {curr_page} 页已无内容，抓取结束。")
                        break

                    # 长文抓取逻辑与 execute() 保持一致
                    for s in statuses:
                        if str(s.get("type")) == "3" or (s.get("title") and not s.get("text")):
                            logging.info(f"🔎 发现长文《{s.get('title')}》，正在向下抓取详情...")
                            long_text = self._fetch_long_post(page, uid, s['id'])
                            if long_text:
                                long_text = re.sub(r'</?(p|br|div)[^>]*>', '\n', long_text, flags=re.IGNORECASE)
                                clean_text = re.sub(r'<[^>]+>', '', long_text)
                                s['text'] = re.sub(r'\n{2,}', '\n', clean_text).strip()
                            else:
                                s['text'] = s.get('description', '')
                            time.sleep(random.uniform(1.0, 2.5))

                    # 收集本轮 statuses（去重），用于构造 recent_posts
                    for s in statuses:
                        sid = s.get("id")
                        if sid is None or sid in seen_ids:
                            continue
                        seen_ids.add(sid)
                        collected_statuses.append(s)

                    new_count = self._save_data(statuses)
                    total_new += new_count
                    logging.info(
                        f"📑 [SingleBlogger] {screen_name} 第 {curr_page} 页："
                        f"获取 {len(statuses)} 条，入库 {new_count} 条。")

                    # 哨兵：增量模式下首页无新数据即停
                    if curr_page == 1 and new_count == 0:
                        logging.info(f"🛡️ [SingleBlogger] {screen_name} 首页无新增动态，无需继续翻页。")
                        break

                    if curr_page < max_pages:
                        wait_time = random.uniform(*page_wait)
                        time.sleep(wait_time)
            finally:
                # 在 context 关闭前取 cookie 诊断信息
                try:
                    all_cookies = context.cookies()      # 不带 url 参数，客户端按 domain 过滤
                    key_names = {"xq_a_token", "xqat", "xq_r_token", "u", "s", "xq_id_token"}
                    result["diagnosis"]["cookies"] = [
                        {
                            "name": c["name"],
                            "value_prefix": c["value"][:30],
                            "expires": c.get("expires", -1),
                        }
                        for c in all_cookies
                        if c["name"] in key_names
                        and "xueqiu.com" in c.get("domain", "")
                    ]
                except Exception as e:
                    result["diagnosis"]["cookies_error"] = str(e)
                context.close()

            self._mark_task_status(uid, 2, 0)
            self._update_last_time(uid)
            self._sync_total_count(uid)

            # 构造 recent_posts：按 created_at 倒序，截前 N 条
            collected_statuses.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            for s in collected_statuses[:recent_posts_limit]:
                text = (s.get("text") or "").strip()
                if not text:
                    text = s.get("description") or ""
                text = re.sub(r'\s+', ' ', text)  # 折叠空白
                if len(text) > 200:
                    text = text[:200] + "…"

                codes = ",".join(s.get("stockCorrelation", []) or [])
                names = ",".join(re.findall(r'\$([^$()]+)\((?:SH|SZ|HK)?\d{5,6}\)\$', s.get("text", "") or ""))
                try:
                    comment_time = datetime.fromtimestamp(
                        s["created_at"] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    comment_time = ""

                result["recent_posts"].append({
                    "id": s.get("id"),
                    "content_preview": text,
                    "comment_time": comment_time,
                    "stock_codes": codes,
                    "stock_names": names,
                })

            result["new_count"] = total_new
            result["fetched_pages"] = curr_page  # 实际跑过的最大页码
            logging.info(
                f"🏁 [SingleBlogger] {screen_name} 抓取完成，本轮共入库 {total_new} 条新动态，"
                f"回执含 {len(result['recent_posts'])} 条 recent_posts。")
            return result

    def execute(self, run_minutes=None, debug_mode=False):
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if not os.path.exists(env_path):
            return f"⚠️ 缺少配置文件：请确保在 {env_path} 路径下存在 .env 文件。"

        load_dotenv(env_path)
        is_headless = False if debug_mode else os.getenv("BROWSER_HEADLESS", "True").lower() == "true"
        run_minutes = run_minutes or int(os.getenv("CRAWL_RUN_MINUTES", 40))
        deadline = datetime.now() + timedelta(minutes=run_minutes)

        logging.info(f"🚀 核心 Skill 启动 | 限时: {run_minutes}min | 无头模式: {is_headless}")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=is_headless,
                args=["--disable-blink-features=AutomationControlled"]
            )
            page = context.new_page()

            if is_headless and not os.path.exists(os.path.join(self.user_data_dir, "Default")):
                context.close()
                return "🚨 【环境未初始化】检测到你从未登录过。请对我说：『开启调试模式运行雪球采集』，在窗口中完成登录。"

            while True:
                if datetime.now() >= deadline:
                    logging.info("⏰ 达到任务限时，安全关闭。")
                    break

                task = self._get_next_task()
                if not task:
                    if len(self.processed_uids) == 0:
                        context.close()
                        return "📭 【任务表为空】请在 `xueqiu_blogger_tasks` 表中添加博主 UID 后再试。"
                    logging.info("🏁 本轮已无可处理的博主，任务圆满结束。")
                    break

                uid, name = task['user_id'], task['screen_name']
                is_incremental = (task['status'] >= 2)
                curr_page = (task['checkpoint_page'] + 1) if not is_incremental else 1

                logging.info(
                    f"🎯 切换博主 -> {name} | {'[增量刷新]' if is_incremental else '[全量同步]'} | 起始页: {curr_page}")

                try:
                    page.goto(f"https://xueqiu.com/u/{uid}", wait_until="domcontentloaded", timeout=25000)
                except:
                    pass

                consecutive_fail_count = 0
                while True:
                    data = self._fetch_inside_page(page, uid, curr_page)

                    if data is None:
                        consecutive_fail_count += 1
                        if consecutive_fail_count >= 3:
                            shot_name = os.path.join(LOG_DIR, f"err_{uid}_{int(time.time())}.png")
                            page.screenshot(path=shot_name)
                            self._send_feishu_alert("采集拦截", f"博主 {name} 连续3次请求失败，可能需要手动滑块验证。",
                                                    shot_name)
                            break
                        time.sleep(20)
                        continue

                    consecutive_fail_count = 0
                    statuses = data.get("statuses", [])

                    if len(statuses) == 0:
                        if not is_incremental: self._mark_task_status(uid, 2, 0)
                        break

                    for s in statuses:
                        if str(s.get("type")) == "3" or (s.get("title") and not s.get("text")):
                            logging.info(f"🔎 发现长文《{s.get('title')}》，正在向下抓取详情...")
                            long_text = self._fetch_long_post(page, uid, s['id'])
                            if long_text:
                                long_text = re.sub(r'</?(p|br|div)[^>]*>', '\n', long_text, flags=re.IGNORECASE)
                                clean_text = re.sub(r'<[^>]+>', '', long_text)
                                s['text'] = re.sub(r'\n{2,}', '\n', clean_text).strip()
                            else:
                                s['text'] = s.get('description', '')
                            time.sleep(random.uniform(1.0, 2.5))

                    new_count = self._save_data(statuses)

                    if is_incremental and new_count == 0:
                        logging.info(f"🛡️ 哨兵拦截：{name} 已追上最新动态，无需翻页。")
                        break

                    if not is_incremental: self._update_checkpoint(uid, curr_page)

                    if datetime.now() >= deadline:
                        logging.warning(f"⏰ 时限临近！保存博主 {name} 进度中...")
                        break

                    wait_time = random.uniform(8, 14)
                    logging.info(f"📑 {name} 第 {curr_page} 页：获取 {len(statuses)} 条，入库 {new_count} 条。")
                    time.sleep(wait_time)
                    curr_page += 1

                self.processed_uids.add(uid)
                self._update_last_time(uid)
                self._sync_total_count(uid)

                if datetime.now() < deadline:
                    if self._check_remaining_tasks():
                        sleep_time = random.uniform(30, 60)
                        logging.info(f"⏸️ 任务切换间歇，随机休眠 {sleep_time:.1f}s...")
                        time.sleep(sleep_time)
                    else:
                        break
                else:
                    break

            context.close()
        return len(self.processed_uids)


if __name__ == "__main__":
    XueqiuSmartSkill().execute()