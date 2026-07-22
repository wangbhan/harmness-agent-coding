# Agent Hooks Design

## Goal

Add configurable command hooks for `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, and `Stop`. Hook definitions use a Claude Code-style JSON file;
`config.yaml` contains the path to that file and runtime defaults.

## Configuration

The main YAML configuration gains a `hooks` section:

```yaml
hooks:
  config_path: "hooks.json"
  default_timeout: 10
  stop_max_continuations: 5
```

An empty `config_path` disables hooks. A relative path is resolved against the
directory from which `config.yaml` is loaded. A configured but missing file,
invalid JSON, unknown event, unknown hook type, invalid regular expression, or
invalid field value is a startup error.

The hook file uses this structure:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python scripts/validate_prompt.py",
            "timeout": 10
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "bash|write|edit",
        "hooks": [
          {
            "type": "command",
            "command": "python scripts/check_tool.py",
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

Only `type: "command"` is supported initially. `matcher` is a regular
expression matched against the complete tool name. It applies to tool events;
omitting it or using an empty string matches every event. Commands in matching
groups run in file order. Each command may specify `timeout` in seconds and
`on_error` as `allow` or `block`; otherwise the YAML default timeout and
`on_error: "allow"` apply.

## Module Boundary

`internal/Agent/hooks.py` is the sole hook implementation module. It owns:

- the four event names;
- JSON loading and validation;
- matcher compilation;
- command subprocess execution;
- stdin/stdout protocol parsing;
- ordered result merging;
- the immutable, lazily initialized process-wide `HookManager`.

The module exposes typed result objects to the Agent loop. It does not call the
LLM, execute registered tools, or mutate conversation history directly.

`internal/Agent/config.py` only models YAML settings and supplies the resolved
hook path. `internal/Agent/base_agent.py` owns event timing and applies hook
results to the conversation or tool execution.

## Command Protocol

Every command runs with the Agent work directory as `cwd`, inherits the Agent
process environment, receives one JSON object on stdin, and may write one JSON
object to stdout. No output on successful exit means allow with no changes.

Every input contains:

```json
{
  "hook_event_name": "PreToolUse",
  "cwd": "/absolute/agent/workdir"
}
```

Event-specific inputs are:

- `UserPromptSubmit`: `prompt`.
- `PreToolUse`: `tool_name`, `tool_input`, and `tool_use_id`.
- `PostToolUse`: `tool_name`, `tool_input`, `tool_use_id`, and `tool_output`.
- `Stop`: `assistant_message` and `continuation_count`.

Output follows a Claude Code-style envelope:

```json
{
  "systemMessage": "optional diagnostic message",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "validation passed",
    "updatedInput": {"command": "git status"},
    "additionalContext": "repository is on a protected branch"
  }
}
```

The `hookEventName` must equal the event being executed. Supported specific
fields are:

- `UserPromptSubmit`: `decision` (`allow` or `block`), `reason`,
  `updatedPrompt`, and `additionalContext`.
- `PreToolUse`: `permissionDecision` (`allow` or `deny`),
  `permissionDecisionReason`, `updatedInput`, and `additionalContext`.
- `PostToolUse`: `decision` (`allow` or `block`), `reason`, `updatedOutput`,
  and `additionalContext`.
- `Stop`: `decision` (`allow` or `block`), `reason`, and
  `additionalContext`.

`systemMessage` is recorded in the activity log for operator visibility;
`additionalContext` is injected into the next LLM request as a system message.
Unknown result fields are ignored for forward compatibility, but known fields
must have the documented type.

When multiple hooks match, an updated prompt, tool input, or tool output is
passed to the next command. Context strings accumulate in configuration order.
A blocking result stops execution of the remaining hooks for that event.

## Exit and Failure Semantics

- Exit code `0`: parse stdout as the result described above. Empty stdout is an
  unchanged allow result.
- Exit code `2`: block the event. The stripped stderr text is the reason; a
  generic reason is used when stderr is empty.
- Any other nonzero exit, timeout, process launch error, or malformed stdout is
  a runtime hook error. It blocks when the command has `on_error: "block"` and
  otherwise logs the failure and allows processing to continue.

Runtime hook errors never expose the inherited environment in logs. Logs
include event name, command text, duration, exit status, timeout state, and a
bounded stderr or parse-error description.

## Event Flow

### UserPromptSubmit

The event runs once for a real user submission before the first LLM request.
It does not run for an internal delegated sub-Agent task. Hooks may rewrite the
prompt or add context. If blocked, the LLM is not called, the reason is shown to
the user, and the rejected prompt is removed from conversation history so it
cannot leak into a later turn.

### PreToolUse and PostToolUse

For one assistant response, all `PreToolUse` chains run in original tool-call
order. Denied tools are not passed to `ToolRegistry`; their denial reason
becomes that call's tool result. Approved tools retain the existing parallel
execution behavior and use the final rewritten input.

After all approved calls finish, `PostToolUse` chains run in original tool-call
order. They may inspect or rewrite output. A blocked post-hook replaces the
visible tool result with its reason. All tool results are appended in original
order, followed by one consolidated system message containing accumulated
context. This preserves the required adjacency and completeness of OpenAI tool
call/result messages.

Tool hooks apply to both the main Agent and delegated sub-Agents.

### Stop

Before returning a final assistant response, the Agent runs `Stop`. An allow
result prints, logs, and returns the response normally. A block result appends
its reason and context as a system message and continues the LLM loop. The
input `continuation_count` starts at zero and increments for each forced
continuation.

At `hooks.stop_max_continuations` (default 5), further block decisions are
ignored, a warning is logged, and the Agent exits normally. Stop hooks apply to
both the main Agent and delegated sub-Agents.

## Logging

Hook lifecycle events integrate with the current structured activity logger:

- `hook_started` records event and command;
- `hook_completed` records duration, exit code, and decision;
- `hook_failed` records duration and a bounded error description;
- `hook_blocked` records event and reason.

Prompt contents, tool inputs, tool outputs, stdout payloads, and environment
variables are not duplicated into hook lifecycle logs. Existing user, LLM, and
tool logging behavior remains unchanged.

## Tests

Automated tests cover:

- YAML defaults and relative/absolute path resolution;
- JSON shape, event names, hook types, matchers, and field validation;
- matcher full-match behavior and configuration ordering;
- stdin payloads and stdout transformations for every event;
- chained prompt/input/output modification and context accumulation;
- exit code 2, other nonzero exits, timeout, launch errors, invalid JSON, and
  both `on_error` policies;
- rejected user prompts never invoking the LLM or remaining in history;
- denied tools never executing;
- deterministic Pre/Post order while approved tools remain parallel;
- PostToolUse replacement behavior;
- Stop allow, forced continuation, and the five-continuation limit;
- main/sub-Agent event scope;
- hook logging without sensitive payload duplication.

Existing configuration, Agent-loop, tool registry, authorization, compaction,
background-task, and logging tests must continue to pass when hooks are
disabled.
