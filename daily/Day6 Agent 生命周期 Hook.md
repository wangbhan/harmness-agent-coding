# Day 6：可配置 Agent 生命周期 Hook

> 日期：2026-07-23
> 涉及文件：`internal/Agent/hooks.py`、`internal/Agent/base_agent.py`、`internal/Agent/config.py`、`internal/Agent/tools/__init__.py`、`internal/conversation_log.py`、`config.yaml`、`hooks.example.json`、`README.md`、`docs/superpowers/plans/2026-07-22-agent-hooks.md`、`tests/test_hooks.py`、`tests/test_agent_hooks.py`、`tests/test_activity_logging.py`

## 一、项目结构变更

相比 Day 5，本轮在 Agent 主循环的四个关键边界加入了可配置命令 Hook，并将 Hook 配置、协议执行、生命周期接入和审计日志拆分到清晰的模块边界中：

```text
app/
├── config.yaml                         # ⬆️ Hook 配置入口和运行时默认值
├── hooks.example.json                  # 🆕 四类事件的安全空模板
├── README.md                           # ⬆️ Hook 配置与命令协议文档
├── tests/
│   ├── test_hooks.py                   # 🆕 配置、匹配、协议和失败语义测试
│   ├── test_agent_hooks.py             # 🆕 四类 Hook 的 Agent 集成测试
│   └── test_activity_logging.py        # ⬆️ Hook 生命周期日志测试
└── internal/
    ├── conversation_log.py             # ⬆️ Hook 开始、完成、失败、阻断日志
    └── Agent/
        ├── hooks.py                    # 🆕 Hook 加载、匹配、执行和结果合并
        ├── base_agent.py               # ⬆️ 接入四个生命周期事件
        ├── config.py                   # ⬆️ HooksConfig + 活动配置目录
        └── tools/
            └── __init__.py             # ⬆️ 子 Agent 跳过 UserPromptSubmit
```

**本轮核心变更**：

- 新增 `UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop` 四类生命周期事件
- Hook 定义使用独立 JSON 文件，YAML 只保存文件路径和默认运行参数
- 新增 `HookManager`，统一负责配置校验、正则匹配、子进程执行和结果合并
- Hook 命令通过 stdin/stdout 交换 UTF-8 JSON，兼容 Claude Code 风格输出结构
- Pre Hook 可拒绝工具或改写参数，Post Hook 可审查、替换工具结果
- Stop Hook 可要求 Agent 继续工作，并通过最大续跑次数防止死循环
- 保留多工具并行执行，同时保证 Pre/Post Hook 和消息追加顺序确定
- Hook 超时时终止整个进程树，避免子进程脱离父 Hook 后继续运行
- 新增 Hook 生命周期审计事件，且不把原始输入、输出和环境变量复制进日志

---

## 二、Hook 整体架构

### 2.1 为什么需要生命周期 Hook

Day 5 已经提供 Bash 命令权限认证，但权限逻辑仍然属于 Agent 内部实现。生命周期 Hook 提供了一个更通用的外部扩展边界，使使用者无需修改 Agent 源码，也能在关键阶段接入自定义脚本：

| 场景 | 对应事件 | 可实现能力 |
|------|----------|------------|
| 用户输入进入模型前 | `UserPromptSubmit` | 内容校验、提示词改写、上下文注入 |
| 工具真正执行前 | `PreToolUse` | 权限判断、参数修正、策略检查 |
| 工具返回结果后 | `PostToolUse` | 结果脱敏、输出改写、内容阻断 |
| Agent 准备结束时 | `Stop` | 完成度检查、强制继续、补充要求 |

Hook 命令是独立进程，因此策略脚本可以由 Python、Shell 或其他可执行程序实现，只要遵循约定的 JSON 协议。

### 2.2 模块职责

```text
config.yaml
    │ 指定 hooks.json 路径与默认值
    ▼
internal/Agent/config.py
    │ 解析 HooksConfig，保存 config.yaml 所在目录
    ▼
internal/Agent/hooks.py
    ├─ 加载并严格校验 JSON
    ├─ 编译 matcher 正则
    ├─ 按配置顺序执行命令
    ├─ 解析和合并 HookResult
    └─ 输出结构化生命周期日志
    │
    ▼
internal/Agent/base_agent.py
    ├─ 在四个生命周期边界触发 Hook
    ├─ 应用输入/输出改写
    ├─ 保持工具并行执行
    └─ 维护合法的消息顺序
```

`hooks.py` 不直接调用 LLM、不执行注册工具，也不直接修改对话历史；它只接收事件载荷并返回结构化 `HookResult`。真正如何处理拒绝、改写和上下文，由 `base_agent.py` 决定。

### 2.3 核心数据模型

```python
class HookEvent(str, Enum):
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"


@dataclass(frozen=True)
class CommandHook:
    command: str
    timeout: int
    on_error: Literal["allow", "block"]


@dataclass(frozen=True)
class HookGroup:
    matcher: re.Pattern[str] | None
    hooks: tuple[CommandHook, ...]


@dataclass(frozen=True)
class HookResult:
    blocked: bool = False
    reason: str = ""
    updated_prompt: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_output: str | None = None
    additional_context: tuple[str, ...] = ()
    system_message: str = ""
```

这些定义均不可变，加载后的规则不会在多个 Agent 或工具线程之间被意外修改。

---

## 三、配置加载与严格校验

### 3.1 YAML 主配置

主配置只负责启用 Hook 并提供运行时默认值：

```yaml
hooks:
  # 为空时禁用；相对路径以 config.yaml 所在目录为基准
  config_path: "hooks.json"
  # 单个 Hook 未单独声明 timeout 时使用
  default_timeout: 10
  # Stop Hook 在一次 Agent.run() 中最多强制续跑次数
  stop_max_continuations: 5
```

对应的 Pydantic 模型为：

```python
class HooksConfig(BaseModel):
    config_path: str = ""
    default_timeout: int = Field(default=10, gt=0)
    stop_max_continuations: int = Field(default=5, ge=0)
```

因此默认超时必须大于 0，Stop 最大续跑次数不能为负数。

### 3.2 独立 JSON 配置

Hook 定义不直接写进 `config.yaml`，而是保存为独立 JSON：

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

当前只支持 `type: "command"`。每条命令可覆盖 `timeout`，也可通过 `on_error` 指定运行异常时放行还是阻断；未设置时分别使用 YAML 默认超时和 `allow`。

仓库根目录的 `hooks.example.json` 默认四类事件均为空，复制并启用后不会意外执行任何命令。

### 3.3 路径解析

`init_config()` 会记录实际加载的 `config.yaml` 所在目录，`get_config_dir()` 将该目录提供给 Hook 加载器：

```text
config_path 为空     → Hook 功能关闭，不读取文件
config_path 为绝对路径 → 直接使用该绝对路径
config_path 为相对路径 → <config.yaml 所在目录>/<config_path>
```

这样即使进程从其他工作目录启动，Hook 配置路径也不会随当前 shell 的 `cwd` 漂移。

### 3.4 启动期校验

配置启用后，以下错误会直接抛出，而不是静默跳过：

- 文件不存在或不是普通文件
- JSON 语法非法，或根对象缺少对象字段 `hooks`
- 包含未知事件名
- 事件分组、Hook 列表或 Hook 条目类型错误
- `matcher` 不是字符串或不是合法正则
- Hook 类型不是 `command`
- `command` 为空
- `timeout` 不是正整数
- `on_error` 不是 `allow` 或 `block`

对安全策略而言，错误配置直接失败比“启动成功但实际没有执行权限 Hook”更可靠。

### 3.5 matcher 匹配规则

`matcher` 只对工具事件生效，并使用 `re.fullmatch()` 匹配完整工具名：

```python
matcher = re.compile("bash|write|edit")
matcher.fullmatch("bash")       # 匹配
matcher.fullmatch("bash_extra") # 不匹配
```

| 事件 | matcher 行为 |
|------|--------------|
| `PreToolUse` / `PostToolUse` | 空 matcher 匹配所有工具；非空 matcher 完整匹配工具名 |
| `UserPromptSubmit` / `Stop` | 只执行未设置 matcher 的分组 |

匹配的分组和命令始终按 JSON 中的声明顺序展开。

---

## 四、命令 Hook 协议

### 4.1 子进程调用方式

每条 Hook 命令都在 Agent 工作目录中启动：

```python
process = subprocess.Popen(
    hook.command,
    shell=True,
    cwd=self.workdir,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,  # POSIX
)
```

Hook 继承 Agent 进程的环境变量，通过 stdin 接收一个 UTF-8 JSON 对象，通过 stdout 返回一个 UTF-8 JSON 对象。命令配置属于受信任的本地控制面；业务输入不应被拼接进 `command` 字符串，而应从 stdin JSON 读取。

### 4.2 通用输入字段

所有事件输入都包含：

```json
{
  "hook_event_name": "PreToolUse",
  "cwd": "/absolute/agent/workdir"
}
```

事件专属字段如下：

| 事件 | stdin 字段 |
|------|------------|
| `UserPromptSubmit` | `prompt` |
| `PreToolUse` | `tool_name`、`tool_input`、`tool_use_id` |
| `PostToolUse` | `tool_name`、`tool_input`、`tool_use_id`、`tool_output` |
| `Stop` | `assistant_message`、`continuation_count` |

例如，PreToolUse Hook 收到的完整输入类似：

```json
{
  "hook_event_name": "PreToolUse",
  "cwd": "/workspace/project",
  "tool_name": "bash",
  "tool_input": {
    "command": "git status"
  },
  "tool_use_id": "call_123"
}
```

### 4.3 输出信封

stdout 非空时必须返回对象，并包含与当前事件一致的 `hookSpecificOutput.hookEventName`：

```json
{
  "systemMessage": "权限检查完成",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "命令安全",
    "updatedInput": {
      "command": "git status --short"
    },
    "additionalContext": "当前仓库处于受保护分支"
  }
}
```

`systemMessage` 是可选诊断字段，会进入 `hook_completed` 日志，但不会自动注入对话；真正提供给 LLM 的附加信息使用 `additionalContext`。

### 4.4 各事件输出字段

| 事件 | 决策字段 | 改写字段 | 其他字段 |
|------|----------|----------|----------|
| `UserPromptSubmit` | `decision: allow/block` | `updatedPrompt` | `reason`、`additionalContext` |
| `PreToolUse` | `permissionDecision: allow/deny` | `updatedInput` | `permissionDecisionReason`、`additionalContext` |
| `PostToolUse` | `decision: allow/block` | `updatedOutput` | `reason`、`additionalContext` |
| `Stop` | `decision: allow/block` | — | `reason`、`additionalContext` |

已知字段会进行严格类型校验，例如 `updatedInput` 必须是对象，`updatedOutput` 必须是字符串，事件名不一致也会被视为协议错误。

### 4.5 串行执行与链式改写

同一事件匹配到多个 Hook 时，它们按配置顺序串行执行。前一个 Hook 的改写会成为后一个 Hook 的输入：

```text
原始 prompt / input / output
        │
        ▼
Hook A 改写内容 + context A
        │ 将改写后的 payload 传给下一条
        ▼
Hook B 再次改写 + context B
        │
        ▼
合并后的 HookResult
```

`additionalContext` 按顺序累积。一旦某条 Hook 阻断，后续 Hook 不再执行，但之前已经产生的改写和上下文仍保留在返回结果中。

---

## 五、四类生命周期事件接入

### 5.1 UserPromptSubmit：用户输入进入 LLM 前

`Agent.run()` 首先检查最后一条消息是否为字符串类型的用户消息，然后执行 UserPromptSubmit：

```text
用户消息
   │
   ▼
UserPromptSubmit
   ├─ allow + updatedPrompt → 替换原始用户输入
   ├─ allow + context       → 在用户消息前插入 system 消息
   └─ block                 → 移除用户消息，不调用 LLM，返回拒绝原因
```

阻断时将刚追加的用户消息从 history 中移除，避免被拒绝的输入残留在下一轮上下文里。

子 Agent 的任务文本由父 Agent 内部生成，不属于真实用户提交。`setup_delegate()` 因此使用：

```python
sub_agent = Agent(
    ...,
    process_user_prompts=False,
)
```

这只跳过子 Agent 的 `UserPromptSubmit`；其 PreToolUse、PostToolUse 和 Stop 仍正常执行。

### 5.2 PreToolUse：工具执行前

LLM 一轮可能同时返回多个工具调用。Agent 先按原始顺序逐个执行 Pre Hook：

```text
tool_call 1 ──Pre──┐
tool_call 2 ──Pre──┼─ 收集允许执行的调用 ─→ ThreadPoolExecutor 并行执行
tool_call 3 ──Pre──┘
```

Pre Hook 的处理结果：

| 结果 | Agent 行为 |
|------|------------|
| `allow` | 使用原始参数执行工具 |
| `allow + updatedInput` | 使用改写后的参数执行工具 |
| `deny` | 不调用工具，以拒绝原因作为该次工具结果 |
| `additionalContext` | 收集后在本轮所有 tool 消息之后统一注入 |

Pre Hook 本身串行，获准的工具仍然并行执行，因此权限检查顺序确定，同时不牺牲原有工具并发能力。

### 5.3 PostToolUse：工具执行后

并行工具执行完成后，Agent 再按 LLM 原始调用顺序执行 Post Hook：

```text
并行工具执行完成
        │
        ▼
按 tool_call 原始顺序执行 Post Hook
        ├─ allow                → 保留原输出
        ├─ allow + updatedOutput → 替换输出
        └─ block                → 用阻断原因替换输出
```

只有通过 Pre Hook 并实际执行过的工具才会进入 Post Hook。Post Hook 的阻断发生在工具执行之后，因此它只能阻止结果进入模型，不能回滚工具已经产生的外部副作用。

### 5.4 Stop：Agent 准备结束时

当 LLM 返回 `finish_reason == "stop"` 时，Agent 不会立即退出，而是先执行 Stop Hook：

```text
LLM 返回最终回答
        │
        ▼
Stop Hook
   ├─ allow → 输出回答并结束
   └─ block → reason + additionalContext 作为 system 消息
                  │
                  ▼
             再调用一次 LLM
```

Stop Hook 可以用于检查任务是否真正完成。例如发现测试未运行时，它可以阻断结束并要求 Agent 继续验证。

为避免配置错误导致无限循环，`stop_max_continuations` 限制单次 `Agent.run()` 的强制续跑次数。达到上限后，即使 Hook 再次阻断，Agent 也会记录 `stop_hook_continuation_limit` 警告并正常返回当前回答。

---

## 六、并发执行与消息顺序

### 6.1 完整工具调用时序

```text
LLM 返回 N 个 tool_calls
        │
        ▼
Pre Hook：按原始顺序串行
        │
        ├─ denied → 直接生成拒绝结果
        └─ approved
              │
              ▼
      approved tools 并行执行
              │
              ▼
Post Hook：按原始顺序串行
        │
        ▼
所有 tool 消息按原始顺序追加
        │
        ▼
合并 additionalContext，追加一条 system 消息
        │
        ▼
下一轮 LLM 调用
```

这里同时满足三个约束：

1. Hook 的执行和副作用顺序可预测
2. 获准工具继续使用 `ThreadPoolExecutor` 并行运行
3. assistant tool call 后紧跟对应的全部 tool 消息，保持 OpenAI 消息协议合法

### 6.2 结果顺序与上下文位置

工具线程按完成时间返回，但结果先写入 `call_id → output` 映射，最终仍按原始 `tool_calls` 顺序追加。

Hook 上下文不能插入 assistant tool call 与 tool result 之间，否则会破坏消息结构。因此所有 Pre/Post Hook 的 `additionalContext` 最后合并为一条 system 消息，放在本轮全部 tool 消息之后。

### 6.3 compact 工具的特殊处理

`compact` 会触发整段对话压缩，因此必须确认它确实执行成功：

```python
if (
    block.function.name == "compact"
    and block.id in approved_ids
):
    compact_called = True
```

如果 compact 被 Pre Hook 拒绝，只会向模型返回拒绝原因，不会误触发 `auto_compact()`。这避免了“权限检查已拒绝，但压缩副作用仍然发生”的隔离问题。

---

## 七、失败语义与进程隔离

### 7.1 退出码约定

| 进程结果 | 行为 |
|----------|------|
| 退出码 `0`，stdout 为空 | 无修改放行 |
| 退出码 `0`，stdout 非空 | 严格解析 JSON 输出并应用决策 |
| 退出码 `2` | 始终阻断，原因读取 stderr；为空时使用默认原因 |
| 其他非零退出码 | 作为运行错误，按 `on_error` 处理 |
| 超时、启动失败、非法 UTF-8、非法 JSON | 作为运行错误，按 `on_error` 处理 |

退出码 `2` 是显式拒绝通道，优先级高于 stdout 解码。即使 Hook 在 stdout 或 stderr 写入了非法 UTF-8，只要退出码为 `2`，事件仍然会被阻断，而不会因为默认 fail-open 意外放行。

### 7.2 fail-open 与 fail-closed

```json
{
  "type": "command",
  "command": "python scripts/check.py",
  "on_error": "block"
}
```

| `on_error` | 运行错误后的结果 | 适用场景 |
|------------|------------------|----------|
| `allow`（默认） | 记录失败并继续 | 非关键提示、格式化、辅助信息 |
| `block` | 记录失败并阻断事件 | 权限、安全、合规检查 |

显式业务决策与运行故障是两种不同语义：退出码 `2` 或合法 JSON 中的 deny/block 一定阻断；只有脚本崩溃、超时或协议非法等运行故障才读取 `on_error`。

### 7.3 超时终止整个进程树

如果只终止 shell 父进程，它启动的子进程仍可能继续运行并产生副作用。本轮使用独立进程组隔离每条 Hook：

```text
POSIX   → start_new_session=True → os.killpg(..., SIGKILL)
Windows → CREATE_NEW_PROCESS_GROUP → taskkill /F /T /PID
```

超时后先终止整个进程树，再通过 `communicate()` 回收父进程，防止僵尸进程和遗留子任务。

### 7.4 错误信息边界

Hook 错误和阻断原因最多保留 1000 个字符，避免外部脚本输出无限放大日志或对话。非法 UTF-8 会以协议错误处理；中文等正常 Unicode 输入输出则统一通过显式 UTF-8 编解码传递。

---

## 八、Hook 生命周期日志

### 8.1 新增事件

HookManager 复用 Day 5 的全局实时日志系统：

| 日志事件 | 级别 | 关键字段 |
|----------|------|----------|
| `hook_started` | INFO | Hook 事件名、command |
| `hook_completed` | INFO | command、耗时、退出码、allow/block、systemMessage |
| `hook_failed` | ERROR | command、耗时、错误、退出码、是否超时 |
| `hook_blocked` | WARNING | Hook 事件名、阻断原因 |

例如一次成功检查会产生：

```text
hook_started
    │
    ▼
执行 command
    │
    ├─ 成功 → hook_completed
    ├─ 故障 → hook_failed
    └─ 阻断 → hook_completed/hook_failed + hook_blocked
```

### 8.2 敏感数据边界

生命周期日志只记录执行元数据，不复制以下内容：

- 发送给 Hook 的完整 stdin payload
- Hook 的原始 stdout / stderr
- Hook 继承的环境变量
- 工具参数、工具结果或用户 prompt 的完整副本

这既保留可观测性，也避免 Hook 日志再制造一份潜在的敏感数据副本。command、诊断消息和错误摘要仍会记录，因此 Hook 脚本也不应主动把密钥写进这些字段。

### 8.3 禁用时不初始化日志

`get_hook_manager()` 使用进程级单例和线程锁进行懒加载。只有 `hooks.config_path` 非空时才获取全局 logger：

```text
Hook 关闭 → 创建空 HookManager，不初始化日志
Hook 开启 → 初始化 logger，加载并校验 JSON
```

这样默认关闭 Hook 时，不会仅因为构造 Agent 就额外创建日志文件。

---

## 九、完整调用链

以“用户要求执行 bash，Pre Hook 修改命令，Post Hook 脱敏结果，Stop Hook 检查完成度”为例：

```text
真实用户输入
    │
    ▼
UserPromptSubmit
    ├─ 可改写 prompt
    └─ 可注入 system context
    │
    ▼
LLM 返回 bash tool_call
    │
    ▼
PreToolUse(bash)
    ├─ deny → 不执行 bash
    └─ allow / updatedInput
              │
              ▼
       ToolRegistry.call(bash)
              │
              ▼
       BashTool.execute(command)
              │
              ▼
PostToolUse(bash)
    ├─ 保留结果
    ├─ updatedOutput 替换结果
    └─ block 用原因覆盖结果
    │
    ▼
tool 消息 + Hook additionalContext
    │
    ▼
LLM 返回最终回答
    │
    ▼
Stop
    ├─ allow → Agent 返回
    └─ block → 注入继续要求，再次调用 LLM（受次数上限保护）
```

四类事件覆盖了“一次 Agent 任务从进入、行动、观察到退出”的完整生命周期。

---

## 十、测试与验证

### 10.1 单元测试覆盖

`tests/test_hooks.py` 主要验证 Hook 基础设施：

| 范围 | 验证内容 |
|------|----------|
| 配置 | 默认值、相对/绝对路径、参数边界、禁用状态 |
| 加载 | 文件缺失、非法 JSON、未知事件、非法 matcher 和字段类型 |
| 匹配 | 完整工具名匹配、分组顺序、非工具事件忽略 matcher 分组 |
| 协议 | 四类载荷、四类决策字段、链式改写、上下文合并 |
| 编码 | 中文 UTF-8 输入输出、非法 UTF-8 失败策略 |
| 退出码 | 空输出放行、退出码 2 强制阻断、其他非零退出码 |
| 故障 | 启动失败、超时、非法 JSON、`on_error` 策略 |
| 隔离 | 超时后终止 Hook 子进程，不留下后台副作用 |

`tests/test_agent_hooks.py` 主要验证 Agent 集成行为：

| 测试场景 | 验证内容 |
|----------|----------|
| Prompt 改写 | 第一次 LLM 请求前已应用新 prompt 和 context |
| Prompt 阻断 | 不调用 LLM，并从 history 移除用户消息 |
| 子 Agent | 内部委派跳过 UserPromptSubmit |
| Stop 续跑 | 阻断原因注入对话并再次调用 LLM |
| Stop 上限 | 默认最多强制续跑 5 次 |
| 工具编排 | Pre/Post 按原始顺序，工具本身保持并行 |
| compact 拒绝 | 被拒绝的 compact 不触发自动压缩 |
| Post 阻断 | 阻断原因替换实际工具输出 |

`tests/test_activity_logging.py` 额外验证 Hook 日志生命周期完整，并确认敏感 payload、stdout 和环境变量不会出现在 Hook 审计事件中。

### 10.2 验证命令

```bash
env AGENT_LOG_CONSOLE=false uv run python -m unittest discover -s tests
```

当前结果：

```text
Ran 44 tests

OK
```

---

## 十一、对应 Git 提交

本轮功能按职责拆分为连续提交：

| 提交 | 内容 |
|------|------|
| `c88028e` | 编写可配置 Agent Hook 实施计划 |
| `1611232` | 增加 YAML Hook 配置模型和活动配置目录 |
| `aac15d6` | 实现 Hook JSON 加载、匹配和命令协议 |
| `ff9aadd` | 将四类 Hook 接入 Agent 生命周期 |
| `685e999` | 增加安全的 Hook 生命周期日志 |
| `1061249` | 补充 README 配置和协议说明 |
| `fdfe4dd` | 完善进程隔离、UTF-8、失败策略和 compact 拒绝语义 |

---

## 十二、后续规划（TODO）

- [ ] **更多 Hook 类型**：在 `command` 之外支持 HTTP、本地 Python callable 等受控执行器
- [ ] **环境变量白名单**：从继承全部环境变量升级为可配置的最小环境集合
- [ ] **输出大小限制**：对子进程 stdout/stderr 增加硬限制，防止异常脚本占用过多内存
- [ ] **异步 Hook**：为纯审计类 Hook 提供不阻塞主流程的异步模式
- [ ] **配置热重载**：在不重启 Agent 的情况下安全刷新 HookManager
- [ ] **独立审计 sink**：将 Hook 安全事件与普通活动日志分离并设置不同保留周期
- [ ] **Post Hook 回滚协议**：为具有事务能力的工具设计可选补偿或回滚接口
- [ ] **内置策略示例**：补充 prompt 校验、命令审批、结果脱敏和完成度检查脚本
- [ ] **Hook 级并发策略**：允许无副作用的检查型 Hook 并行执行，同时保留确定性合并规则
