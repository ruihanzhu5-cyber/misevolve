import json

import pytest

from misevolve.multiagent.models import UnsupportedSkillError, content_hash
from misevolve.multiagent.registry import GlobalSkillRegistry


def bundle(text="first", key="local-key", version="0.1.0"):
    return {
        "local_skill_key": key, "version": version,
        "skill_files": {"SKILL.md": f"---\nname: synthetic\n---\n{text}\n"},
        "native_metadata": {"metadata": {"nested": [1, 2]}, "source": {"type": "synthetic"}},
        "origin": "locally_created",
    }


def test_all_versions_preserved_deduplicated_and_recovered(tmp_path):
    registry = GlobalSkillRegistry(tmp_path)
    first = registry.register_skill("A", bundle(), ["task-1"], block_id="block-1")
    first_bytes = (tmp_path / "artifacts" / first.global_skill_id / "metadata.json").read_bytes()
    duplicate = registry.register_skill("A", bundle(), ["task-2"], block_id="block-1")
    assert duplicate.global_skill_id == first.global_skill_id
    second = registry.register_skill("A", bundle("second", version="0.1.1"), ["task-3"], block_id="block-1")
    assert first.global_skill_id != second.global_skill_id
    assert (tmp_path / "artifacts" / first.global_skill_id / "metadata.json").read_bytes() == first_bytes
    restored = GlobalSkillRegistry(tmp_path)
    assert len(restored.get_skills_by_owner("A")) == 2
    assert restored.checkpoint == 3
    assert restored.get_skill(first.global_skill_id).native_metadata == bundle()["native_metadata"]
    assert restored.select_transfer_versions("A", "block-1") == [second]
    assert restored.export_skill(first.global_skill_id) == first.skill_files


def test_final_per_key_uses_observation_order_even_after_content_reversion(tmp_path):
    registry = GlobalSkillRegistry(tmp_path)
    first = registry.register_skill("A", bundle(), block_id="one")
    checkpoint = registry.checkpoint
    registry.register_skill("A", bundle("changed"), block_id="two")
    other = registry.register_skill("A", bundle("other", key="other-key"), block_id="two")
    registry.register_skill("A", bundle(), block_id="two")
    assert registry.select_transfer_versions("A", "two") == [first, other]
    assert len(registry.get_new_versions("A", checkpoint)) == 3
    assert GlobalSkillRegistry(tmp_path).select_transfer_versions("A", "two") == [first, other]
    assert registry.select_transfer_versions("B", "two") == []


def test_hash_normalizes_only_line_endings_and_identity_is_unambiguous(tmp_path):
    assert content_hash({"SKILL.md": "a\r\nb\r"}) == content_hash({"SKILL.md": "a\nb\n"})
    registry = GlobalSkillRegistry(tmp_path)
    a = registry.register_skill("a", bundle(key="bc"))
    b = registry.register_skill("ab", bundle(key="c"))
    assert a.global_skill_id != b.global_skill_id
    exported = registry.export_skill(a.global_skill_id)
    exported["SKILL.md"] = "mutated external copy"
    assert registry.export_skill(a.global_skill_id) == a.skill_files


def test_retrieval_does_not_create_parent_edges(tmp_path):
    registry = GlobalSkillRegistry(tmp_path)
    parent = registry.register_skill("A", bundle())
    child = registry.register_skill("B", bundle("new"), provenance={
        "retrieved_parent_ids": [parent.global_skill_id],
    })
    assert child.parent_skill_ids == []
    assert child.invoked_parent_ids == []
    with pytest.raises(ValueError, match="independent derivation"):
        registry.register_skill("C", bundle(), provenance={"parent_skill_ids": [parent.global_skill_id]})
    proven = registry.register_skill("C", bundle(), provenance={
        "parent_skill_ids": [parent.global_skill_id],
        "parent_evidence": {parent.global_skill_id: {"type": "native_revision", "source_ref": "synthetic-event"}},
    })
    assert proven.parent_skill_ids == [parent.global_skill_id]


def test_unsupported_resources_and_corrupt_history_fail_explicitly(tmp_path):
    registry = GlobalSkillRegistry(tmp_path)
    bad = bundle()
    bad["skill_files"]["references/extra.md"] = "extra"
    with pytest.raises(UnsupportedSkillError):
        registry.register_skill("A", bad)
    artifact = registry.register_skill("A", bundle())
    (tmp_path / "artifacts" / artifact.global_skill_id / "SKILL.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        GlobalSkillRegistry(tmp_path)


def test_recovery_of_complete_orphan_never_overwrites_it(tmp_path):
    registry = GlobalSkillRegistry(tmp_path)
    artifact = registry.register_skill("A", bundle())
    registry.journal.unlink()  # Simulate interruption before the first journal append.
    restored = GlobalSkillRegistry(tmp_path)
    assert restored.register_skill("A", bundle()) == artifact
    with registry.journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "partial"}))
    with pytest.raises(ValueError, match="incomplete JSONL"):
        GlobalSkillRegistry(tmp_path)
