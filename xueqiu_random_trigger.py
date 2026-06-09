import os
import sys
import time
import random
import logging
from datetime import datetime, timedelta, time as dt_time
from dotenv import load_dotenv
from xueqiu_crawl_skill import XueqiuSmartSkill

# 加载配置
load_dotenv()

# --- 共享日志配置 ---
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

log_filename = os.path.join(LOG_DIR, f"xueqiu_{datetime.now().strftime('%Y%m%d')}.log")
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - [RandomTrigger] - %(levelname)s - %(message)s')

# 文件和控制台处理器
file_handler = logging.FileHandler(log_filename, encoding='utf-8')
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

LOCK_FILE = os.path.join(os.path.dirname(__file__), "xueqiu_run.lock")


class XueqiuRandomTrigger:
    def _get_env_time(self, key, default):
        time_str = os.getenv(key, default)
        try:
            h, m = map(int, time_str.split(':'))
            return dt_time(h, m)
        except:
            dh, dm = map(int, default.split(':'))
            return dt_time(dh, dm)

    def _is_quiet_time(self):
        now = datetime.now().time()
        q_start = self._get_env_time("QUIET_START", "00:00")
        q_end = self._get_env_time("QUIET_END", "00:00")
        if q_start > q_end:
            return now >= q_start or now < q_end
        return q_start <= now < q_end

    def run(self, run_minutes=None, debug_mode=False):
        logging.info("🔔 [Step 1/3] RandomTrigger 已被激活...")

        if debug_mode:
            logging.info("🛠️ 调试模式：跳过延迟与静默期，直接下发任务。")
            return self._execute_core(run_minutes, True)

        if self._is_quiet_time():
            msg = f"☕ 当前处于静默期 ({os.getenv('QUIET_START', '00:00')}-{os.getenv('QUIET_END', '00:00')})，触发器放弃任务并进入休眠。"
            logging.info(msg)
            return msg

        d_min = int(os.getenv("START_DELAY_MIN", 1))
        d_max = int(os.getenv("START_DELAY_MAX", 5))
        delay_mins = random.randint(d_min, d_max)
        wait_seconds = delay_mins * random.randint(40, 70)
        expected_t = (datetime.now() + timedelta(seconds=wait_seconds)).strftime('%H:%M:%S')

        logging.info(f"🎲 [Step 2/3] 模拟真人随机行为：延迟 {delay_mins} 分钟启动 | 预计执行点: {expected_t}")
        time.sleep(wait_seconds)

        logging.info("🚀 [Step 3/3] 延迟结束，正在移交控制权给核心 Skill...")
        result = self._execute_core(run_minutes, False)

        logging.info(f"🏁 RandomTrigger 周期执行完毕。返回状态: {result}")
        return result

    def _execute_core(self, run_minutes, debug_mode):
        if os.path.exists(LOCK_FILE):
            if time.time() - os.path.getmtime(LOCK_FILE) > 7200:
                os.remove(LOCK_FILE)
                logging.info("🔓 清理过期锁文件。")
            else:
                msg = "⚠️ 环境占用中，避让。"
                logging.warning(msg)
                return msg

        try:
            with open(LOCK_FILE, "w") as f:
                f.write(str(os.getpid()))

            skill = XueqiuSmartSkill()
            final_dur = run_minutes or random.randint(int(os.getenv("RUN_DURATION_MIN", 20)),
                                                      int(os.getenv("RUN_DURATION_MAX", 30)))

            res = skill.execute(run_minutes=final_dur, debug_mode=debug_mode)
            return f"Processed: {res}"
        except Exception as e:
            logging.error(f"💥 核心链路崩溃: {e}")
            return str(e)
        finally:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
                logging.info("🔓 环境锁已释放。")


if __name__ == "__main__":
    XueqiuRandomTrigger().run()