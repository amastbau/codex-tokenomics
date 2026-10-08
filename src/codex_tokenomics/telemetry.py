"""Normalize explicitly allowlisted telemetry without retaining source content.

Only per-response token_usage_record events produce UsageSample records. Token-count
events contain repeated cumulative values and are used solely for rate-limit snapshots.
"""

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class EventContext:
    source_path: str | Path
    device: int
    inode: int
    byte_offset: int
    session_id: str | None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    timestamp: str
    thread_id: str | None = None
    parent_thread_id: str | None = None
    source: str = "unknown"
    agent_kind: str = "unknown"
    cli_version: str | None = None
    model_provider: str | None = None
    cwd: str | None = None
    workspace_roots: tuple[str, ...] = ()
    kind: Literal["session"] = field(default="session", init=False)


@dataclass(frozen=True, slots=True)
class TurnRecord:
    session_id: str
    timestamp: str
    turn_id: str
    thread_id: str | None = None
    root_turn_id: str | None = None
    model: str | None = None
    model_provider: str | None = None
    reasoning_effort: str | None = None
    collaboration_mode: str | None = None
    sandbox_mode: str | None = None
    approval_mode: str | None = None
    cwd: str | None = None
    workspace_roots: tuple[str, ...] = ()
    context_window: int | None = None
    status: str | None = None
    started_at: int | None = None
    completed_at: int | None = None
    duration_ms: int | None = None
    time_to_first_token_ms: int | None = None
    kind: Literal["turn"] = field(default="turn", init=False)


@dataclass(frozen=True, slots=True)
class UsageSample:
    session_id: str
    timestamp: str
    response_id: str
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    thread_id: str | None = None
    turn_id: str | None = None
    root_turn_id: str | None = None
    cumulative_total_tokens: int | None = None
    kind: Literal["usage"] = field(default="usage", init=False)


@dataclass(frozen=True, slots=True)
class RateLimitSample:
    session_id: str
    timestamp: str
    window: Literal["primary", "secondary"]
    used_percent: float
    limit_id: str | None = None
    limit_name: str | None = None
    window_minutes: int | None = None
    resets_at: int | None = None
    kind: Literal["rate_limit"] = field(default="rate_limit", init=False)


@dataclass(frozen=True, slots=True)
class ToolEvent:
    session_id: str
    timestamp: str
    event_id: str
    name: str
    tool_type: str
    status: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    duration_ms: int | None = None
    status_code: int | None = None
    kind: Literal["tool"] = field(default="tool", init=False)


type TelemetryRecord = SessionRecord | TurnRecord | UsageSample | RateLimitSample | ToolEvent


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _enum(value: object, allowed: set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(path for path in value if _string(path) is not None)


def _timestamp(event: Mapping[str, object]) -> str | None:
    value = _string(event.get("timestamp"))
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _session_id(payload: Mapping[str, object], context: EventContext) -> str | None:
    return _string(payload.get("session_id")) or _string(context.session_id)


def _normalize_session(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    session_id = _string(payload.get("session_id")) or _string(payload.get("id"))
    timestamp = _timestamp(event)
    if session_id is None or timestamp is None:
        return ()
    raw_source = payload.get("source")
    source = _enum(raw_source, {"cli", "vscode", "exec", "mcp"}) or "unknown"
    agent_kind = "user" if source != "unknown" else "unknown"
    parent_thread_id = _string(payload.get("parent_thread_id"))
    source_fields = _mapping(raw_source)
    if "subagent" in source_fields:
        source = "subagent"
        subagent = source_fields.get("subagent")
        agent_kind = "review" if subagent == "review" else "subagent"
        spawn = _mapping(_mapping(subagent).get("thread_spawn"))
        parent_thread_id = parent_thread_id or _string(spawn.get("parent_thread_id"))
    if source_fields.get("internal") == "guardian":
        source, agent_kind = "internal", "guardian"
    thread_source = _enum(payload.get("thread_source"), {"user", "subagent", "guardian_review"})
    if thread_source is not None:
        agent_kind = "guardian" if thread_source == "guardian_review" else thread_source
    return (SessionRecord(
        session_id=session_id,
        timestamp=timestamp,
        thread_id=_string(payload.get("id")),
        parent_thread_id=parent_thread_id,
        source=source,
        agent_kind=agent_kind,
        cli_version=_string(payload.get("cli_version")),
        model_provider=_string(payload.get("model_provider")),
        cwd=_string(payload.get("cwd")),
        workspace_roots=_paths(payload.get("runtime_workspace_roots")),
    ),)


def _normalize_turn(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    session_id, timestamp = _session_id(payload, context), _timestamp(event)
    turn_id = _string(payload.get("turn_id"))
    if session_id is None or timestamp is None or turn_id is None:
        return ()
    return (TurnRecord(
        session_id=session_id,
        timestamp=timestamp,
        turn_id=turn_id,
        thread_id=_string(payload.get("thread_id")),
        root_turn_id=_string(payload.get("root_turn_id")),
        model=_string(payload.get("model")),
        model_provider=_string(payload.get("model_provider")),
        reasoning_effort=_enum(payload.get("effort"), {
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
        }),
        collaboration_mode=_enum(_mapping(payload.get("collaboration_mode")).get("mode"), {
            "default", "plan",
        }),
        sandbox_mode=_enum(_mapping(payload.get("sandbox_policy")).get("type"), {
            "read-only", "workspace-write", "danger-full-access", "external-sandbox",
        }),
        approval_mode=_enum(payload.get("approval_policy"), {
            "untrusted", "on-failure", "on-request", "never",
        }),
        cwd=_string(payload.get("cwd")),
        workspace_roots=_paths(payload.get("workspace_roots")),
        context_window=_integer(payload.get("model_context_window")),
    ),)


def _normalize_usage(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    session_id, timestamp = _session_id(payload, context), _timestamp(event)
    response_id = _string(payload.get("response_id"))
    usage = _mapping(payload.get("usage"))
    input_tokens, output_tokens = _integer(usage.get("input_tokens")), _integer(
        usage.get("output_tokens")
    )
    total_tokens = _integer(usage.get("total_tokens"))
    cached_input_tokens = _integer(usage.get("cached_input_tokens", 0))
    cache_write_input_tokens = _integer(usage.get("cache_write_input_tokens", 0))
    reasoning_output_tokens = _integer(usage.get("reasoning_output_tokens", 0))
    if any(value is None for value in (
        session_id, timestamp, response_id, input_tokens, output_tokens, total_tokens,
        cached_input_tokens, cache_write_input_tokens, reasoning_output_tokens,
    )):
        return ()
    return (UsageSample(
        session_id=session_id,
        timestamp=timestamp,
        response_id=response_id,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning_output_tokens,
        total_tokens=total_tokens,
        thread_id=_string(payload.get("thread_id")),
        turn_id=_string(payload.get("turn_id")),
        root_turn_id=_string(payload.get("root_turn_id")),
        cumulative_total_tokens=_integer(
            _mapping(payload.get("thread_token_usage")).get("total_tokens")
        ),
    ),)


def _normalize_limits(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    session_id, timestamp = _session_id(payload, context), _timestamp(event)
    if session_id is None or timestamp is None:
        return ()
    limits = _mapping(payload.get("rate_limits"))
    records: list[TelemetryRecord] = []
    for window_name in ("primary", "secondary"):
        window = _mapping(limits.get(window_name))
        used_percent = window.get("used_percent")
        if type(used_percent) not in (int, float) or not 0 <= used_percent <= 100:
            continue
        if not math.isfinite(used_percent):
            continue
        records.append(RateLimitSample(
            session_id=session_id,
            timestamp=timestamp,
            window=window_name,
            used_percent=float(used_percent),
            limit_id=_string(limits.get("limit_id")),
            limit_name=_string(limits.get("limit_name")),
            window_minutes=_integer(window.get("window_minutes")),
            resets_at=_integer(window.get("resets_at")),
        ))
    return tuple(records)


def _normalize_lifecycle(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    session_id, timestamp = _session_id(payload, context), _timestamp(event)
    turn_id = _string(payload.get("turn_id"))
    if session_id is None or timestamp is None or turn_id is None:
        return ()
    return (TurnRecord(
        session_id=session_id,
        timestamp=timestamp,
        turn_id=turn_id,
        thread_id=_string(payload.get("thread_id")),
        root_turn_id=_string(payload.get("root_turn_id")),
        context_window=_integer(payload.get("model_context_window")),
        status="started" if payload.get("type") == "task_started" else "completed",
        started_at=_integer(payload.get("started_at")),
        completed_at=_integer(payload.get("completed_at")),
        duration_ms=_integer(payload.get("duration_ms")),
        time_to_first_token_ms=_integer(payload.get("time_to_first_token_ms")),
    ),)


def _fallback_event_id(context: EventContext) -> str:
    # Length-prefix the path to make the four-field encoding unambiguous.
    path = str(context.source_path).encode("utf-8")
    identity = f"{len(path)}:".encode() + path + (
        f":{context.device}:{context.inode}:{context.byte_offset}"
    ).encode()
    return hashlib.sha256(identity).hexdigest()


def _normalize_tool(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    session_id, timestamp = _session_id(payload, context), _timestamp(event)
    item = _mapping(payload.get("item"))
    builtin_names = {
        "CommandExecution": "exec_command", "FileChange": "apply_patch",
        "WebSearch": "web_search", "ImageView": "view_image",
        "ImageGeneration": "image_generation",
    }
    item_type = _enum(item.get("type"), {
        "CommandExecution", "DynamicToolCall", "McpToolCall", "CollabAgentToolCall",
        "FileChange", "WebSearch", "ImageView", "ImageGeneration",
    })
    if session_id is None or timestamp is None or item_type is None:
        return ()
    name = builtin_names.get(item_type) or _string(item.get("tool"))
    if name is None:
        return ()
    started_at_ms, completed_at_ms = _integer(payload.get("started_at_ms")), _integer(
        payload.get("completed_at_ms")
    )
    duration_ms = None
    if (started_at_ms is not None and completed_at_ms is not None
            and completed_at_ms >= started_at_ms):
        duration_ms = completed_at_ms - started_at_ms
    if duration_ms is None:
        duration = _mapping(item.get("duration"))
        seconds, nanoseconds = _integer(duration.get("secs")), _integer(duration.get("nanos"))
        if seconds is not None and nanoseconds is not None and nanoseconds < 1_000_000_000:
            duration_ms = seconds * 1000 + nanoseconds // 1_000_000
    exit_code = item.get("exit_code")
    return (ToolEvent(
        session_id=session_id,
        timestamp=timestamp,
        event_id=_string(item.get("id")) or _fallback_event_id(context),
        name=name,
        tool_type=item_type,
        status=_enum(item.get("status"), {
            "in_progress", "completed", "failed", "declined", "interrupted",
        }),
        thread_id=_string(payload.get("thread_id")),
        turn_id=_string(payload.get("turn_id")),
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        duration_ms=duration_ms,
        status_code=exit_code if type(exit_code) is int else None,
    ),)


def _normalize_event_message(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    payload = _mapping(event.get("payload"))
    subtype = payload.get("type")
    if subtype in ("task_started", "task_complete"):
        return _normalize_lifecycle(event, context)
    if subtype == "token_count":
        return _normalize_limits(event, context)
    if subtype == "item_completed":
        return _normalize_tool(event, context)
    return ()


HANDLERS: dict[str, Callable[[Mapping[str, object], EventContext], tuple[TelemetryRecord, ...]]] = {
    "session_meta": _normalize_session,
    "turn_context": _normalize_turn,
    "token_usage_record": _normalize_usage,
    "event_msg": _normalize_event_message,
}


def normalize_event(
    event: Mapping[str, object], context: EventContext,
) -> tuple[TelemetryRecord, ...]:
    """Return immutable telemetry built from named fields; ignore all other data."""
    event_type = event.get("type")
    handler = HANDLERS.get(event_type) if isinstance(event_type, str) else None
    return handler(event, context) if handler else ()
