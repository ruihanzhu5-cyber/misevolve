from types import SimpleNamespace

from misevolve.scope.mock import mock_block, mock_cases, mock_version
from misevolve.scope.services import KimiReviewer, MisevolveJudgeScorer


def test_kimi_client_is_independent_and_keeps_usage_and_raw_evidence():
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"mechanisms":["x"]}'))],
                               usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 7}))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    reviewer = KimiReviewer(client, model="explicit-test-kimi", max_tokens=123)
    review = reviewer({"stage": "group", "candidates": []})
    assert review["mechanisms"] == ["x"] and review["usage"]["total_tokens"] == 7
    assert sent[0]["model"] == "explicit-test-kimi" and sent[0]["max_tokens"] == 123
    assert "publication authority" in sent[0]["messages"][0]["content"]


def test_invalid_review_cannot_become_an_approval(system):
    store, base, factory, validator, controller = system
    controller.reviewer = lambda request: {"status": "invalid", "raw": "approve everything"}
    cid = store.stage(base=base, skill_id="A:skill", after=mock_version())
    result = controller.run("invalid-review", base=base, candidates=[cid],
                            cases=mock_cases(), block_tasks=mock_block())
    assert result["status"] == "hold" and not factory.executions


def test_deepseek_service_delegates_existing_assessment_and_artifact_rubrics(monkeypatch, tmp_path):
    from misevolve.team import assessment

    calls = []
    judge = SimpleNamespace(artifact=lambda artifact: {"content_unsafety": 2})
    result = object()

    def assess(actual_judge, actual_result, episode, task):
        calls.append((actual_judge, actual_result, episode, task))
        return {"team_verdict": {"utility": True}}

    monkeypatch.setattr(assessment, "assess_task", assess)
    service = MisevolveJudgeScorer(judge, {"malicious_goal": "test"}, lambda record, **kwargs: result)
    assert service({}, {"prompt": "test"}, out_dir=tmp_path)["team_verdict"]["utility"]
    assert calls[0][0] is judge and calls[0][1] is result
    assert service.artifact(object()) == {"content_unsafety": 2}
