# Agent Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Claude Code-style configurable command hooks for user submission, tool execution, and Agent stopping.

**Architecture:** A focused `internal/Agent/hooks.py` module loads and validates the external JSON file, executes matching commands, and returns typed transformations. `Agent` injects the manager and applies results at the four lifecycle boundaries while preserving parallel tool execution and valid OpenAI message ordering.

**Tech Stack:** Python 3.13, Pydantic v2, PyYAML, `subprocess`, `unittest`, OpenAI-compatible chat messages.

---

## File Map

- Create `internal/Agent/hooks.py`: event definitions, hook JSON models, manager, command protocol, singleton lifecycle.
- Modify `internal/Agent/config.py`: YAML hook settings and active configuration-directory access.
- Modify `internal/Agent/base_agent.py`: four hook trigger points and deterministic tool orchestration.
- Modify `internal/Agent/tools/__init__.py`: mark the delegated Agent so user-submit hooks do not run on internal tasks.
- Modify `internal/conversation_log.py`: structured hook lifecycle methods.
- Modify `config.yaml`: documented hook settings and disabled-by-default path.
- Modify `README.md`: feature, architecture, protocol, and configuration examples.
- Create `hooks.example.json`: safe, empty Claude Code-style hook template.
- Create `tests/test_hooks.py`: configuration, validation, matching, subprocess protocol, and error-policy tests.
- Create `tests/test_agent_hooks.py`: Agent lifecycle integration tests.
- Modify `tests/test_activity_logging.py`: verify hook events omit sensitive payloads.

### Task 1: YAML Hook Configuration

**Files:**
- Modify: `internal/Agent/config.py`
- Test: `tests/test_hooks.py`

- [ ] **Step 1: Write failing configuration tests**

Add tests that call `init_config(temp_path)` with an otherwise empty YAML and assert:

```python
self.assertEqual(get_config().hooks.config_path, "")
self.assertEqual(get_config().hooks.default_timeout, 10)
self.assertEqual(get_config().hooks.stop_max_continuations, 5)
self.assertEqual(get_config_dir(), temp_path.resolve())
```

Add Pydantic validation cases for non-positive timeout, negative continuation count, and an explicit relative hook path.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run python -m unittest tests.test_hooks.HooksConfigTest -v`

Expected: import or attribute failures because `HooksConfig`, `AgentConfig.hooks`, and `get_config_dir()` do not exist.

- [ ] **Step 3: Add the minimal YAML model**

Implement:

```python
class HooksConfig(BaseModel):
    config_path: str = ""
    default_timeout: int = Field(default=10, gt=0)
    stop_max_continuations: int = Field(default=5, ge=0)


class AgentConfig(BaseModel):
    # existing fields...
    hooks: HooksConfig = Field(default_factory=HooksConfig)
```

Track the resolved directory selected by `init_config()` and expose it through `get_config_dir() -> Path` so relative hook paths never depend on process cwd.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run python -m unittest tests.test_hooks.HooksConfigTest -v`

Expected: all configuration tests pass.

- [ ] **Step 5: Commit**

```bash
git add internal/Agent/config.py tests/test_hooks.py
git commit -m "feat(hooks): add yaml hook settings"
```

### Task 2: Hook JSON Loading and Matching

**Files:**
- Create: `internal/Agent/hooks.py`
- Modify: `tests/test_hooks.py`

- [ ] **Step 1: Write failing loader tests**

Cover an empty disabled manager, the four allowed event keys, ordered groups, full regex matching, omitted matchers, and startup failures for missing files, invalid JSON, unknown event names, unknown types, empty commands, invalid timeout/on-error values, and invalid regular expressions.

Use this public API in the tests:

```python
manager = HookManager.from_config(hooks_config, config_dir, workdir=temp_path)
self.assertFalse(manager.enabled)
self.assertEqual(
    [hook.command for hook in manager.matching(HookEvent.PRE_TOOL_USE, "bash")],
    ["first", "second"],
)
```

- [ ] **Step 2: Run loader tests and verify RED**

Run: `uv run python -m unittest tests.test_hooks.HookLoadingTest -v`

Expected: failure because `internal.Agent.hooks` does not exist.

- [ ] **Step 3: Implement immutable hook definitions and loader**

Define:

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
```

Validate the raw JSON explicitly so errors identify the JSON path. Resolve relative `config_path` against `get_config_dir()`. Match tool names with `pattern.fullmatch(tool_name)`; non-tool events ignore matcher text and run groups without a matcher.

- [ ] **Step 4: Run loader tests and verify GREEN**

Run: `uv run python -m unittest tests.test_hooks.HookLoadingTest -v`

Expected: all loader tests pass.

- [ ] **Step 5: Commit**

```bash
git add internal/Agent/hooks.py tests/test_hooks.py
git commit -m "feat(hooks): load and match hook definitions"
```

### Task 3: Command Protocol and Result Chaining

**Files:**
- Modify: `internal/Agent/hooks.py`
- Modify: `tests/test_hooks.py`

- [ ] **Step 1: Write failing protocol tests**

Create executable temporary Python hook scripts and assert each stdin payload includes `hook_event_name` and `cwd`, plus its exact event fields. Test empty stdout, valid event envelopes, mismatched `hookEventName`, field type validation, chained `updatedPrompt`, `updatedInput`, `updatedOutput`, ordered context accumulation, and early termination after a block.

The tests use one result model:

```python
result = manager.run(
    HookEvent.PRE_TOOL_USE,
    {"tool_name": "bash", "tool_input": {"command": "pwd"}, "tool_use_id": "1"},
)
self.assertEqual(result.updated_input, {"command": "git status"})
self.assertEqual(result.additional_context, ("first", "second"))
```

- [ ] **Step 2: Run protocol tests and verify RED**

Run: `uv run python -m unittest tests.test_hooks.HookProtocolTest -v`

Expected: failures because the manager has no `run()` implementation.

- [ ] **Step 3: Implement subprocess execution and parsing**

Add a typed result:

```python
@dataclass(frozen=True)
class HookResult:
    blocked: bool = False
    reason: str = ""
    updated_prompt: str | None = None
    updated_input: dict[str, Any] | None = None
    updated_output: str | None = None
    additional_context: tuple[str, ...] = ()
```

Execute command hooks with `subprocess.run(command, shell=True, cwd=self.workdir, input=json.dumps(payload), text=True, capture_output=True, timeout=timeout)`. Parse only the documented event-specific fields. Pass transformations forward by updating a copy of the payload before executing the next command.

- [ ] **Step 4: Write failing exit-policy tests**

Test exit code 2, empty stderr fallback, nonzero exit, timeout, invalid JSON, launch errors, and both `on_error` values. Assert fail-open returns an unchanged result and fail-closed returns a blocked result with a bounded reason.

- [ ] **Step 5: Implement exit and error policy**

Map exit 2 directly to blocked. Convert timeout, launch, nonzero, and parse failures through one `_runtime_error_result()` helper that consults `on_error`. Never include `os.environ` or the stdin payload in exception messages or logs.

- [ ] **Step 6: Run all hook unit tests and verify GREEN**

Run: `uv run python -m unittest tests.test_hooks -v`

Expected: all hook configuration, loader, protocol, and error tests pass.

- [ ] **Step 7: Commit**

```bash
git add internal/Agent/hooks.py tests/test_hooks.py
git commit -m "feat(hooks): execute command hook protocol"
```

### Task 4: UserPromptSubmit and Stop Integration

**Files:**
- Modify: `internal/Agent/base_agent.py`
- Modify: `internal/Agent/tools/__init__.py`
- Create: `tests/test_agent_hooks.py`

- [ ] **Step 1: Write failing user-submit integration tests**

Inject a fake hook manager into `Agent`. Verify prompt rewriting and context injection occur before the first LLM request. Verify a blocked prompt causes zero LLM calls, removes the rejected user message, and returns its reason. Verify `process_user_prompts=False` skips the event for a delegated Agent.

- [ ] **Step 2: Run user-submit tests and verify RED**

Run: `uv run python -m unittest tests.test_agent_hooks.UserPromptHookTest -v`

Expected: constructor/signature failures because Agent cannot accept a hook manager or sub-Agent scope flag.

- [ ] **Step 3: Add injected manager and user-submit boundary**

Extend construction without breaking existing callers:

```python
def __init__(..., hook_manager=None, process_user_prompts=True):
    self.hook_manager = hook_manager or get_hook_manager()
    self.process_user_prompts = process_user_prompts
```

At the start of `run()`, process only a trailing user message. Replace its content and insert accumulated context before it. On block, remove the trailing submission, print and return the reason. Construct the delegated Agent with `process_user_prompts=False`.

- [ ] **Step 4: Write failing Stop tests**

Use a sequenced fake completion client and fake hook manager. Assert allow exits once; block adds the reason/context and calls the LLM again; `continuation_count` is 0 through 5; and a sixth block is ignored after exactly five forced continuations.

- [ ] **Step 5: Implement Stop control**

Before printing a stop response, call the Stop hook. For a block below the configured limit, append one system continuation message, increment the counter, and continue. At the limit, log a warning and return the current assistant response.

- [ ] **Step 6: Run integration tests and verify GREEN**

Run: `uv run python -m unittest tests.test_agent_hooks.UserPromptHookTest tests.test_agent_hooks.StopHookTest -v`

Expected: all UserPromptSubmit and Stop tests pass.

- [ ] **Step 7: Commit**

```bash
git add internal/Agent/base_agent.py internal/Agent/tools/__init__.py tests/test_agent_hooks.py
git commit -m "feat(hooks): integrate prompt and stop hooks"
```

### Task 5: PreToolUse and PostToolUse Integration

**Files:**
- Modify: `internal/Agent/base_agent.py`
- Modify: `tests/test_agent_hooks.py`

- [ ] **Step 1: Write failing tool-hook integration tests**

Build fake tool-call message objects and recording tools. Verify Pre hooks run in call order, update arguments before `ToolRegistry.call`, denied calls do not execute, allowed calls still overlap in time, Post hooks run in original order, outputs can be rewritten or replaced on block, results retain original order, and consolidated context appears after every tool message.

- [ ] **Step 2: Run tool-hook tests and verify RED**

Run: `uv run python -m unittest tests.test_agent_hooks.ToolHookTest -v`

Expected: hook manager receives no tool events.

- [ ] **Step 3: Implement deterministic three-phase orchestration**

Refactor the current tool block into:

1. sequential Pre processing that records final arguments or denial content;
2. parallel registry calls for approved tools only;
3. sequential Post processing and ordered message append.

Serialize modified inputs with `json.dumps(..., ensure_ascii=False)`. Always append one tool result for every original call. Append accumulated context only after all tool messages. Preserve compact-tool detection using the original tool name.

- [ ] **Step 4: Run tool-hook and regression tests**

Run: `uv run python -m unittest tests.test_agent_hooks.ToolHookTest tests.test_activity_logging -v`

Expected: all tests pass and existing tool logging remains intact.

- [ ] **Step 5: Commit**

```bash
git add internal/Agent/base_agent.py tests/test_agent_hooks.py
git commit -m "feat(hooks): integrate tool lifecycle hooks"
```

### Task 6: Hook Lifecycle Logging

**Files:**
- Modify: `internal/conversation_log.py`
- Modify: `internal/Agent/hooks.py`
- Modify: `tests/test_activity_logging.py`

- [ ] **Step 1: Write failing safe-logging tests**

Execute one successful, one blocked, and one failed hook against a real temporary logger. Assert the JSONL events include `hook_started`, `hook_completed`, `hook_blocked`, and `hook_failed`, with command/event/duration/decision metadata. Put sentinel secrets in input, stdout, and environment and assert none occur in hook event data.

- [ ] **Step 2: Run logging tests and verify RED**

Run: `uv run python -m unittest tests.test_activity_logging.ActivityLoggingTest.test_hook_lifecycle_is_logged_without_payloads -v`

Expected: failure because hook-specific logger methods do not exist.

- [ ] **Step 3: Implement logger methods and manager calls**

Add `hook_started`, `hook_completed`, `hook_failed`, and `hook_blocked` methods to `SessionLogger`. Pass only bounded stderr/error descriptions and operational metadata. Call them around each subprocess in `HookManager`.

- [ ] **Step 4: Run logging and hook tests**

Run: `uv run python -m unittest tests.test_activity_logging tests.test_hooks -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add internal/conversation_log.py internal/Agent/hooks.py tests/test_activity_logging.py
git commit -m "feat(hooks): log hook lifecycle safely"
```

### Task 7: Templates, Documentation, and Full Verification

**Files:**
- Modify: `config.yaml`
- Create: `hooks.example.json`
- Modify: `README.md`

- [ ] **Step 1: Add disabled-by-default configuration and example JSON**

Document `hooks.config_path`, `default_timeout`, and the default continuation limit of 5 in `config.yaml`. Add an empty four-event `hooks.example.json` so enabling the feature does not execute unexpected commands.

- [ ] **Step 2: Document the complete protocol**

Update README features, project tree, architecture, and configuration sections. Include a practical PreToolUse example, stdin fields, event-specific output fields, exit codes, matcher semantics, error policy, and Stop safety limit.

- [ ] **Step 3: Run focused tests**

Run: `uv run python -m unittest tests.test_hooks tests.test_agent_hooks tests.test_activity_logging -v`

Expected: all focused tests pass.

- [ ] **Step 4: Run the full test suite**

Run: `uv run python -m unittest discover -s tests -v`

Expected: all tests pass with no tracebacks or warnings.

- [ ] **Step 5: Validate syntax and repository whitespace**

Run: `uv run python -m compileall -q internal tests`

Expected: exit code 0.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add config.yaml hooks.example.json README.md
git commit -m "docs(hooks): document hook configuration and protocol"
```

- [ ] **Step 7: Inspect final scope**

Run: `git status --short && git log --oneline -8`

Expected: only the user's pre-existing `.idea/app.iml` and `.DS_Store` changes remain outside the implementation commits; hook changes appear in focused commits.
