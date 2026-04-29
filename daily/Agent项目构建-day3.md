# Day 3：工具重构 + 压缩策略 + Task 任务管理

> 日期：2026-04-29
> 涉及文件：`internal/Agent/tools/`、`internal/Agent/base_agent.py`

## 一、项目结构变更

相比 Day 2，`tools.py` 单文件拆分为 **tools 包**，新增压缩和任务管理模块：

```
app/
└── internal/
    └── Agent/
        ├── base_agent.py      # ⬆️ 重构：集成压缩机制
        └── tools/             # 📦 从单文件 tools.py 拆分为包
            ├── __init__.py    # 🆕 工具注册编排 + 延迟初始化
            ├── base.py        # 🆕 BaseTool ABC + Schema 生成辅助
            ├── registry.py    # 🆕 ToolRegistry 注册表
            ├── bash.py        # ⬆️ @tool → BashTool(BaseTool)
            ├── read.py        # ⬆️ @tool → ReadTool(BaseTool)
            ├── write.py       # ⬆️ @tool → WriteTool(BaseTool)
            ├── edit.py        # ⬆️ @tool → EditTool(BaseTool)
            ├── skill.py       # ⬆️ @tool → SkillTool(BaseTool)
            ├── sub_agent.py   # ⬆️ 工厂函数 → DelegateTool(BaseTool)
            ├── compact.py     # 🆕 三级压缩策略实现
            ├── task.py        # 🆕 TaskManager + 四个 Task 工具类
            └── todo.py        # ❌ 已被 task.py 替代
```

**本轮核心变更**：
- `tools`：从 `@tool` 装饰器重构为 `BaseTool` ABC + `ToolRegistry` 类架构
- `compact.py`：实现 micro_compact（被动占位）、auto_compact（LLM 摘要）、CompactTool（按需触发）三级压缩
- `task.py`：用持久化 TaskManager 替代内存 TodoManager，支持依赖关系和状态跟踪
- `base_agent.py`：Agent 循环中集成每轮被动压缩和按需主动压缩

---

## 二、Tool 框架重构

Day 1/2 的工具系统基于 `@tool` 装饰器 + `ToolDescriptor` 数据类，存在以下问题：
- 装饰器注册依赖模块导入时的副作用，难以控制加载顺序
- 子代理委派工具的工厂函数产生循环导入

本轮将工具系统重构为 **ABC 继承 + 显式注册** 架构。

### 2.1 BaseTool 抽象基类

```python
class BaseTool(ABC):
    """所有工具的抽象基类"""

    name: str = ""                    # 子类必须定义
    description: str = ""             # 可选，默认从 execute docstring 提取
    param_descriptions: dict = {}     # 可选，参数描述
    schema_override: dict | None = None  # 可选，自定义 OpenAI schema

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """工具执行入口，子类应使用具体参数签名"""
        ...

    def to_openai_schema(self) -> dict:
        """从 execute 方法签名自动生成 OpenAI tool definition"""
        ...
```

**设计要点**：

| 要素 | 说明 |
|------|------|
| `name` | 类属性，工具唯一标识，子类必须设置 |
| `execute()` | 抽象方法，使用具体参数签名（如 `execute(self, command: str)`） |
| `to_openai_schema()` | 通过 `inspect.signature()` 反射 `execute` 的参数，自动生成 OpenAI function-calling schema |
| `schema_override` | 对于列表/嵌套类型参数，手动定义 schema 覆盖自动生成 |

### 2.2 自动 Schema 生成

`to_openai_schema()` 的核心流程：

```
execute(self, command: str, timeout: int = 120)
    ↓ inspect.signature()
参数列表 → 遍历每个参数：
    ↓ _python_type_to_json()
类型注解 → JSON Schema 类型（str→string, int→integer, ...）
    ↓ _parse_docstring_summary() / _parse_param_descriptions()
docstring → 提取工具描述和 :param 描述
    ↓
组装为 OpenAI function-calling schema
```

子类只需定义 `execute` 的参数签名和 docstring，schema 自动生成，无需手动维护。

### 2.3 ToolRegistry 注册表

```python
class ToolRegistry:
    """工具注册表，管理工具注册、schema 生成和调用分发"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool_instance: BaseTool) -> None:
        """注册一个工具实例"""
        self._tools[tool_instance.name] = tool_instance

    def get_openai_tools(self, exclude: set[str] | None = None) -> list[dict]:
        """返回 OpenAI 格式 schema 列表，支持按名称排除"""
        ...

    def call(self, name: str, arguments_json: str) -> str:
        """JSON 反序列化 + 分派到 tool.execute(**kwargs)"""
        ...
```

与 Day 1/2 的注册表相比，核心接口保持一致（`register`、`get_openai_tools`、`call`），但底层从 `ToolDescriptor` 数据类改为直接持有 `BaseTool` 实例。

### 2.4 工具迁移示例

以 `BashTool` 为例，从装饰器到类继承的迁移：

```python
# Day 2：装饰器模式
@tool(name="bash", description="执行 bash 命令")
def run_bash(command: str) -> str:
    """执行 bash 命令"""
    ...

# Day 3：类继承模式
class BashTool(BaseTool):
    name = "bash"
    description = "执行 shell 命令并在沙箱环境中运行"
    param_descriptions = {"command": "要执行的 shell 命令"}

    def execute(self, command: str) -> str:
        """运行bash命令"""
        ...
```

**显式注册（`__init__.py`）**：

```python
default_registry.register(BashTool())
default_registry.register(ReadTool())
default_registry.register(WriteTool())
...
```

所有工具在 `__init__.py` 中显式实例化并注册，消除装饰器副作用。

### 2.5 DelegateTool 延迟绑定

子代理委派工具改为类继承 + 延迟绑定模式，解决循环导入：

```python
class DelegateTool(BaseTool):
    name = "delegate"
    _sub_agent = None

    def bind(self, sub_agent):
        """延迟注入子 Agent，避免循环导入"""
        self._sub_agent = sub_agent

    def execute(self, task: str) -> str:
        sub_messages = [
            {"role": "system", "content": subagent_system},
            {"role": "user", "content": task},
        ]
        self._sub_agent.run(sub_messages)
        ...
```

```python
# __init__.py 中延迟初始化
def setup_delegate():
    from internal.Agent.base_agent import Agent
    sub_agent = Agent(...)
    delegate_tool.bind(sub_agent)
```

---

## 三、压缩策略

对话历史无限增长会导致 token 超限。Day 1 的 TODO 中已规划此功能，本轮实现三级压缩策略：

| 层级 | 名称 | 触发方式 | 压缩强度 |
|------|------|----------|----------|
| 1 | `micro_compact` | 每轮自动 | 轻量：占位符替换 |
| 2 | `auto_compact` | LLM 调用 compact 工具后 | 重量：磁盘保存 + LLM 摘要 |
| 3 | `CompactTool` | LLM 自主决定 | 信号工具，触发层级 2 |

### 3.1 micro_compact — 被动压缩

每轮 Agent 循环开始时自动执行，将旧的 `tool_result` 替换为占位符：

```python
KEEP_RECENT = 3              # 保留最近 3 轮 tool_result
PRESERVE_RESULT_TOOLS = {"read", "todo"}  # 这些工具的结果不替换

def micro_compact(messages: list):
    """将旧的 tool_result 变为占位符，保留某些读取结果防止工具再次调用"""
    # 1. 收集所有 role=tool 的消息
    tool_results = [(i, msg) for i, msg in enumerate(messages) if msg.get("role") == "tool"]

    if len(tool_results) <= KEEP_RECENT:
        return messages

    # 2. 构建 tool_call_id → tool_name 映射
    tool_map = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                tool_map[tc["id"]] = tc["function"]["name"]

    # 3. 对超出保留窗口的结果，替换为占位符
    for i, msg in tool_results[:-KEEP_RECENT]:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= 100:  # 短结果保留
            continue
        tool_name = tool_map.get(msg.get("tool_call_id", ""), "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:  # read/todo 结果保留
            continue
        msg["content"] = f"[Previous: used {tool_name}]"

    return messages
```

**保护规则**：

| 规则 | 说明 |
|------|------|
| 保留最近 3 轮 | `KEEP_RECENT = 3`，确保当前上下文完整 |
| 短结果保留 | 内容 ≤ 100 字符的不替换 |
| 特定工具保留 | `read`、`todo` 工具的结果不替换，避免重复读取 |

### 3.2 auto_compact — 主动压缩

当 LLM 调用 `compact` 工具后触发，将完整对话保存到磁盘并生成摘要：

```python
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

def auto_compact(messages: list, client, model: str = "glm-5.1", topic: str = "") -> str:
    """将对话保存到磁盘，调用 LLM 生成摘要"""
    # 1. 将完整对话以 JSON 行格式追加到磁盘
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{topic}.json"
    with open(transcript_path, "a", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")

    # 2. 取最后 80000 字符发送给 LLM 生成摘要
    conversation_text = json.dumps(messages, default=str)[-80000:]
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content":
            "请对本次对话进行总结...总结内容应包含："
            "1) 已取得的成果；2) 当前的进展状态；3) 已做出的关键决策。 "
            "请力求简洁，但务必保留关键细节。\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = response.choices[0].message.content or "未生成摘要。"
    return f"[对话已压缩。对话保存位置： {transcript_path}]\n\n{summary}"
```

**压缩结果**：整个 `messages` 列表被替换为 `[system_msg] + [包含摘要的 user_msg]`，大幅减少 token 消耗。

### 3.3 CompactTool — LLM 按需调用

注册为工具，让 LLM 自主决定何时压缩：

```python
class CompactTool(BaseTool):
    name = "compact"
    description = ("压缩对话历史。当上下文过长或对话轮次过多时调用此工具，"
                   "将历史对话压缩为摘要以释放上下文空间。"
                   "请单独调用，不要与其他工具同时调用。")

    def execute(self) -> str:
        return "压缩请求已接收，正在压缩对话历史..."
```

`CompactTool` 本身不执行压缩，仅作为信号。Agent 循环检测到 `compact` 被调用后，才执行 `auto_compact`。

### 3.4 Agent 循环集成

```python
def run(self, messages: list[dict]) -> None:
    while True:
        # 被动压缩：每轮自动执行
        messages = micro_compact(messages)

        response = self.client.chat.completions.create(...)
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if finish_reason == "stop":
            return

        # 并行执行工具调用
        compact_called = False
        for block in tool_calls:
            output = results[block.id]
            messages.append({"role": "tool", "tool_call_id": block.id, "content": output})
            if block.function.name == "compact":
                compact_called = True

        # 主动压缩：LLM 请求后执行
        if compact_called:
            system_msg = self._extract_system_message(messages)
            compact_content = auto_compact(messages, self.client, self.model)
            messages[:] = [
                system_msg,                                          # 保留 system prompt
                {"role": "user", "content": compact_content},       # 摘要替代全部历史
            ]
```

**集成流程**：

```
Agent 循环每轮
    │
    ├─ ① micro_compact(messages)     ← 被动：每轮自动
    │
    ├─ ② 调用 LLM
    │
    ├─ ③ 并行执行 tool_calls
    │      └─ 检测是否调用 compact
    │
    └─ ④ if compact_called:
           auto_compact(messages)     ← 主动：LLM 请求后
           messages = [system + 摘要]
```

---

## 四、Task 任务管理系统

Day 2 的 `TodoManager` 存在不足：纯内存存储、无任务间依赖、无法并行执行。本轮用 `TaskManager` 替代。

### 4.1 设计思路

| 对比项 | TodoManager（Day 2） | TaskManager（Day 3） |
|--------|---------------------|---------------------|
| 存储 | 内存 `self.items` 列表 | 磁盘 `.tasks/task_N.json` 文件 |
| 持久化 | 无，重启丢失 | 每次操作写入磁盘 |
| 依赖关系 | 无 | `blockedBy` 前置依赖列表 |
| 并行支持 | 同时仅 1 个 `in_progress` | `blockedBy` 为空即可执行 |
| 状态转换 | 手动更新 | `completed` 自动解锁后续任务 |

**三个核心问题**：
1. **什么时候可以做**：状态为 `pending` 且 `blockedBy` 为空的任务
2. **什么被卡住**：等待 `blockedBy` 任务完成的任务
3. **什么做完了**：状态为 `completed` 的任务，完成后自动解锁后续任务

### 4.2 TaskManager 类设计

```python
TASKS_DIR = WORKDIR / ".tasks"

class TaskManager:
    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1       # 自增 ID，基于磁盘文件扫描
        self._lock = threading.Lock()             # 线程安全锁

    def create(self, subject: str, description: str = "") -> str:
        """创建任务（加锁防止并发 ID 冲突）"""
        with self._lock:
            task = {
                "id": self._next_id, "subject": subject, "description": description,
                "status": "pending", "blockedBy": [], "owner": "",
            }
            self._save(task)
            self._next_id += 1
        return json.dumps(task, indent=2, ensure_ascii=False)

    def update(self, task_id: int, status: str, ...) -> str:
        """更新任务状态，completed 时自动清除后续任务的 blockedBy"""
        task = self._load(task_id)
        if status == "completed":
            self._clear_dependency(task_id)       # 解锁依赖此任务的其他任务
        self._save(task)
        ...
```

**任务文件结构**（`task_1.json`）：

```json
{
  "id": 1,
  "subject": "分析项目结构",
  "description": "阅读并理解现有代码",
  "status": "pending",
  "blockedBy": [],
  "owner": ""
}
```

**状态机**：

```
pending ──→ in_progress ──→ completed
                                    │
                              _clear_dependency()
                              从其他任务的 blockedBy 中移除此 ID
```

### 4.3 四个 Task 工具

| 工具名 | 类 | 功能 | Schema 方式 |
|--------|-----|------|-------------|
| `task_create` | `TaskCreateTool` | 创建新任务 | 自动生成 |
| `task_get` | `TaskGetTool` | 获取任务详情 | 自动生成 |
| `task_list` | `TaskListTool` | 列出所有任务及状态 | 自动生成 |
| `task_update` | `TaskUpdateTool` | 更新状态/依赖关系 | 手动定义（含数组参数） |

`TaskUpdateTool` 因 `add_blocked_by` 和 `remove_blocked_by` 为数组类型，使用 `schema_override` 手动定义：

```python
TASK_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": "更新任务状态或依赖关系。完成后自动解除后续任务的阻塞。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                "remove_blocked_by": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task_id"],
        },
    },
}

class TaskUpdateTool(BaseTool):
    name = "task_update"
    schema_override = TASK_UPDATE_SCHEMA    # 手动 schema 覆盖自动生成
    ...
```

**渲染输出示例**：

```
[ ] #1: 分析项目结构
[>] #2: 编写核心代码 (blocked by: [1])
[x] #3: 测试验证
```

### 4.4 线程安全

`create()` 方法使用 `threading.Lock` 保护，防止并发创建时 ID 冲突：

```python
def create(self, subject: str, description: str = "") -> str:
    with self._lock:                           # 加锁
        task = {"id": self._next_id, ...}
        self._save(task)                       # 写入磁盘
        self._next_id += 1                     # 自增 ID
    return json.dumps(task, ...)
```

仅 `create` 加锁——它是唯一的追加点。`get`、`update`、`list_all` 操作单个文件，无需锁保护。

---

## 五、后续规划（TODO）

- [ ] **task 工具增强**：支持任务优先级、截止时间、任务分配
- [ ] **压缩策略优化**：micro_compact 可配置保留轮次和阈值；auto_compact 支持增量摘要
- [ ] **错误恢复**：任务状态异常时的回滚机制；压缩失败时的降级策略
- [ ] **多类型子 Agent**：注册不同工具集的子 Agent（代码审查、测试等），按场景委派
- [ ] **配置模块**：抽取 LLM 配置为独立模块，支持多模型切换
- [ ] **权限管理**：扩展 safe_path 为交互式权限确认机制
