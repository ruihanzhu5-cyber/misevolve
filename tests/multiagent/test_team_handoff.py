"""Exercise binary delivery policy without network, Docker, or model calls."""

from types import SimpleNamespace

from misevolve.multiagent._io import read_jsonl
from misevolve.team.handoff import deliver_artifact
from misevolve.team.router import ChainRouter, TeamEventLogger, TeamMessage


def test_handoff_is_edge_scoped_and_exposes_only_verified_receipt(tmp_path):
    path = tmp_path / "events.jsonl"
    router = ChainRouter(TeamEventLogger(path, "episode", "chain"))
    data = b"\x00\xff\x03"
    read_paths = []
    written = []
    usage = {"files": 0, "bytes": 0}

    def read(relative):
        read_paths.append(relative)
        return data

    def receive(delivery_id, name, contents):
        assert contents == data
        written.append((delivery_id, name))
        return {"relative_path": f".misevolve-handoffs/{delivery_id}/{name}",
                "sha256": "a" * 64, "size_bytes": len(contents)}

    def message(src, dst, relative):
        return TeamMessage("episode", "task", src, dst, "ARTIFACT_HANDOFF", {"relative_path": relative})

    runners = {"A": SimpleNamespace(read_artifact=read), "B": SimpleNamespace(receive_artifact=receive)}
    assert deliver_artifact(router, message("A", "C", "fixture.bin"), runners, usage) is None
    assert deliver_artifact(router, message("A", "B", "../fixture.bin"), runners, usage) is None
    valid = message("A", "B", "fixture.bin")
    delivered = deliver_artifact(router, valid, runners, usage)
    assert delivered.message_id == valid.message_id and delivered.payload["sha256"] == "a" * 64
    assert read_paths == ["fixture.bin"] and written == [(valid.message_id, "fixture.bin")]
    assert usage == {"files": 1, "bytes": 3}
    events = read_jsonl(path)
    assert [event["reason"] for event in events if event["event_type"] == "MESSAGE_BLOCKED"] == [
        "edge_or_type_not_allowed", "relative_artifact_path_required",
    ]
    delivered_event = next(event for event in events if event["event_type"] == "ARTIFACT_HANDED_OFF")
    assert delivered_event["sha256"] == delivered.payload["sha256"]
