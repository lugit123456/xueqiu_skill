import os
import sys
import time
import logging
import traceback
import subprocess
import psycopg2
from psycopg2.extras import DictCursor
from financial_hub_postgres import FinancialHubClient
from dotenv import load_dotenv

load_dotenv()
# ── 1. 统一元配置与统一 PG 数据库配置 ──
COMPONENT_NAME = "xueqiu_crawler"
SOURCE_TYPE = "xueqiu"

# 统一复用你的 Hub 及雪球本地业务 PostgreSQL 配置
DB_CONFIG = {
    'host': os.getenv('POSTGRES_HOST', '127.0.0.1'),
    'port': int(os.getenv('POSTGRES_PORT', 5432)),
    'user': os.getenv('POSTGRES_USER', 'hub_user'),
    'password': os.getenv('POSTGRES_PASSWORD', 'hub_password'),
    'dbname': os.getenv('POSTGRES_DB', 'financial_hub')
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('hub_xueqiu_adapter')


def get_pg_connection():
    """获取统一的 PostgreSQL 连接"""
    return psycopg2.connect(**DB_CONFIG)


# --- 2. 核心同步与打卡逻辑 (严格对齐微信 Hub 功能) ---

def sync_hub_targets_to_local_bloggers(hub_targets):
    """
    步骤 A: 从 Hub 的公共 crawl_targets 表中捞出活跃的目标，
    增量 Copy 到雪球业务表 xueqiu_blogger_tasks 中。
    💡 增强：不仅增量写入，还能同步更新博主的启用状态与昵称。
    """
    logger.info("🔄 [数据同步] 正在检查并增量 Copy 抓取目标至雪球本地 xueqiu_blogger_tasks 表...")
    conn = get_pg_connection()
    cur = conn.cursor()
    try:
        # 1. 先将本地所有博主的启用状态重置（准备根据 Hub 状态进行同步）
        # 假设你的本地任务表可以扩展一个用于标记同步的辅助字段，或者我们通过在 Hub 启用的列表中进行上下架

        for target in hub_targets:
            # 使用点语法安全读取 FinancialHubClient 抛出的对象属性
            uid = int(target.target_identifier)
            name = target.target_name

            # 2. 使用 ON CONFLICT 语法：如果 user_id 存在，则仅更新名称（保护本地断点 checkpoint_page）
            # 如果本地无此博主，则初始化新增
            sql = """
                INSERT INTO xueqiu_blogger_tasks 
                (user_id, screen_name, status, priority, checkpoint_page, last_crawl_time, total_posts_count) 
                VALUES (%s, %s, 0, 5, 0, NOW(), 0)
                ON CONFLICT (user_id) DO UPDATE 
                SET screen_name = EXCLUDED.screen_name;
            """
            cur.execute(sql, (uid, name))

        conn.commit()
        logger.info("✅ [数据同步] 雪球目标池增量同步与状态对齐完毕。")
    except Exception as e:
        logger.error(f"❌ 同步至本地雪球业务表发生异常: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


def sync_local_results_back_to_hub(hub_targets, is_process_success, global_error_msg):
    """
    步骤 B: 爬虫进程结束，读取雪球本地业务数据状态，
    同时回写更新 【雪球本地任务表】 与 【Hub 全局大表】。
    """
    logger.info("🔄 [数据同步] 正在将雪球运行战果同步回 Hub 控制台及本地统计表...")
    conn = get_pg_connection()
    cur = conn.cursor(cursor_factory=DictCursor)

    try:
        for target in hub_targets:
            hub_id = target.id
            uid = int(target.target_identifier)

            # 1. 盘点战果：从雪球内容表 xueqiu_blogger_posts 统计该博主真实成功的抓取总数
            cur.execute("SELECT COUNT(1) as actual_count FROM xueqiu_blogger_posts WHERE user_id = %s", (uid,))
            actual_count_row = cur.fetchone()
            actual_total = actual_count_row['actual_count'] if actual_count_row else 0

            # 2. 先原地更新【雪球本地任务表】的 total_posts_count 数量统计
            cur.execute("""
                UPDATE xueqiu_blogger_tasks 
                SET total_posts_count = %s 
                WHERE user_id = %s
            """, (actual_total, uid))

            # 3. 判定 Hub 回写状态（兼容雪球独特的策略性避让）
            if global_error_msg and ("避让" in global_error_msg or "静默期" in global_error_msg):
                target_status = 'idle'
                error_msg = global_error_msg.strip()
            elif not is_process_success:
                target_status = 'failed'
                error_msg = global_error_msg
            else:
                target_status = 'success'
                error_msg = None

            # 4. 原地更新【Hub 全局大表】控制台
            cur.execute("""
                UPDATE crawl_targets 
                SET last_crawl_at = NOW(),
                    last_crawl_status = %s,
                    last_error = %s,
                    total_items = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (target_status, error_msg, actual_total, hub_id))

        conn.commit()
        logger.info("✅ [数据同步] 雪球本地任务表与 Hub 全局状态大表回写对齐完成。")
    except Exception as e:
        logger.error(f"❌ 反向同步回 Hub 发生异常: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


# --- 3. 统一生命周期控制 ---

def main():
    hub_conn = get_pg_connection()
    try:
        # 创建公共封装的 Hub 客户端（内部自动调度组件状态、运行流水和事件）
        client = FinancialHubClient(hub_conn)

        logger.info(f"=== 🔍 正在从 Hub 扫描活跃的 '{SOURCE_TYPE}' 任务池 ===")
        # 1. 捞出 Hub 统一大表中所有启用的雪球抓取对象
        targets = client.get_crawl_targets(source_type=SOURCE_TYPE, enabled=True)

        if not targets:
            logger.info("ℹ️ 没有在 Hub 中找到开启的雪球抓取目标。安全退出。")
            return

        for t in targets:
            logger.info(f"  📍 [Hub ID: {t.id}] {t.target_name} -> UID: {t.target_identifier}")

        # 2. 【数据 Copy】增量合并并激活本地雪球任务池
        sync_hub_targets_to_local_bloggers(targets)

        # 3. ── Step 1: 组件向 Hub 打卡启动 (写入 crawl_runs & 更新 component_status) ──
        logger.info("[1/3] 整体监控上报: notify_crawl_start ...")
        primary_target_id = targets[0].id
        run = client.notify_crawl_start(
            target_id=primary_target_id,
            component_name=COMPONENT_NAME,
            metadata={"trigger": "hub_xueqiu_adapter_cron", "total_targets": len(targets)},
        )
        logger.info(f"      [OK] 获发流水单号 run_id={run.id}")

        # 4. ── Step 2: 进程隔离驱动随机触发器 ──
        logger.info("[2/3] 唤起雪球核心驱动引擎 xueqiu_random_trigger.py (Process Isolation) ...")
        start_time = time.time()

        success = False
        global_error_message = None

        try:
            # 1. 获取当前脚本所在的绝对目录路径
            current_dir = os.path.dirname(os.path.abspath(__file__))

            # 2. 动态拼接出同级别下 xueqiu_random_trigger.py 的绝对路径
            trigger_path = os.path.join(current_dir, "xueqiu_random_trigger.py")

            # 3. 使用绝对路径组装命令，彻底解决因 cd 目录引起的找不到文件问题
            cmd = [sys.executable, trigger_path]

            # 保持原有的捕获输出和执行逻辑
            result = subprocess.run(cmd, check=True, text=True, capture_output=True)
            stdout_output = result.stdout or ""

            # 判定脚本输出中是否含有防风控避让标记
            if "避让" in stdout_output or "静默期" in stdout_output:
                success = True
                global_error_message = stdout_output.strip().split('\n')[-1]
            elif result.returncode == 0:
                success = True
            else:
                success = False
                global_error_message = f"雪球触发器返回非零状态码: {result.returncode}"
        except subprocess.CalledProcessError as e:
            combined_output = f"{e.stdout or ''}\n{e.stderr or ''}"
            if "避让" in combined_output or "静默期" in combined_output:
                success = True
                global_error_message = "环境占用或处于静默期，已执行策略性避让。"
            else:
                success = False
                global_error_message = f"雪球调度子进程崩溃: {str(e)}\n细节: {(e.stderr or '')[:200]}"
        except Exception as e:
            success = False
            global_error_message = f"适配器拦截异常: {str(e)}\n{traceback.format_exc()}"

        duration_ms = int((time.time() - start_time) * 1000)

        # 5. 【两阶段数据回写】同步更新本地雪球表与 Hub 全局大表的数据量和状态
        sync_local_results_back_to_hub(targets, success, global_error_message)

        # 6. ── Step 3: 向 Hub 打卡关闭流水 (通知其释放运行锁并记入日志、系统事件) ──
        status_label = "SUCCESS" if success else "FAILED"
        logger.info(f"[3/3] 整体监控上报: notify_crawl_end ({status_label}) ...")

        # 处理成功时标记受影响的目标数，如果是避让，数量置为 0
        processed_count = len(targets) if success and not (
                global_error_message and "避让" in global_error_message) else 0

        client.notify_crawl_end(
            run_id=run.id,
            target_id=primary_target_id,
            component_name=COMPONENT_NAME,
            success=success,
            items_found=processed_count,
            items_new=0,
            items_failed=0 if success else len(targets),
            error_message=global_error_message[:500] if global_error_message else None,
            duration_ms=duration_ms,
        )
        logger.info(f"🏁 雪球全局打卡闭环完成。整体耗时: {duration_ms}ms")

    finally:
        hub_conn.close()


if __name__ == "__main__":
    main()