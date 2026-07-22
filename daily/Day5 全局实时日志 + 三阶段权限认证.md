# Day 5：全局实时日志 + 三阶段权限认证

> 日期：2026-07-22
> 涉及文件：`internal/conversation_log.py`、`internal/Agent/auth.py`、`internal/Agent/base_agent.py`、`internal/Agent/start.py`、`internal/Agent/config.py`、`internal/Agent/tools/registry.py`、`internal/Agent/tools/bash.py`、`internal/Agent/tools/background.py`、`config.yaml`、`tests/test_activity_logging.py`

## 一、项目结构变更

相比 Day 4，本轮将日志从 Agent 私有模块升级为 `internal` 下的项目级全局能力，同时完善 Bash 命令的三阶段权限认证：

```text
app/
├── config.yaml                         # ⬆️ 日志开关 + 权限规则配置
├── tests/
│   └── test_activity_logging.py        # 🆕 实时日志生命周期测试
└── internal/
    ├── conversation_log.py             # 🆕 项目级 Loguru 全局实时日志
    └── Agent/
        ├── auth.py                     # 🆕 三阶段命令权限认证
        ├── base_agent.py               # ⬆️ LLM 调用生命周期埋点
        ├── config.py                   # ⬆️ 日志及权限配置模型
        ├── start.py                    # ⬆️ 日志初始化与会话收尾
        └── tools/
            ├── registry.py             # ⬆️ 工具调用生命周期统一埋点
            ├── bash.py                 # ⬆️ 前台命令接入权限认证
            └── background.py           # ⬆️ 后台命令接入认证及日志
```

**本轮核心变更**：

- `internal/conversation_log.py`：提供进程级全局日志、JSONL 持久化 sink、逐条 flush/fsync、终端 sink 和结构化事件接口
- `base_agent.py`：实时记录 LLM 请求开始、完成、异常、耗时和 token usage
- `registry.py`：所有工具调用统一记录开始、结果、异常和执行耗时
- `background.py`：记录后台任务的开始、完成和失败状态
- `auth.py`：实现“拒绝列表 → 风险规则 → 人工审批”的三阶段认证
- `bash.py` / `background.py`：前台、后台命令共用同一套权限入口
- `config.py` / `config.yaml`：日志级别、控制台开关、危险命令和风险规则全部配置化

---

## 二、全局实时日志系统

Day 4 的日志位于 `internal/Agent/conversation_log.py`，更偏向 Agent 会话内部使用。本轮将其移动到 `internal/conversation_log.py`，使 Agent、工具、权限认证和其他 `internal` 模块共享同一个日志实例。

### 2.1 整体架构

```text
                        ┌────────────────────┐
                        │ get_logger()       │
                        │ 全局按需初始化      │
                        └─────────┬──────────┘
                                  │
                ┌─────────────────┼─────────────────┐
                │                 │                 │
                ▼                 ▼                 ▼
         Agent LLM 调用      ToolRegistry      权限/后台任务
         request/response    call/result       approve/deny
                │                 │                 │
                └─────────────────┼─────────────────┘
                                  ▼
                        ┌────────────────────┐
                        │ SessionLogger      │
                        │ 结构化事件统一出口  │
                        └─────────┬──────────┘
                                  │
                     ┌────────────┴────────────┐
                     ▼                         ▼
             JSONL 文件同步写入          stderr 终端实时输出
             完整结构化数据              单行摘要（最多 800 字符）
```

日志文件默认保存至：

```text
<project-root>/logs/sessions/session_<UTC时间>-<进程ID>.jsonl
```

文件名同时包含毫秒时间和进程 ID，降低并行启动多个 Agent 时的文件名冲突概率。

### 2.2 全局单例与按需初始化

模块通过 `_global_logger` 保存进程级实例，并使用 `threading.RLock` 保护首次初始化：

```python
_global_logger: SessionLogger | None = None
_logger_lock = threading.RLock()


def init_logger(level=None, *, log_dir=None, console=None) -> SessionLogger:
    global _global_logger
    with _logger_lock:
        if _global_logger is None:
            _global_logger = SessionLogger(
                level=level,
                log_dir=log_dir,
                console=console,
            )
        return _global_logger


def get_logger() -> SessionLogger:
    if _global_logger is None:
        return init_logger()
    return _global_logger
```

**设计特点**：

| 特性 | 说明 |
|------|------|
| 全局共享 | 所有模块通过 `get_logger()` 获取同一个实例，无需逐层传参 |
| 自动初始化 | 非 CLI 入口直接调用日志时也不会静默丢失事件 |
| 线程安全 | 多个工具线程同时首次访问时，由 `RLock` 串行化初始化 |
| 重复调用幂等 | 已初始化后再次调用 `init_logger()` 返回原实例 |
| 显式关闭 | `close_logger()` 完成最终刷盘、移除 sink 并清空全局实例 |

### 2.3 Loguru 实时写入策略

文件 sink 使用自定义 `_DurableFileSink`，并保持同步模式：

```python
class _DurableFileSink:
    def write(self, message: str) -> None:
        self._stream.write(message)

    def flush(self) -> None:
        self._stream.flush()
        if self._fsync:
            os.fsync(self._stream.fileno())


logger.add(
    self._disk_sink,
    level=self.level,
    format=_json_formatter,
    enqueue=False,
    catch=True,
)
```

| 参数 | 作用 |
|------|------|
| `enqueue=False` | 不进入异步队列，事件在当前调用中直接交给 sink |
| `flush()` | 每条事件写入后刷新 Python 文件缓冲区 |
| `fsync()` | 默认将文件状态继续同步到磁盘，可通过配置关闭以换取性能 |
| `catch=True` | sink 自身异常不会中断 Agent 主流程 |
| `logger.complete()` | 关闭会话时确保剩余日志完成处理 |

因此 `llm_call_started` 和 `tool_call_started` 会在实际调用前写入。运行期间无需等待 Agent 退出，即可读取或使用 `tail -f` 观察日志：

```bash
tail -f <project-root>/logs/sessions/session_*.jsonl
```

### 2.4 日志落盘与会话文件生命周期

日志不会只停留在终端或进程内存中。`SessionLogger` 初始化时会解析持久化目录、自动创建目录，并为当前进程生成独立的 JSONL 文件：

```python
resolved_log_dir = _resolve_log_dir(log_dir, cfg)
resolved_log_dir.mkdir(parents=True, exist_ok=True)

now = datetime.now(timezone.utc)
self.session_id = (
    now.strftime("session_%Y-%m-%dT%H-%M-%S-")
    + f"{now.microsecond // 1000:03d}-{os.getpid()}"
)
self.log_path = resolved_log_dir / f"{self.session_id}.jsonl"
```

#### 落盘目录解析

落盘目录由 `paths.workdir` 和 `paths.logs_dir` 共同决定：

```yaml
paths:
  workdir: ""
  logs_dir: "logs/sessions"
```

| 配置情况 | 实际日志目录 |
|----------|--------------|
| `init_logger(log_dir=...)` 显式传入 | 使用指定目录，主要用于测试或自定义入口 |
| `paths.workdir` 非空 | `<paths.workdir>/<paths.logs_dir>` |
| `paths.workdir` 为空 | `<project-root>/<paths.logs_dir>` |

默认目录结构如下：

```text
<project-root>/
└── logs/
    └── sessions/
        ├── session_2026-07-22T02-30-00-123-12345.jsonl
        ├── session_2026-07-22T03-10-18-456-12680.jsonl
        └── ...
```

每次启动创建一个新的会话文件，不覆盖之前的日志。当前实现没有自动删除历史会话，因此文件会跨进程重启保留，直到人工清理或后续配置轮转策略。

#### 单条事件落盘流程

```text
业务模块调用 get_logger().info()/tool_call()/llm_request()
                          │
                          ▼
                  SessionLogger._emit()
                          │
                          ▼
             loguru bind(event, data).log()
                          │
                ┌─────────┴─────────┐
                ▼                   ▼
       _json_formatter()     _console_formatter()
                │                   │
                ▼                   ▼
       JSONL 文件同步写入       stderr 实时展示
```

文件 sink 使用 `enqueue=False`。每次 `_emit()` 返回前，当前事件已经依次执行 `write → flush → fsync`；测试也会在 `close_logger()` 之前直接读取文件，确认日志在运行过程中可见。

#### 会话关闭与最终刷盘

CLI 使用 `try/finally` 保证即使用户中断也执行会话收尾：

```python
try:
    # Agent REPL
    ...
finally:
    get_logger().session_end(
        turn_count=len([m for m in history if m["role"] == "user"])
    )
    close_logger()
```

`close_logger()` 的内部顺序为：

```text
session_completed（由调用方记录）
        │
        ▼
logger_closed
        │
        ▼
logger.complete()  → 完成剩余日志处理
        │
        ▼
logger.remove()    → 移除当前会话 sink
        │
        ▼
清空全局实例，下一次调用可创建新会话文件
```

落盘后的日志可以直接查看：

```bash
# 查看最新生成的会话文件
ls -lt <project-root>/logs/sessions/

# 实时追踪当前会话
tail -f <project-root>/logs/sessions/session_*.jsonl

# 按事件筛选（已安装 jq 时）
jq 'select(.event == "command_denied")' \
  <project-root>/logs/sessions/session_*.jsonl
```

> 当前“实时落盘”默认逐条执行 `flush + fsync`。如果调用频率较高，可设置 `log.fsync: false`，保留实时文件写入但减少强制磁盘同步开销。日志轮转、压缩和保留天数仍属于后续增强项。

### 2.5 双 sink 输出

日志默认同时写入文件和终端：

| Sink | 格式 | 用途 |
|------|------|------|
| JSONL 文件 | 每行一个 JSON 对象，保留结构化字段 | 审计、检索、统计和故障复盘 |
| stderr 终端 | 时间、级别、事件、数据摘要 | 开发时实时观察 Agent 状态 |

终端摘要最长保留 800 个字符，避免工具输出过长导致终端被淹没；完整记录按日志配置中的长度限制写入 JSONL。

### 2.6 JSONL 结构

每条日志包含以下顶层字段：

| 字段 | 含义 |
|------|------|
| `timestamp` | 带时区的 ISO 8601 时间 |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `session_id` | 当前 Agent 会话标识 |
| `event` | 结构化事件名称 |
| `process_id` | 产生日志的进程 ID |
| `thread_id` | 产生日志的线程 ID，用于分析并行工具调用 |
| `data` | 不同事件对应的业务数据 |

示例：

```json
{"timestamp":"2026-07-22T10:30:00.123+08:00","level":"INFO","session_id":"session_2026-07-22T02-30-00-123-12345","event":"tool_call_started","process_id":12345,"thread_id":9981,"data":{"tool_name":"bash","call_id":"call_xxx","arguments_summary":"{\"command\":\"pwd\"}"}}
{"timestamp":"2026-07-22T10:30:00.145+08:00","level":"INFO","session_id":"session_2026-07-22T02-30-00-123-12345","event":"tool_call_completed","process_id":12345,"thread_id":9981,"data":{"tool_name":"bash","call_id":"call_xxx","duration_ms":21.8,"result_summary":"/workspace","result_length":10}}
```

### 2.7 日志事件清单

| 范围 | 事件 | 级别 | 关键数据 |
|------|------|------|----------|
| Logger | `logger_initialized` | INFO | `log_path` |
| Logger | `logger_closed` | INFO | — |
| Session | `session_started` | INFO | system prompt 长度和预览 |
| Session | `session_completed` | INFO | 用户轮次数 |
| Session | `user_input` | INFO | 用户输入 |
| LLM | `llm_call_started` | INFO | `request_id`、模型、消息数、工具数 |
| LLM | `llm_call_completed` | INFO | 完成原因、耗时、usage、是否含工具调用 |
| LLM | `llm_call_failed` | ERROR | 异常类型、异常信息、耗时 |
| Agent | `agent_reply` | INFO | 回复内容、原始长度 |
| Tool | `tool_call_started` | INFO | 工具名、`call_id`、参数摘要 |
| Tool | `tool_call_arguments` | DEBUG | 更完整的工具参数 |
| Tool | `tool_call_completed` | INFO | 耗时、结果摘要、结果长度 |
| Tool | `tool_call_result` | DEBUG | 更完整的工具结果 |
| Tool | `tool_call_failed` | ERROR | 异常类型、异常信息、耗时 |
| Background | `background_task_started` | INFO | 任务 ID、命令 |
| Background | `background_task_completed` | INFO | 状态、耗时、结果摘要 |
| Background | `background_task_failed` | ERROR | 状态、耗时、错误摘要 |
| Auth | `command_approved` | WARNING | 阶段、命令、命中原因 |
| Auth | `command_denied` | WARNING | 阶段、命令、拒绝原因 |

### 2.8 LLM 调用埋点

`Agent.run()` 在请求模型前创建唯一 `request_id` 并记录开始事件：

```python
request_id = f"req_{uuid.uuid4().hex}"
activity_log.llm_request(
    request_id=request_id,
    model=self.model,
    message_count=len(messages),
    tool_count=len(self.tools),
)
started_at = time.perf_counter()
```

请求成功后记录：

- `finish_reason`
- 是否包含工具调用
- 请求耗时 `duration_ms`
- `prompt_tokens`、`completion_tokens` 等 usage 数据

请求抛出异常时记录 `llm_call_failed`，随后继续抛出原异常，日志不会改变原有错误处理语义。

### 2.9 工具调用统一埋点

日志被放在 `ToolRegistry.call()` 中，而不是分散到每一个工具类：

```text
registry.call(name, arguments_json, call_id)
        │
        ├─ 立即记录 tool_call_started
        ├─ JSON 参数解析
        ├─ tool.execute(**kwargs)
        ├─ 成功：tool_call_completed + tool_call_result
        └─ 异常：tool_call_failed → 重新抛出
```

这样无论工具由父 Agent、子 Agent还是测试代码直接调用，都会经过同一套日志链路。并行工具调用使用 OpenAI 返回的 `call_id` 关联开始和结束事件；直接调用注册表时则自动生成 UUID。

### 2.10 日志配置

```yaml
log:
  level: "DEBUG"
  console: true
  fsync: true
  max_args_info: 500
  max_args_debug: 10000
  max_result_info: 500
  max_result_debug: 10000
  max_reply_len: 2000
```

支持的环境变量覆盖：

| 环境变量 | 对应配置 |
|----------|----------|
| `AGENT_LOG_LEVEL` | `log.level` |
| `AGENT_LOG_CONSOLE` | `log.console` |
| `AGENT_LOG_FSYNC` | `log.fsync` |

`AGENT_LOG_CONSOLE` 和 `AGENT_LOG_FSYNC` 支持 `1/0`、`true/false`、`yes/no`、`on/off`。关闭终端输出不会影响 JSONL 文件实时写入；关闭 fsync 则可以降低高频日志的磁盘同步开销。

其他内部模块可直接记录自定义事件：

```python
from internal.conversation_log import get_logger

get_logger().info("custom_event", {"task_id": "123"})
```

---

## 三、三阶段权限认证

Agent 的 Bash 工具可以执行真实系统命令，仅靠固定黑名单无法兼顾安全性和可用性。本轮将权限判断拆成三个阶段：明确危险的命令直接拒绝，具有风险但可能合理的命令交给用户确认，普通命令直接放行。

### 3.1 认证流程

```text
                      check_command(command)
                               │
                               ▼
                ┌─────────────────────────────┐
                │ Phase 1：拒绝列表子串匹配   │
                └──────────────┬──────────────┘
                               │
                   命中 ───────┴─────── 未命中
                    │                     │
                    ▼                     ▼
               直接拒绝          ┌───────────────────────┐
               allowed=False     │ Phase 2：风险正则匹配 │
                                  └───────────┬───────────┘
                                              │
                                  未命中 ─────┴───── 命中
                                    │                 │
                                    ▼                 ▼
                                直接放行       Phase 3：人工审批
                                allowed=True          │
                                                y ────┴──── 其他/中断
                                                │               │
                                                ▼               ▼
                                              放行             拒绝
```

### 3.2 AuthResult 返回模型

权限入口统一返回 `AuthResult`：

```python
@dataclass
class AuthResult:
    allowed: bool
    reason: str
```

| 字段 | 说明 |
|------|------|
| `allowed` | 是否允许继续执行命令 |
| `reason` | 放行时为空字符串；拒绝时包含阶段和具体原因 |

调用方不直接依赖认证内部实现，只需要判断 `allowed`：

```python
result = check_command(command)
if not result.allowed:
    return f"命令被拒绝：{result.reason}"
```

### 3.3 Phase 1：危险命令直接拒绝

Phase 1 使用配置中的 `dangerous_commands` 做字符串子串匹配：

```yaml
dangerous_commands:
  - "rm -rf /"
  - "sudo"
  - "reboot"
  - "shutdown"
```

```python
for blocked in cfg.dangerous_commands:
    if blocked in command:
        get_logger().warning("command_denied", {
            "phase": 1,
            "command": command,
            "keyword": blocked,
        })
        return AuthResult(
            allowed=False,
            reason=f"[Phase 1] 命令包含禁止关键字：{blocked!r}",
        )
```

这一阶段不弹出审批提示，避免用户误批准具有明确破坏性的命令。

### 3.4 Phase 2：风险规则识别

未命中拒绝列表的命令会进入正则规则匹配。规则由 `pattern + description` 组成：

```yaml
review_patterns:
  - pattern: '\brm\b'
    description: "删除文件（含工作目录外）"
  - pattern: '\bmv\b'
    description: "移动/重命名文件（可能影响工作目录外的文件）"
  - pattern: '(\$HOME|~/|/Users/)'
    description: "操作用户主目录（工作目录外）的路径"
  - pattern: '\bgit\s+push\b'
    description: "推送到远程仓库"
```

当前配置还覆盖以下风险类型：

| 风险类型 | 示例规则 |
|----------|----------|
| 文件删除或移动 | `rm`、`mv` |
| 工作区外路径 | `$HOME`、`~/`、`/Users/` |
| 权限与所有权 | `chmod`、`chown` |
| 系统关键目录 | `/etc`、`/usr`、`/var`、`/sys` 等 |
| 进程控制 | `kill`、`pkill`、`killall` |
| 远程副作用 | `git push` |
| 远程脚本执行 | `curl/wget ... | sh` |
| 磁盘操作 | `mkfs`、`fdisk`、`parted`、`dd of=` |
| 系统计划和网络 | `crontab`、`iptables`、`nft` |

若没有规则命中，命令直接返回 `AuthResult(allowed=True, reason="")`；若命中，则将第一条匹配规则的描述传给 Phase 3。

### 3.5 正则编译缓存

为避免每次执行命令都重新编译全部正则，认证模块缓存编译结果：

```python
_compiled_patterns: list[tuple[re.Pattern, str]] | None = None
_compiled_from_cfg_id: int | None = None
```

`_get_compiled_patterns()` 使用配置对象的 `id` 判断配置是否变化：

```text
首次调用 / 配置对象变化 → 重新编译 review_patterns
配置对象未变化          → 直接复用缓存
```

正则统一使用 `re.IGNORECASE`，避免通过大小写变化绕过风险规则。

### 3.6 Phase 3：人工审批

命中风险规则后，认证模块在 stderr 输出审批框：

```text
────────────────────────────────────────────────────────────
  [需要审批] Agent 请求执行以下命令
────────────────────────────────────────────────────────────
  命令   : rm old.log
  原因   : 删除文件（含工作目录外）
────────────────────────────────────────────────────────────
  是否允许执行？[y/N]
```

审批策略采用保守默认值：

| 用户行为 | 结果 |
|----------|------|
| 输入 `y` | 批准并执行 |
| 输入其他内容或直接回车 | 拒绝 |
| `EOFError` / `KeyboardInterrupt` | 默认拒绝 |

只有精确输入小写化后的 `y` 才会放行，避免误触确认。

### 3.7 并发审批锁

多个工具可能通过 `ThreadPoolExecutor` 并行执行。如果多个风险命令同时调用 `input()`，终端内容和用户输入会相互干扰。因此 Phase 3 使用模块级锁串行化整个审批过程：

```python
_approval_lock = threading.Lock()

with _approval_lock:
    _print_approval_prompt(command, matched_reason)
    answer = input("  是否允许执行？[y/N] ").strip().lower()
```

锁仅包围人工审批阶段；普通命令和 Phase 1 直接拒绝不会等待审批锁。

### 3.8 前台与后台命令统一认证

认证逻辑没有写死在 `BashTool` 内部，而是抽取为独立的 `check_command()`，供两种执行入口复用：

| 入口 | 认证时机 | 认证通过后行为 |
|------|----------|----------------|
| `BashTool.execute()` | 调用 `subprocess.run()` 之前 | 前台同步执行命令 |
| `BackGroundManager.run()` | 获取信号量、创建任务线程之前 | 创建后台任务并立即返回任务 ID |

后台命令在启动线程前同步完成审批，避免未经授权的任务先进入后台运行。

### 3.9 权限审计日志

权限认证复用项目级全局日志，不需要额外维护审计文件：

```python
get_logger().warning("command_approved", {
    "phase": 3,
    "command": command,
    "reason": matched_reason,
})

get_logger().warning("command_denied", {
    "phase": 3,
    "command": command,
    "reason": matched_reason,
})
```

| 认证结果 | 日志事件 | 审计数据 |
|----------|----------|----------|
| Phase 1 直接拒绝 | `command_denied` | 阶段、命令、命中关键字 |
| Phase 3 用户批准 | `command_approved` | 阶段、命令、风险原因 |
| Phase 3 用户拒绝 | `command_denied` | 阶段、命令、风险原因 |

审批事件使用 `WARNING`，便于从普通调用日志中过滤出具有安全意义的操作。

---

## 四、日志与权限认证的完整调用链

以一次风险 Bash 工具调用为例：

```text
LLM 返回 bash tool_call
        │
        ▼
ToolRegistry.call()
        ├─ tool_call_started（立即写入）
        ├─ tool_call_arguments（DEBUG）
        │
        ▼
BashTool.execute(command)
        │
        ▼
check_command(command)
        ├─ Phase 1：拒绝列表
        ├─ Phase 2：风险规则
        └─ Phase 3：用户审批
                ├─ 批准 → command_approved
                └─ 拒绝 → command_denied
        │
        ├─ 拒绝：返回“命令被拒绝”
        └─ 批准：subprocess.run()
                    │
                    ▼
ToolRegistry.call()
        ├─ tool_call_completed + duration_ms
        └─ tool_call_result（DEBUG）
```

这里的“认证拒绝”属于工具正常返回结果，因此工具层会记录 `tool_call_completed`，同时权限层记录 `command_denied`。只有 JSON 解析或工具执行抛出未处理异常时，才记录 `tool_call_failed`。

---

## 五、测试与验证

新增 `tests/test_activity_logging.py`，使用临时目录和伪造 LLM 客户端验证日志行为，不发起真实网络请求。

### 5.1 测试覆盖

| 测试 | 验证内容 |
|------|----------|
| `test_tool_call_is_logged_immediately_with_duration` | 工具开始/完成事件、call ID、执行耗时、未关闭前实时可读 |
| `test_tool_failure_is_logged_before_exception_is_raised` | JSON 参数异常会先写入失败事件，再向上抛出 |
| `test_llm_call_lifecycle_is_logged` | LLM 开始/完成事件共享 request ID，并记录模型和 token usage |
| `test_log_file_remains_readable_after_logger_is_closed` | 关闭 logger 后 JSONL 文件仍存在，并包含业务事件和关闭事件 |
| `test_each_event_can_be_fsynced` | 持久化开启时每条事件都会调用 `os.fsync()` |
| `test_default_relative_log_dir_uses_project_root` | 相对日志路径默认解析到项目根目录，而不是当前工作目录的父级 |

测试中的 `_events_on_disk()` 特意在 `close_logger()` 前读取日志：

```python
def _events_on_disk(self) -> list[dict]:
    return [
        json.loads(line)
        for line in self.activity_log.log_path
            .read_text(encoding="utf-8")
            .splitlines()
    ]
```

这可以直接验证“实时落盘”，而不仅仅是退出时最终刷盘。

### 5.2 验证命令

```bash
.venv/bin/python -m compileall -q internal tests
.venv/bin/python -m unittest discover -s tests -v
```

当前结果：

```text
Ran 6 tests

OK
```

---

## 六、后续规划（TODO）

- [ ] **权限认证测试**：为 Phase 1、Phase 2、Phase 3 及输入中断补充独立单元测试
- [ ] **审批中断审计**：用户触发 EOF/KeyboardInterrupt 时补充 `command_denied` 日志
- [ ] **敏感信息脱敏**：对日志中的 token、密码和环境变量值增加字段级脱敏规则
- [ ] **命令解析增强**：从字符串/正则匹配升级为 Shell AST 或分段解析，减少误报和绕过空间
- [ ] **权限策略分级**：支持 always allow、ask、deny 等可配置策略及会话级临时授权
- [ ] **审计日志隔离**：将普通调用日志和安全审计日志写入不同 sink，并配置不同保留周期
- [ ] **日志轮转与保留**：增加 rotation、retention 和 compression 配置
- [ ] **链路追踪**：在 LLM request ID、tool call ID、background task ID 之间增加统一 trace ID
