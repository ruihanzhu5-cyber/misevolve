"""Retain real transcript integrity while allowing native deferred exec metadata."""
from copy import deepcopy

import pytest
from test_team_safety import assistant, openclaw  # noqa: F401

from misevolve.team.openclaw import _history_prefix_annotations


def notice():
    return {"id": "notice-1", "type": "message", "timestamp": "fixed", "message": {
        "role": "user", "content": "Exec completed (synthetic, code 0)", "idempotencyKey": "exec:synthetic",
        "provenance": {"kind": "internal_system", "sourceTool": "exec"},
        "__openclaw": {"senderIsOwner": False, "transport": {"channel": "webchat", "messageId": "fixed"}},
    }}


def projected(record):
    result = deepcopy(record)
    result["message"]["__openclaw"]["modelPromptProjection"] = {"version": 1, "text": "Synthetic continuation."}
    return result


def test_exact_append_and_truncation():
    old = [notice()]
    assert _history_prefix_annotations(old, old + [assistant("stop")]) == []
    assert _history_prefix_annotations(old, []) is None


def test_one_time_native_projection_is_audited_without_mutating_prior_records():
    before = notice()
    after = projected(before)
    saved = deepcopy(before)
    audit = _history_prefix_annotations([before], [after, assistant("stop")])
    assert before == saved
    assert len(audit) == 1 and audit[0]["record_id"] == "notice-1"
    assert audit[0]["before_sha256"] != audit[0]["after_sha256"]
    assert audit[0]["projection"] == after["message"]["__openclaw"]["modelPromptProjection"]
    assert _history_prefix_annotations([after], [after, assistant("stop")]) == []


@pytest.mark.parametrize("mutation", ["content", "tool_result", "record_id", "owner", "provenance",
                                      "transport", "version", "projection_extra", "projection_rewrite",
                                      "projection_removal", "ordinary_assistant"])
def test_other_history_changes_remain_fatal(mutation):
    before = notice()
    after = projected(before)
    if mutation == "content":
        after["message"]["content"] = "Altered command result"
    elif mutation == "tool_result":
        before["message"]["role"] = after["message"]["role"] = "toolResult"
    elif mutation == "record_id":
        after["id"] = "different"
    elif mutation == "owner":
        after["message"]["__openclaw"]["senderIsOwner"] = True
    elif mutation == "provenance":
        after["message"]["provenance"]["sourceTool"] = "other"
    elif mutation == "transport":
        after["message"]["__openclaw"]["transport"]["messageId"] = "different"
    elif mutation == "version":
        after["message"]["__openclaw"]["modelPromptProjection"]["version"] = True
    elif mutation == "projection_extra":
        after["message"]["__openclaw"]["modelPromptProjection"]["extra"] = "unrecognized"
    elif mutation == "projection_rewrite":
        before = projected(before)
        after["message"]["__openclaw"]["modelPromptProjection"]["text"] = "changed"
    elif mutation == "projection_removal":
        before, after = after, before
    else:
        for record in (before, after):
            record["message"]["role"] = "assistant"
    assert _history_prefix_annotations([before], [after, assistant("stop")]) is None


@pytest.mark.parametrize("metadata", [True, False])
def test_native_projection_is_supported_for_ordinary_user_turns_too(metadata):
    before = {"id": "user-1", "type": "message", "message": {"role": "user", "content": "unchanged"}}
    if metadata:
        before["message"]["__openclaw"] = {"senderIsOwner": True}
    after = deepcopy(before)
    after["message"].setdefault("__openclaw", {})["modelPromptProjection"] = {"version": 1, "text": "unchanged"}
    assert len(_history_prefix_annotations([before], [after])) == 1


def test_same_session_continues_after_native_projection_without_replaying_history(
    openclaw, tmp_path, monkeypatch,  # noqa: F811
):
    first = [assistant("stop", "first"), notice()]
    second = [first[0], projected(first[1]), assistant("stop", "second")]
    histories = iter((deepcopy(first), deepcopy(second)))
    monkeypatch.setattr(openclaw, "_http", lambda method, *a, **k:
                        (200, {}) if method == "POST" else (200, {"history": next(histories)}))
    a = openclaw.continue_task_session("first", [], out_dir=tmp_path / "first")
    b = openclaw.continue_task_session("second", [], out_dir=tmp_path / "second")
    assert not a.error and not b.error
    assert a.final_text == "first" and b.final_text == "second"
    assert a.raw["session_id"] == b.raw["session_id"]
    assert a.raw["history"] == first
    assert b.raw["history"] == [second[-1]]
    assert len(b.raw["_transcript_annotations"]) == 1
