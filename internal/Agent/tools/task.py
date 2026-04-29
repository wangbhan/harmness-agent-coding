"""
todo工具中只是在内存中规划要做的事情，虽然有顺序和状态，但是没有执行任务的前后依赖，
并且每次只能执行一个任务，但是有时是可以并行执行的
方案：
将工具中的任务清单升级为持久化存储为磁盘的任务图，每个任务作为一个json文件，有状态、前置依赖等，并且随时回答一下三个问题：
 1.什么时候可以做：状态为pending且blockedby为空的任务
 2.什么被卡住：等待blockedby任务完成的任务
 3.什么做完了：状态为completed的任务，完成后自动解锁后续任务
"""
import json
from pathlib import Path

from internal.Agent.tools.base import BaseTool, WORKDIR

TASKS_DIR = WORKDIR / ".tasks"

class TaskManager:
    """任务管理器，维护任务列表的增删改查"""
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1
        self.items = []

    def _max_id(self) -> int:
        """找出最大任务数"""
        ids = [int(p.stem.split("_")[1]) for p in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    def _load(self, task_id: int) -> dict:
        """加载任务"""
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"没有{task_id}任务")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        """存储任务"""
        path = self.dir / f"task_{task['id']}.json"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def _clear_dependency(self, completed_id: int):
        """任务完成后，清除任务的前置锁"""
        for f in self.dir.glob(f"task_*.json"):
            task = json.loads(f.read_text())
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def create(self, subject: str, description: str = "") -> str:
        """创建任务"""
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "owner": "",
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, task_id: int) -> str:
        """获取任务信息"""
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)

    def update(self, task_id: int, status: str, add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        """更新任务列表，校验参数并渲染结果"""
        # 加载当前任务
        task = self._load(task_id)
        # 判断状态
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"任务 {task_id}: 错误状态 '{status}'")
            task["status"] = status
            # 状态完成后清楚任务前置锁
            if status == "completed":
                self._clear_dependency(task_id)
        # 如果添加新的前置锁需要放到任务的前置锁中
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        # 如果有移除前置锁，则需要从任务前置锁中移除
        if remove_blocked_by:
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
        # 保存任务状态
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)


    def list_all(self) -> str:
        """渲染所有任务列表为可读文本"""
        tasks = []
        # 对任务列表进行排序
        files = sorted(
            self.dir.glob("task_*.json"),
            key=lambda f: int(f.stem.split("_")[1])
        )
        for f in files:
            task = json.loads(f.read_text())
            tasks.append(task)

        if not tasks:
            return "已无其他任务"

        lines = []

        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")

        return "\n".join(lines)

TASKS = TaskManager(TASKS_DIR)

# ============================================================
# Schema（list 类型参数无法自动生成，需手动定义）
# ============================================================

TASK_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": ("更新任务状态或依赖关系。可将任务标记为 pending/in_progress/completed，"
                        "完成后自动解除后续任务的阻塞。也可添加或移除前置依赖。"),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "要更新的任务ID"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                    "description": "任务新状态，completed 时自动解锁后续任务",
                },
                "add_blocked_by": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "添加前置依赖的任务ID列表",
                },
                "remove_blocked_by": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "移除前置依赖的任务ID列表",
                },
            },
            "required": ["task_id"],
        },
    },
}


# ============================================================
# 工具类
# ============================================================

class TaskCreateTool(BaseTool):
    name = "task_create"
    description = "创建新任务，支持设置标题和描述"
    param_descriptions = {
        "subject": "任务标题",
        "description": "任务详细描述",
    }

    def execute(self, subject: str, description: str = "") -> str:
        """创建任务"""
        try:
            return TASKS.create(subject, description)
        except Exception as e:
            return f"错误：{e}"


class TaskGetTool(BaseTool):
    name = "task_get"
    description = "获取指定任务的详细信息"
    param_descriptions = {"task_id": "任务ID"}

    def execute(self, task_id: int) -> str:
        """获取任务"""
        try:
            return TASKS.get(task_id)
        except Exception as e:
            return f"错误：{e}"


class TaskListTool(BaseTool):
    name = "task_list"
    description = "列出所有任务及其状态，显示哪些可执行、哪些被阻塞、哪些已完成"

    def execute(self) -> str:
        """列出所有任务"""
        try:
            return TASKS.list_all()
        except Exception as e:
            return f"错误：{e}"


class TaskUpdateTool(BaseTool):
    name = "task_update"
    schema_override = TASK_UPDATE_SCHEMA

    def execute(self, task_id: int, status: str = "",
                add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        """更新任务"""
        try:
            return TASKS.update(task_id, status or None, add_blocked_by, remove_blocked_by)
        except Exception as e:
            return f"错误：{e}"