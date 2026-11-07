import pytest

from misevolve.multiagent.models import MAX_SKILL_BYTES, UnsupportedSkillError, text_skill_files


@pytest.mark.parametrize("files", [
    {}, {"SKILL.md": ""}, {"SKILL.md": b"binary"}, {"SKILL.md": "\x00"},
    {"SKILL.md": "\ud800"}, {"SKILL.md": "x" * (MAX_SKILL_BYTES + 1)},
    {"SKILL.md": "text", "../outside": "extra"},
])
def test_unsupported_bundles_fail_without_silent_truncation(files):
    with pytest.raises(UnsupportedSkillError):
        text_skill_files(files)
