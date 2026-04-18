from internal.Agent.tools.base_tools import tool


class TodoManager:
    """任务管理器，维护任务列表的增删改查"""
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """更新任务列表，校验参数并渲染结果"""
        if len(items) > 20:
            raise ValueError("至多20个任务")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            print(f"任务 {item_id}: {text}")
            print(f"任务状态: {status}")
            print(f"任务ID: {item_id}")
            if not text:
                raise ValueError(f"任务 {item_id}: 必要文本不能为空")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("同一时间只能有一个任务处于进行中状态")
        self.items = validated
        print("任务可读文本：", self.render())
        return self.render()

    def render(self) -> str:
        """渲染任务列表为可读文本"""
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


# 模块级管理器实例
_todo_manager = TodoManager()

# agent_todo工具的自定义 OpenAI schema（嵌套 array 类型，无法自动生成）
TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": "更新任务列表，跟踪多步骤任务的进度",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "任务列表，每项包含 id、text、status",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "任务ID"},
                            "text": {"type": "string", "description": "任务描述"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态",
                            },
                        },
                        "required": ["id", "text", "status"],
                    },
                }
            },
            "required": ["items"],
        },
    },
}

@tool(schema=TODO_SCHEMA)
def run_todo(items: list):
    """
    更新任务列表，跟踪多步骤任务的进度
    :param items: 任务列表
    """
    try:
        return _todo_manager.update(items)
    except ValueError as e:
        return f"参数错误：{e}"