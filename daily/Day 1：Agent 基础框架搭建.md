# Day 1：Agent 基础框架搭建

> 日期：2026-04-15
> 涉及文件：`internal/Agent/start.py`、`internal/Agent/tools.py`

## 一、项目结构

```
app/
├── main.py                  # 入口文件（尚未对接 Agent）
├── pyproject.toml           # 项目配置与依赖
├── config.yaml              # 配置文件（待填充）
├── configs/                 # 配置目录（待填充）
├── daily/                   # 开发日志
└── internal/
    └── Agent/
        ├── start.py         # Agent 核心循环
        ├── tools.py         # 工具注册框架 + 工具实现
        ├── llm_config.py    # LLM 配置（TODO）
        └── todolist.py      # 任务管理（TODO）
```

**技术栈**：Python 3.13 + OpenAI SDK（兼容接口）+ Pydantic
**LLM**：通过 z.ai 平台调用 `glm-4.7` 模型

---

## 二、Agent 双循环架构

Agent 采用 **外层用户对话循环 + 内层任务执行循环** 的双循环设计：

```
用户对话循环（外层）
│
├─ 用户输入问题 → 追加到 history
│
└─ 任务执行循环（内层 agent_loop）
   │
   ├─ 调用 LLM，传入 messages + tools
   │
   ├─ 判断 finish_reason：
   │   ├─ "stop"       → 返回文本回复，结束循环
   │   └─ "tool_calls" → 执行工具，结果回传，继续循环
   │
   └─ 重复直到任务完成
```

### 2.1 内层循环：任务执行（agent_loop）

核心流程：

1. **调用 LLM 接口**：将完整消息历史（含 system prompt）和工具定义传入模型
2. **判断响应类型**：
   - `finish_reason == "stop"` — 模型直接返回文本，循环结束
   - `finish_reason == "tool_calls"` — 模型请求调用工具，进入分发流程
3. **工具分发**：遍历 `message.tool_calls`，通过 `default_registry.call(name, arguments)` 执行对应工具
4. **结果回传**：将工具执行结果以 `{role: "tool", tool_call_id, content}` 格式追加到消息历史
5. **循环回到步骤 1**，直到模型不再请求工具调用

### 2.2 外层循环：用户对话

```python
while True:
    query = input("请输入问题：")
    if query in ("q", "exit", ""):
        break
    history.append({"role": "user", "content": query})
    agent_loop(history)
```

- 维护持久 `history` 列表，跨轮次保留上下文
- 用户输入 `q`、`exit` 或空字符串时退出对话

### 2.3 LLM 接口返回结构

以一次实际的工具调用响应为例，关键字段说明：

```python
ChatCompletion(
    choices=[
        Choice(
            finish_reason='tool_calls',   # "tool_calls"=需要执行工具 | "stop"=直接返回文本
            message=ChatCompletionMessage(
                content='我来帮你配置环境变量...',     # 模型回答内容
                tool_calls=[
                    ChatCompletionMessageFunctionToolCall(
                        id='call_-7709354386652127844',
                        function=Function(
                            arguments='{"command":"echo ..."}',  # 工具参数（JSON 字符串）
                            name='bash'                          # 工具名称
                        ),
                        type='function',
                    )
                ],
                reasoning_content="..."   # 模型思考过程
            )
        )
    ],
    usage=CompletionUsage(
        completion_tokens=100,
        prompt_tokens=178,
        total_tokens=278,
    ),
)
```

---

## 三、Tool 工具框架

### 3.1 框架设计

工具框架采用 **装饰器 + 数据类 + 注册表** 的三层架构：

| 层次 | 组件 | 职责 |
|------|------|------|
| 解析层 | `_parse_docstring_summary`、`_parse_param_descriptions`、`_python_type_to_json` | 从函数签名和 docstring 提取工具元信息 |
| 描述层 | `ToolDescriptor`（dataclass） | 封装工具的名称、描述、参数，可转换为 OpenAI tools schema |
| 注册层 | `ToolRegistry` | 管理工具注册、schema 生成、工具调用分发 |

**工作流程**：

```
普通函数 → @tool 装饰器 → 提取元信息 → 封装为 ToolDescriptor → 注册到 ToolRegistry
                                                                          ↓
LLM 返回 tool_calls → registry.call(name, arguments_json) → 执行对应 handler
```

### 3.2 @tool 装饰器

支持两种用法：

```python
# 自动从函数名和 docstring 提取信息
@tool
def run_bash(command: str) -> str:
    """执行 bash 命令"""
    ...

# 自定义名称和描述
@tool(name="bash", description="执行 shell 命令")
def run_bash(command: str) -> str:
    ...
```

工具名默认取 `fn.__name__.removeprefix("run_")`，即 `run_bash` → `bash`。

### 3.3 已实现工具

| 工具名 | 函数 | 功能 | 安全机制 |
|--------|------|------|----------|
| `bash` | `run_bash` | 执行 shell 命令 | 危险命令黑名单 + 120s 超时 + 输出截断 5000 字符 |
| `read` | `run_read` | 读取文件内容 | `safe_path` 路径验证 + 输出截断 50000 字符 |
| `write` | `run_write` | 写入文件 | `safe_path` 路径验证 + 自动创建父目录 |
| `edit` | `run_edit` | 替换文件中的文本片段 | `safe_path` 路径验证 + 仅替换首次匹配 |

### 3.4 safe_path 路径安全机制

所有文件操作工具均通过 `safe_path` 验证路径安全性：

```python
WORKDIR = Path.cwd()

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()        # 拼接并解析为绝对路径
    if not path.is_relative_to(WORKDIR):  # 检查是否在工作区内
        raise ValueError("路径超出工作区范围")
    return path
```

- 将用户传入的路径与工作目录拼接后 `.resolve()`，防止 `../` 等路径穿越攻击
- 仅允许操作当前工作目录下的文件

---

## 四、后续规划（TODO）

- [ ] **llm_config.py**：抽取 LLM 配置（API Key、Base URL、模型名等）为独立模块
- [ ] **todolist.py**：实现任务管理功能
- [ ] **对话压缩策略**：history 无限增长会导致 token 超限，需实现滑动窗口或摘要压缩
- [ ] **权限管理**：safe_path 可扩展为"询问用户是否允许"的交互式权限机制
- [ ] **main.py 对接**：将 Agent 接入主入口文件
- [ ] **config.yaml 配置**：填充配置文件
