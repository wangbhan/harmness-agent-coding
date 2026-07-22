# Coding Agent

基于 ReAct 循环的自主编码 Agent，具备文件读写、Shell 执行、任务管理、子 Agent 委派和对话压缩等能力。

## 功能特性

- **ReAct 推理-行动循环** — Agent 在"思考"与"工具调用"之间迭代，直到任务完成
- **13 个内置工具** — bash、read、write、edit、task_create/get/list/update、skill、delegate、compact、background
- **子 Agent 委派** — 将子任务委派给独立上下文的子 Agent 执行，避免主上下文膨胀
- **三层对话压缩** — micro（被动占位）、auto（LLM 摘要）、manual（LLM 自主触发）
- **持久化任务管理** — 任务以 JSON 存储到磁盘，支持 `blockedBy` 依赖关系，完成后自动解锁后续任务
- **可扩展技能系统** — 预置 PDF 处理、PPTX 处理、技能创建器等技能
- **后台任务执行** — 耗时命令异步执行，信号量控制并发上限，完成后自动通知 Agent
- **配置中心化** — Pydantic v2 配置模型，三级加载（yaml → local → 环境变量）
- **全局实时日志** — loguru JSONL 结构化日志，实时记录 LLM、工具和后台任务的调用生命周期
- **可配置生命周期 Hook** — 通过独立 JSON 配置 UserPromptSubmit、PreToolUse、PostToolUse 和 Stop 命令 Hook

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.13 |
| 包管理 | [uv](https://docs.astral.sh/uv/) |
| LLM SDK | OpenAI SDK |
| LLM 后端 | z.ai API — GLM-5.1 |
| 数据校验 | Pydantic v2 |
| 配置解析 | PyYAML |

## 项目结构

```
app/
├── pyproject.toml              # 项目依赖
├── config.yaml                 # 配置文件
├── hooks.example.json          # Hook JSON 空白模板
├── README.md
├── daily/                      # 开发日志
│   ├── Agent项目构建-day1.md
│   ├── Agent项目构建-day2.md
│   ├── Agent项目构建-day3.md
│   └── Day4 后台任务执行+懒加载+添加日志+重构config.md
├── configs/                    # 扩展配置目录
└── internal/
    ├── conversation_log.py     # 全局 loguru JSONL 实时日志
    └── Agent/
        ├── start.py            # CLI 入口（53行精简版）
        ├── base_agent.py       # Agent 类（ReAct 循环 + 日志埋点）
        ├── config.py           # Pydantic 配置模型 + 三级加载
        ├── llm_config.py       # LLM 客户端配置
        ├── hooks.py            # Hook 加载、匹配、命令协议与结果合并
        ├── system.py           # 系统提示词（动态时间）
        ├── tools/
        │   ├── __init__.py     # 工具注册入口
        │   ├── base.py         # BaseTool 抽象基类 + safe_path
        │   ├── registry.py     # ToolRegistry 工具注册表
        │   ├── bash.py         # Shell 命令执行
        │   ├── read.py         # 文件读取
        │   ├── write.py        # 文件写入
        │   ├── edit.py         # 字符串替换编辑
        │   ├── task.py         # 任务管理（创建/查询/列表/更新）
        │   ├── skill.py        # 技能加载器
        │   ├── sub_agent.py    # 子 Agent 委派
        │   ├── compact.py      # 三层对话压缩
        │   ├── background.py   # 后台任务管理（异步执行 + 通知队列）
        │   └── skills/         # 预置技能定义
        │       ├── pdf/
        │       ├── pptx/
        │       └── skill-creator/
        └── todolist.py         # 旧版 Todo（已弃用）
```

## 快速开始

### 1. 设置环境变量

```bash
export ZAI_API_KEY="your-api-key"
```

### 2. 安装依赖

```bash
uv sync
```

### 3. 启动 Agent

```bash
uv run python internal/Agent/start.py
```

启动后进入交互式 REPL，输入问题即可与 Agent 对话，输入 `q` 或 `exit` 退出。

### 实时日志

日志会自动初始化，无论从 CLI、`Agent` 类还是 `ToolRegistry` 进入，都无需额外传递
logger。默认同时输出到终端并同步写入：

```text
<project-root>/logs/sessions/session_<UTC时间>-<进程ID>.jsonl
```

每次 LLM 或工具调用都会依次产生 `*_started`、`*_completed` 或 `*_failed`
事件，并记录 `request_id`/`call_id`、耗时和 token usage。持久化 sink 对每条日志
执行 `flush`，默认继续执行 `fsync` 强制同步到磁盘，
运行过程中可直接执行 `tail -f` 查看：

```bash
tail -f logs/sessions/session_*.jsonl
```

可在 `config.yaml` 中设置 `log.level`、`log.console` 和 `log.fsync`（也可通过
`AGENT_LOG_LEVEL`、`AGENT_LOG_CONSOLE`、`AGENT_LOG_FSYNC` 覆盖）；关闭终端输出
不会影响 JSONL 实时落盘。其他 Agent 模块也可直接记录自定义结构化事件：

```python
from internal.conversation_log import get_logger

get_logger().info("custom_event", {"task_id": "123"})
```

## 工具列表

| 工具名 | 源文件 | 说明 |
|--------|--------|------|
| `bash` | `tools/bash.py` | 执行 Shell 命令，内置危险命令黑名单，默认 120s 超时 |
| `read` | `tools/read.py` | 读取文件内容，支持行数限制，超 50K 字符自动截断 |
| `write` | `tools/write.py` | 写入/创建文件，自动创建父目录 |
| `edit` | `tools/edit.py` | 字符串替换编辑，精确匹配首处出现 |
| `task_create` | `tools/task.py` | 创建新任务（标题 + 描述） |
| `task_get` | `tools/task.py` | 查询指定任务详情 |
| `task_list` | `tools/task.py` | 列出所有任务及状态标记 |
| `task_update` | `tools/task.py` | 更新任务状态/依赖关系，completed 时自动解锁后续任务 |
| `skill` | `tools/skill.py` | 加载并执行预置技能 |
| `delegate` | `tools/sub_agent.py` | 将子任务委派给独立子 Agent |
| `compact` | `tools/compact.py` | 压缩对话历史以释放上下文空间 |
| `background` | `tools/background.py` | 后台异步执行耗时 Shell 命令，支持查询任务状态和结果 |

## 架构说明

### ReAct 循环

Agent 在 `base_agent.py` 中实现经典的 ReAct 模式：

1. **推理** — LLM 接收完整消息历史 + 工具定义，决定下一步行动
2. **行动** — 若 LLM 请求工具调用，通过 `ThreadPoolExecutor` 并行执行所有调用
3. **观察** — 工具结果追加到消息历史，回到步骤 1
4. **终止** — 当 LLM 直接返回文本回复（`finish_reason == "stop"`）时循环结束

### 工具注册机制

- **BaseTool**（`tools/base.py`）：抽象基类，子类只需定义 `name` 属性和 `execute()` 方法
- **自动 Schema 生成**：从 `execute()` 的函数签名和 docstring 自动生成 OpenAI tool definition
- **ToolRegistry**（`tools/registry.py`）：统一管理工具注册、查找和调用

### 生命周期 Hook

Hook 定义保存在独立 JSON 文件中，主配置只负责指定它的位置和运行时默认值：

```yaml
hooks:
  config_path: "hooks.json"
  default_timeout: 10
  stop_max_continuations: 5
```

`config_path` 为空时功能关闭；相对路径以 `config.yaml` 所在目录为基准。配置了不存在或非法的 JSON 文件时，Agent 会在启动阶段报错，避免权限 Hook 被静默跳过。

JSON 采用 Claude Code 风格的事件分组结构。例如，在 `bash`、`write` 或 `edit` 执行前运行权限脚本：

```json
{
  "hooks": {
    "UserPromptSubmit": [],
    "PreToolUse": [
      {
        "matcher": "bash|write|edit",
        "hooks": [
          {
            "type": "command",
            "command": "python scripts/check_tool.py",
            "timeout": 10,
            "on_error": "block"
          }
        ]
      }
    ],
    "PostToolUse": [],
    "Stop": []
  }
}
```

`matcher` 是对完整工具名执行的 Python 正则匹配；省略或为空表示匹配全部。匹配的 Hook 按 JSON 中的顺序串行执行，前一个 Hook 的输入改写会传递给后一个 Hook。不同工具通过 Pre Hook 后仍保持并行执行，Post Hook 再按原始工具调用顺序处理。

命令在 Agent 工作目录中运行，从 stdin 接收 JSON，并在 stdout 返回 JSON。所有事件输入都包含 `hook_event_name` 和 `cwd`，事件字段如下：

| 事件 | stdin 附加字段 | `hookSpecificOutput` 控制字段 |
|------|----------------|--------------------------------|
| `UserPromptSubmit` | `prompt` | `decision`、`reason`、`updatedPrompt`、`additionalContext` |
| `PreToolUse` | `tool_name`、`tool_input`、`tool_use_id` | `permissionDecision`、`permissionDecisionReason`、`updatedInput`、`additionalContext` |
| `PostToolUse` | 工具字段及 `tool_output` | `decision`、`reason`、`updatedOutput`、`additionalContext` |
| `Stop` | `assistant_message`、`continuation_count` | `decision`、`reason`、`additionalContext` |

例如，PreToolUse Hook 可以修改参数并注入上下文：

```json
{
  "systemMessage": "权限检查完成",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "命令安全",
    "updatedInput": {
      "command": "git status"
    },
    "additionalContext": "当前仓库处于受保护分支"
  }
}
```

退出码约定：

- `0`：解析 stdout；stdout 为空表示无修改放行。
- `2`：阻断当前事件，原因读取 stderr。
- 其他非零退出码、超时或非法 stdout：默认记录错误后放行；配置 `on_error: "block"` 时改为阻断。

`Stop` 返回 `decision: "block"` 会把 `reason` 注入对话并强制 LLM 继续，单次 `Agent.run()` 默认最多续跑 5 次。UserPromptSubmit 只处理真实用户输入；其余三类 Hook 对主 Agent 和子 Agent 都生效。

### 三层压缩策略

| 层级 | 触发方式 | 行为 |
|------|---------|------|
| micro | 被动，每轮自动执行 | 将旧 tool_result 替换为占位符，保留近 3 轮 |
| auto | LLM 调用 compact 工具 | 保存完整对话到磁盘，LLM 生成摘要替代历史 |
| manual | auto_compact 函数 | 保存对话并请求 LLM 生成摘要 |

### 子 Agent 委派

- 父 Agent 通过 `delegate` 工具生成子 Agent
- 子 Agent 拥有除 `delegate` 外的所有工具（防止无限递归）
- 子 Agent 独立运行至完成后返回摘要（截断至 8000 字符）

## 安全机制

- **路径沙箱** — `safe_path()` 函数将所有文件操作限制在工作区（`WORKDIR = cwd().parent`）内，防止路径穿越
- **命令黑名单** — bash 工具内置危险命令过滤
- **后台并发控制** — `threading.Semaphore` 限制最大并发后台任务数，`threading.Lock` 保证线程安全
- **结果截断** — 文件读取和子 Agent 返回均有长度限制

## 开发日志

| 日期 | 主题 |
|------|------|
| Day 1 | [基础框架 — 双循环架构 + 装饰器工具系统](daily/Day 1：Agent 基础框架搭建.md) |
| Day 2 | [重构 Agent 类 + 并行执行 + Todo + 子 Agent](daily/Day 2：工具完善 + Agent 类封装 + 子代理委派.md) |
| Day 3 | [BaseTool ABC 重构 + 任务持久化 + 三层压缩](daily/Day 3：工具重构 + 压缩策略 + Task 任务管理.md) |
| Day 4 | [后台任务 + 配置中心化 + 会话日志 + 架构重构](daily/Day4 后台任务执行+懒加载+添加日志+重构config.md) |
| Day 5 | [全局实时日志 + 三阶段权限认证](<daily/Day5 全局实时日志 + 三阶段权限认证.md>) |
