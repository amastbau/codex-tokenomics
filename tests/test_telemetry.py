import copy
import json
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path

import pytest

from codex_tokenomics.telemetry import (
    EventContext,
    RateLimitSample,
    SessionRecord,
    ToolEvent,
    TurnRecord,
    UsageSample,
    normalize_event,
)

SECRET = "PROHIBITED-CONTENT-9b84f1"
FIXTURE = Path(__file__).parent / "fixtures" / "mixed-session.jsonl"
EVENT_CONTEXT = EventContext(
    source_path="/synthetic/session.jsonl", device=7, inode=42, byte_offset=100,
    session_id="session-1",
)


def fixture_events() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def normalized_fixture_records() -> tuple:
    return tuple(
        record for index, event in enumerate(fixture_events())
        for record in normalize_event(event, replace(EVENT_CONTEXT, byte_offset=index * 100))
    )


def token_usage_event() -> dict:
    return fixture_events()[3]


def tool_event() -> dict:
    return fixture_events()[5]


def test_token_usage_record_normalizes_only_numeric_telemetry() -> None:
    records = normalize_event(token_usage_event(), EVENT_CONTEXT)
    usage = next(record for record in records if isinstance(record, UsageSample))
    assert usage.response_id == "response-1"
    assert usage.total_tokens == 1_337
    assert usage.cached_input_tokens == 900
    assert usage.cache_write_input_tokens == 20
    assert usage.reasoning_output_tokens == 37
    assert usage.cumulative_total_tokens == 8000
    assert (usage.thread_id, usage.turn_id, usage.root_turn_id) == (
        "thread-1", "turn-1", "root-turn-1",
    )


def test_child_metadata_uses_its_thread_identity_not_parent_session() -> None:
    event = fixture_events()[0]
    event["payload"].update(id="child-thread", session_id="parent-session")
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert record.session_id == "child-thread"
    assert record.thread_id == "child-thread"


def test_child_usage_and_turn_keep_rollout_identity_with_parent_session_payload() -> None:
    context = replace(EVENT_CONTEXT, session_id="child-thread")
    for event in (token_usage_event(), fixture_events()[1]):
        event["payload"]["session_id"] = "parent-session"
        event["payload"]["thread_id"] = "child-thread"
        assert normalize_event(event, context)[0].session_id == "child-thread"


def test_explicit_usage_thread_wins_over_ancestor_rollout_context() -> None:
    event = token_usage_event()
    event["payload"].update(session_id="parent", thread_id="child")
    assert normalize_event(event, EVENT_CONTEXT)[0].session_id == "child"


def test_nested_prohibited_content_never_survives_normalization(capsys) -> None:
    records = normalized_fixture_records()
    serialized = json.dumps([asdict(record) for record in records], sort_keys=True)
    assert records
    assert SECRET not in serialized
    for field in ("arguments", "aggregated_output", "command", "base_instructions", "unknown"):
        assert f'"{field}"' not in serialized
    assert '"output"' not in serialized
    assert SECRET not in repr(records)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("event_type", [
    "future_content_event", "response_item", "world_state", "error", None, 42, {}, [],
])
def test_unknown_event_type_is_ignored(event_type: object) -> None:
    assert normalize_event({"type": event_type, "payload": {"text": SECRET}}, EVENT_CONTEXT) == ()


def test_session_turn_rate_limit_and_tool_metadata_are_allowlisted() -> None:
    records = normalized_fixture_records()
    session = next(record for record in records if isinstance(record, SessionRecord))
    turn = next(record for record in records if isinstance(record, TurnRecord))
    limits = [record for record in records if isinstance(record, RateLimitSample)]
    tool = next(record for record in records if isinstance(record, ToolEvent))
    assert (session.agent_kind, turn.model) == ("subagent", "gpt-6.1-sol")
    assert session.parent_thread_id == "parent-1"
    assert session.workspace_roots == ("/synthetic/project",)
    assert (session.source, session.cli_version, session.model_provider) == (
        "subagent", "0.155.1", "openai",
    )
    assert (turn.reasoning_effort, turn.sandbox_mode, turn.approval_mode) == (
        "high", "workspace-write", "on-request",
    )
    assert turn.collaboration_mode == "default"
    assert len(limits) == 2
    assert (limits[0].limit_name, limits[0].window, limits[0].used_percent) == (
        "tokens", "primary", 12.5,
    )
    assert (limits[1].window_minutes, limits[1].resets_at) == (10080, 1792051200)
    assert (tool.name, tool.status, tool.event_id) == ("exec_command", "completed", "tool-1")
    assert (tool.duration_ms, tool.status_code) == (1000, 0)


def test_turn_lifecycle_keeps_only_timing_metadata() -> None:
    records = normalized_fixture_records()
    turns = [record for record in records if isinstance(record, TurnRecord)]
    started = next(record for record in turns if record.status == "started")
    completed = next(record for record in turns if record.status == "completed")
    assert started.context_window == 258400
    assert started.started_at == 1791446401
    assert (completed.started_at, completed.completed_at) == (1791446401, 1791446404)
    assert (completed.duration_ms, completed.time_to_first_token_ms) == (3000, 40)


@pytest.mark.parametrize("source,thread_source,expected", [
    ("cli", None, "user"),
    ("exec", None, "user"),
    ({"subagent": "review"}, None, "review"),
    ({"internal": "guardian"}, None, "guardian"),
    ("cli", "guardian_review", "guardian"),
    ({"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}}, None, "subagent"),
])
def test_session_sources_classify_user_subagent_and_review_sessions(
    source: object, thread_source: str | None, expected: str,
) -> None:
    event = fixture_events()[0]
    event["payload"]["source"] = source
    event["payload"]["thread_source"] = thread_source
    assert normalize_event(event, EVENT_CONTEXT)[0].agent_kind == expected


@pytest.mark.parametrize("source,expected", [
    ({"subagent": "review"}, "review"),
    ({"internal": "guardian"}, "guardian"),
])
@pytest.mark.parametrize("thread_source", ["subagent", "user"])
def test_specific_review_and_guardian_classification_survives_generic_thread_source(
    source: object, expected: str, thread_source: str,
) -> None:
    event = fixture_events()[0]
    event["payload"]["source"] = source
    event["payload"]["thread_source"] = thread_source
    assert normalize_event(event, EVENT_CONTEXT)[0].agent_kind == expected


def test_token_count_never_emits_cumulative_usage_as_response_usage() -> None:
    event = fixture_events()[4]
    records = normalize_event(event, EVENT_CONTEXT)
    assert len(records) == 2
    assert all(isinstance(record, RateLimitSample) for record in records)
    event["payload"]["rate_limits"] = None
    assert normalize_event(event, EVENT_CONTEXT) == ()


def test_usage_without_stable_response_identity_is_ignored() -> None:
    event = token_usage_event()
    del event["payload"]["response_id"]
    assert normalize_event(event, EVENT_CONTEXT) == ()


@pytest.mark.parametrize("value", [True, -1, 1.5, "1337", None, {}, float("nan")])
def test_invalid_token_counter_is_ignored_without_coercion(value: object) -> None:
    event = token_usage_event()
    event["payload"]["usage"]["total_tokens"] = value
    assert normalize_event(event, EVENT_CONTEXT) == ()


def test_zero_usage_and_absent_optional_cache_counters_are_preserved() -> None:
    event = token_usage_event()
    event["payload"]["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert (record.total_tokens, record.cached_input_tokens, record.cache_write_input_tokens) == (
        0, 0, 0,
    )


@pytest.mark.parametrize("event_type", [
    "session_meta", "turn_context", "token_usage_record", "event_msg",
])
@pytest.mark.parametrize("payload", [None, [], SECRET, 3])
def test_malformed_payload_is_ignored(event_type: str, payload: object) -> None:
    event = {"type": event_type, "timestamp": "2026-10-08T08:00:00Z", "payload": payload}
    assert normalize_event(event, EVENT_CONTEXT) == ()


@pytest.mark.parametrize("subtype", ["agent_message", "agent_reasoning", "error", [], None])
def test_unallowlisted_event_message_subtype_is_ignored(subtype: object) -> None:
    event = {"type": "event_msg", "payload": {"type": subtype, "text": SECRET}}
    assert normalize_event(event, EVENT_CONTEXT) == ()


@pytest.mark.parametrize("item_type", ["AgentMessage", "Reasoning", "UserMessage", "Plan", "NewTool"])
def test_content_items_do_not_become_tool_events(item_type: str) -> None:
    event = tool_event()
    event["payload"]["item"]["type"] = item_type
    assert normalize_event(event, EVENT_CONTEXT) == ()


@pytest.mark.parametrize("item_type,name_field,name", [
    ("DynamicToolCall", "tool", "lookup"),
    ("McpToolCall", "tool", "search"),
    ("CollabAgentToolCall", "tool", "spawn_agent"),
])
def test_named_tool_metadata_is_preserved_without_custom_payloads(
    item_type: str, name_field: str, name: str,
) -> None:
    event = tool_event()
    event["payload"]["item"]["type"] = item_type
    event["payload"]["item"][name_field] = name
    event["payload"]["item"]["content_items"] = [{"text": SECRET}]
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert record.name == name
    assert SECRET not in json.dumps(asdict(record))


def test_missing_tool_id_depends_only_on_file_identity_and_offset() -> None:
    first = tool_event()
    del first["payload"]["item"]["id"]
    second = copy.deepcopy(first)
    second["payload"]["item"]["arguments"] = {"changed": "different content"}
    second["payload"]["item"]["command"] = ["different command"]
    second["payload"]["item"]["aggregated_output"] = "different output"
    first_id = normalize_event(first, EVENT_CONTEXT)[0].event_id
    assert normalize_event(second, EVENT_CONTEXT)[0].event_id == first_id
    assert len(first_id) == 64
    assert set(first_id) <= set("0123456789abcdef")
    for context in (
        replace(EVENT_CONTEXT, source_path="/synthetic/other.jsonl"),
        replace(EVENT_CONTEXT, device=8),
        replace(EVENT_CONTEXT, inode=43),
        replace(EVENT_CONTEXT, byte_offset=101),
    ):
        assert normalize_event(first, context)[0].event_id != first_id
    assert normalize_event(first, replace(EVENT_CONTEXT, session_id="other-session"))[0].event_id == (
        first_id
    )


def test_structured_optional_metadata_is_not_coerced_to_strings() -> None:
    event = fixture_events()[1]
    event["payload"]["model"] = {"content": SECRET}
    event["payload"]["cwd"] = [SECRET]
    event["payload"]["effort"] = {"text": SECRET}
    event["payload"]["workspace_roots"] = [{"text": SECRET}]
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert (record.model, record.cwd, record.reasoning_effort) == (None, None, None)
    assert record.workspace_roots == ()
    assert SECRET not in json.dumps(asdict(record))


@pytest.mark.parametrize("value", [True, -1, "12.5", None, float("inf"), float("nan"), {}])
def test_invalid_rate_limit_measurement_is_ignored(value: object) -> None:
    event = fixture_events()[4]
    event["payload"]["rate_limits"]["primary"]["used_percent"] = value
    records = normalize_event(event, EVENT_CONTEXT)
    assert len(records) == 1
    assert records[0].window == "secondary"


def test_records_are_immutable_and_do_not_reference_mutable_source_data() -> None:
    event = fixture_events()[0]
    record = normalize_event(event, EVENT_CONTEXT)[0]
    event["payload"]["runtime_workspace_roots"].append(SECRET)
    assert record.workspace_roots == ("/synthetic/project",)
    for item in (EVENT_CONTEXT, *normalized_fixture_records()):
        with pytest.raises(FrozenInstanceError):
            setattr(item, fields(item)[0].name, None)
    assert [field.name for field in fields(EVENT_CONTEXT)] == [
        "source_path", "device", "inode", "byte_offset", "session_id", "thread_id",
    ]


@pytest.mark.parametrize("item_type,name", [
    ("FileChange", "apply_patch"),
    ("WebSearch", "web_search"),
    ("ImageView", "view_image"),
    ("ImageGeneration", "image_generation"),
])
def test_builtin_tool_names_do_not_come_from_content(item_type: str, name: str) -> None:
    event = tool_event()
    event["payload"]["item"]["type"] = item_type
    event["payload"]["item"]["name"] = SECRET
    event["payload"]["item"]["changes"] = {"file": {"content": SECRET}}
    event["payload"]["item"]["result"] = SECRET
    event["payload"]["item"]["revised_prompt"] = SECRET
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert record.name == name
    assert SECRET not in json.dumps(asdict(record))


@pytest.mark.parametrize("seconds,nanoseconds,expected", [(1, 250000000, 1250), (0, 0, 0)])
def test_structured_tool_duration_is_normalized_without_envelope_timing(
    seconds: int, nanoseconds: int, expected: int,
) -> None:
    event = tool_event()
    del event["payload"]["started_at_ms"]
    del event["payload"]["completed_at_ms"]
    event["payload"]["item"]["duration"] = {
        "secs": seconds, "nanos": nanoseconds, "unknown": {"text": SECRET},
    }
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert record.duration_ms == expected
    assert SECRET not in json.dumps(asdict(record))


@pytest.mark.parametrize("duration", [
    {"secs": True, "nanos": 0}, {"secs": -1, "nanos": 0},
    {"secs": 1, "nanos": 1000000000}, {"secs": 1, "nanos": SECRET}, SECRET,
])
def test_malformed_tool_duration_is_not_preserved(duration: object) -> None:
    event = tool_event()
    del event["payload"]["started_at_ms"]
    del event["payload"]["completed_at_ms"]
    event["payload"]["item"]["duration"] = duration
    record = normalize_event(event, EVENT_CONTEXT)[0]
    assert record.duration_ms is None
    assert SECRET not in json.dumps(asdict(record))
