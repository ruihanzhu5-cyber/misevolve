from misevolve.adapters import build_runner
from misevolve.adapters._normalize import read_backbone_key
from misevolve.backbones import get_backbone, list_backbones, list_harnesses, resolve_agent
from misevolve.judges.behavioral import _JUDGE_PROMPT_PATH, _UTIL_PROMPT_PATH
from misevolve.judges.config import artifact, assert_independent, behavioral


def test_paper_profile_is_default():
    backbone = get_backbone()
    assert backbone.id == "minimax-m2.7"
    assert backbone.model_id == "MiniMax-M2.7"
    assert backbone.anthropic_base_url


def test_four_harnesses_resolve_against_default_backbone():
    assert list_harnesses() == ["cc", "codex", "hermes", "openclaw"]
    assert "minimax-m2.7" in list_backbones()
    for harness_id in list_harnesses():
        resolved = resolve_agent(harness_id)
        assert resolved.backbone == "MiniMax-M2.7"
        assert resolved.image.startswith("misevolve-")


def test_judges_are_cross_family():
    assert behavioral().model != artifact().model
    assert_independent()


def test_judge_prompts_are_packaged():
    assert _JUDGE_PROMPT_PATH.is_file()
    assert _UTIL_PROMPT_PATH.is_file()


def test_backbone_credential_uses_declared_environment(monkeypatch):
    config = resolve_agent("codex")
    monkeypatch.setenv(config.key_env, "test-key")
    assert read_backbone_key(config) == "test-key"


def test_all_adapters_initialize_with_declared_credential(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    for harness_id in list_harnesses():
        runner = build_runner(harness_id)
        runner.isolate(tmp_path / harness_id)
        assert runner._auth == "test-key"
