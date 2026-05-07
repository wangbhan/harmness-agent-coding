"""
将耗时的后台任务放到线程中进行执行，通过上锁的方式来保证线程安全
1.进行危险command拦截
2.所有操作消息队列中的数据全都要上锁后进行操作，防止队列数据被无效修改
3.添加最大后台执行任务数量，同时通过信号量的方式来保证最大后台任务数量
4.在每次调用模型前将已完成的任务放到与模型的对话中
"""
import subprocess
import threading
import uuid
from pathlib import Path

from internal.Agent.config import get_config
from internal.Agent.tools.base import BaseTool, get_workdir

_BG_MANAGER : "BackGroundManager | None" = None

def _get_bg_manager() -> "BackGroundManager":
    """延迟初始化 TaskManager，确保配置和 WORKDIR 已就绪"""
    global _BG_MANAGER
    if _BG_MANAGER is None:
        cfg = get_config().tools.bash
        work_dir = get_workdir()
        _BG_MANAGER = BackGroundManager(work_dir, cfg.bg_max_concurrent)
    return _BG_MANAGER


class BackGroundManager:
    def __init__(self, work_dir: Path, max_concurrent: int):
        self.tasks = {}
        self._lock = threading.Lock()
        self._notifications = []
        self.work_dir = work_dir
        self._semaphore = threading.Semaphore(max_concurrent)

    def run(self, command: str) -> str:
        cfg = get_config().tools.bash
        # 危险command拦截
        if any(cmd in command for cmd in cfg.dangerous_commands):
            return "不允许执行危险命令"
        # 并发控制
        if not self._semaphore.acquire(blocking=False):
            return f"当前后台任务已达上线（{cfg.bg_max_concurrent}），请稍后再试"
        # 创建任务并执行任务
        task_id = str(uuid.uuid4())
        # 所有操作任务状态需要放到上锁执行
        with self._lock:
            self.tasks[task_id] = {"status": "running", "command": command, "result": None}
        # 执行任务
        thread = threading.Thread(target=self._work, args=(task_id, command), daemon=True)
        thread.start()
        return f"任务已开始执行，任务ID为：{task_id}"

    def _work(self, task_id: str, command: str):
        """执行任务命令"""
        cfg = get_config().tools.bash
        try:
            result = subprocess.run(command, shell=True, cwd=self.work_dir,
                                    capture_output=True, text=True, timeout=cfg.bg_timeout, encoding=cfg.encoding)
            output = ((result.stdout or "") + (result.stderr or "")).strip()[:cfg.max_output_len]
            status = "completed"
        # 超时情况
        except subprocess.TimeoutExpired:
            output = f"任务执行超时，{cfg.bg_timeout}秒内未完成"
            status = "timeout"
        # 执行错误情况
        except Exception as e:
            output = f"任务执行错误：{str(e)}"
            status = "error"
        # 对任务进行上锁
        with self._lock:
            # 更新任务状态
            self.tasks[task_id]["status"] = status
            self.tasks[task_id]["result"] = output or ""
            # 加入通知队列
            self._notifications.append({"task_id": task_id, "status": status, "command": command[:80], "result": output[:500]})
            # 任务完成释放信号量
            self._semaphore.release()

    def check(self, task_id: str) -> str:
        """查询单个任务状态或列出所有任务"""
        with self._lock:
            if task_id:
                task = self.tasks.get(task_id)
                if not task:
                    return f"任务不存在：{task_id}"
                return f"任务状态为：{task['status']} {task['command'][:60]} \n 输出结果为：{task.get('result') or '(running)'}"
            lines = []
            for tid, t in self.tasks.items():
                lines.append(f"任务ID：{tid} 状态：{t['status']} 命令：{t['command'][:60]}")
        return "\n".join(lines) if lines else "没有任务"

    def drain_notifications(self) -> list:
        """返回并清除所有待处理的完成通知"""
        with self._lock:
            notifs = list(self._notifications)
            self._notifications.clear()
        return notifs

class BgTool(BaseTool):
    name = "background"
    description = "在后台运行或查询bash命令。action='run'启动后台任务，action='check'查询任务状态和结果"
    param_descriptions = {
        "action": "操作类型：'run' 启动后台任务，'check' 查询任务状态",
        "command": "bash命令（action='run'时必填）",
        "task_id": "任务ID（action='check'时必填，留空列出所有任务）",
    }

    def execute(self, action: str = "run", command: str = "", task_id: str = "") -> str:
        try:
            if action == "check":
                return _get_bg_manager().check(task_id)
            if not command:
                return "错误：action='run' 时必须提供 command 参数"
            return _get_bg_manager().run(command)
        except Exception as e:
            return f"错误：{e}"

