# Day 2：工具完善 + Agent 类封装 + 子代理委派

> 日期：2026-04-17
> 涉及文件：`internal/Agent/start.py`、`internal/Agent/tools.py`

## 一、项目结构变更

相比 Day 1，文件结构无新增文件，但对两个核心文件进行了**重大重构**：

```
app/
└── internal/
    └── Agent/
        ├── start.py         # ⬆️ 重构：函数 → Agent 类 + 子代理委派
        ├── tools.py         # ⬆️ 重构：新增 TodoManager + 多线程执行
        ├── llm_config.py    # LLM 配置（TODO）
        └── todolist.py      # 任务管理（TODO）
```

**本轮核心变更**：
- `start.py`：从函数式循环重构为 `Agent` 类封装，新增 `delegate` 子代理委派机制，工具调用改为并行执行
- `tools.py`：新增 `TodoManager` 任务管理器及 `todo` 工具，新增 `schema_override` 支持自定义 Schema

---

## 二、Agent 类封装与并行执行

Day 1 的 `agent_loop` 是一个独立函数，本轮将其封装为 `Agent` 类，并引入多线程并行工具执行。

### 2.1 Agent 类设计

```python
class Agent:
    """LLM Agent，封装客户端、工具集和对话循环"""

    def __init__(self, client, registry, tools, model="glm-5.1", max_tokens=8000):
        self.client = client       # OpenAI 兼容客户端
        self.registry = registry   # 工具注册表
        self.tools = tools         # OpenAI 格式工具 Schema 列表
        self.model = model
        self.max_tokens = max_tokens

    def run(self, messages: list[dict]) -> None:
        """执行 agent 循环，直接修改 messages 列表"""
        ...
```

**设计要点**：

| 要素 | 说明 |
|------|------|
| `client` | OpenAI 兼容客户端，支持不同 SDK 接入 |
| `registry` | 工具注册表（`ToolRegistry` 实例），负责工具分发 |
| `tools` | OpenAI 格式 Schema 列表，传入 LLM 接口 |
| `run()` | 执行 ReAct 循环，直接修改传入的 `messages` 列表 |

通过将 `client`、`registry`、`tools` 作为构造参数，可以灵活创建**不同配置的 Agent 实例**——这为后续子代理机制奠定了基础。

### 2.2 并行工具执行

Day 1 中工具调用是串行执行的。本轮使用 `ThreadPoolExecutor` 将同一轮中的多个工具调用并行化：

```python
# 并行执行所有工具调用
tool_calls = message.tool_calls
with ThreadPoolExecutor() as executor:
    future_to_id = {
        executor.submit(
            self.registry.call, block.function.name, block.function.arguments
        ): block.id
        for block in tool_calls
    }
    results = {}
    for future in as_completed(future_to_id):
        call_id = future_to_id[future]
        results[call_id] = future.result()

# 按原始顺序追加结果
for block in tool_calls:
    output = results[block.id]
    messages.append({
        "role": "tool",
        "tool_call_id": block.id,
        "content": output,
    })
```

**并行策略**：

| 步骤 | 说明 |
|------|------|
| 1. 提交任务 | 遍历 `tool_calls`，为每个调用创建 `ThreadPoolExecutor` 子任务 |
| 2. 异步收集 | 通过 `as_completed` 收集结果，映射 `call_id → result` |
| 3. 有序追加 | 遍历原始 `tool_calls` 顺序，将结果追加到 `messages` |

> 注意：使用 `as_completed` 收集后按原始顺序追加，保证了消息历史的一致性。

---

## 三、TodoManager 任务管理工具

新增 `todo` 工具，使 Agent 具备**先规划后执行**的能力。

### 3.1 设计思路

传统的 Agent 直接执行工具，缺乏全局规划。`todo` 工具让 Agent 在处理复杂任务时：

1. **先规划**：将大任务拆解为多个步骤，写入任务列表
2. **再执行**：逐步完成每个子任务，更新任务状态
3. **后回顾**：每完成一步后检查任务列表，决定下一步操作

### 3.2 TodoManager 类

```python
class TodoManager:
    """任务管理器，维护任务列表的增删改查"""
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """更新任务列表，校验参数并渲染结果"""
        # 校验：最多 20 个任务
        # 校验：同一时间只有 1 个 in_progress 任务
        ...

    def render(self) -> str:
        """渲染任务列表为可读文本"""
        ...
```

**校验规则**：

| 校验项 | 规则 |
|--------|------|
| 任务数量 | 最多 20 个 |
| 进行中任务 | 同一时间仅允许 1 个 `in_progress` |
| 状态枚举 | `pending` / `in_progress` / `completed` |
| 文本必填 | 每个任务的 `text` 不能为空 |

**渲染输出示例**：

```
[ ] #1: 分析项目需求
[>] #2: 编写核心代码
[x] #3: 测试验证

(1/3 completed)
```

### 3.3 自定义 Schema

`todo` 工具的参数是嵌套数组类型，无法通过函数签名自动生成 Schema，因此使用 `schema_override` 手动定义：

```python
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
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
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
    """更新任务列表，跟踪多步骤任务的进度"""
    return _todo_manager.update(items)
```

为此，`ToolDescriptor.to_openai_schema()` 新增了 `schema_override` 优先判断：

```python
def to_openai_schema(self) -> dict:
    if self.schema_override:
        return self.schema_override          # 优先使用自定义 Schema
    # 否则从函数签名自动生成 ...
```

---

## 四、子 Agent 委派机制

参考主流 Agent 框架思路，将子 Agent 封装为一个工具（`delegate`），主 Agent 可按需委派子任务。

### 4.1 架构设计

```
┌─────────────────────────────────┐
│          Parent Agent           │
│  tools: [read, write, edit,     │
│          bash, todo, delegate]  │
│                                 │
│   当需要委派子任务时              │
│            ↓ 调用 delegate 工具  │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│          Sub Agent              │
│  tools: [read, write, edit,     │
│          bash, todo]            │
│  （排除 delegate，防止递归）     │
│                                 │
│  独立上下文，完成后返回摘要       │
└─────────────────────────────────┘
```

### 4.2 关键设计点

| 设计点 | 实现方式 |
|--------|----------|
| **子 Agent 独立上下文** | 子 Agent 拥有自己的 `messages` 列表，不污染父 Agent 上下文 |
| **结果摘要返回** | 子 Agent 完成后仅返回最终文本摘要（截断 8000 字符），而非完整对话历史 |
| **防止无限递归** | 子 Agent 工具集排除 `delegate`，不支持嵌套委派 |
| **工厂函数注册** | 通过 `make_delegate_handler()` 闭包捕获子 Agent 实例 |

### 4.3 委派工具实现

```python
def make_delegate_handler(sub_agent: Agent):
    """工厂函数：创建 delegate handler，闭包捕获子 agent 实例"""
    def run_delegate(task: str) -> str:
        sub_messages = [
            {"role": "system", "content": subagent_system},
            {"role": "user", "content": task},
        ]
        sub_agent.run(sub_messages)          # 执行子 Agent 循环
        last = sub_messages[-1]              # 取最后一段回复
        content = last.get("content", "")
        return content[:8000]                # 截断返回
    return run_delegate
```

### 4.4 父子 Agent 组装流程

```python
# 1. 创建子 Agent（工具集排除 delegate）
sub_agent = Agent(
    client=client,
    registry=default_registry,
    tools=default_registry.get_openai_tools(exclude={"delegate"}),
)

# 2. 将 delegate 工具注册到 default_registry
default_registry.register(ToolDescriptor(
    handler=make_delegate_handler(sub_agent),
    name="delegate",
    description="将子任务委派给子代理执行",
    schema_override=DELEGATE_SCHEMA,
))

# 3. 创建父 Agent（包含全部工具）
parent_agent = Agent(
    client=client,
    registry=default_registry,
    tools=default_registry.get_openai_tools(),
)
```

> `get_openai_tools(exclude={"delegate"})` 是新增的排除参数，允许按名称排除特定工具。

---

## 五、后续规划（TODO）

- [ ] **多类型子 Agent**：注册不同工具集的子 Agent（如代码审查 Agent、测试 Agent），按场景委派
- [ ] **llm_config.py**：抽取 LLM 配置为独立模块，支持多模型切换
- [ ] **对话压缩策略**：history 无限增长导致 token 超限，需实现滑动窗口或摘要压缩
- [ ] **权限管理**：扩展 safe_path 为交互式权限确认机制
- [ ] **config.yaml 配置**：填充配置文件，支持从配置加载 Agent 参数
