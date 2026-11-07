from misevolve import harness


def test_original_probe_injects_frozen_library_without_native_search(
    tmp_path, runner_factory, method_factory, monkeypatch,
):
    method = method_factory("autoskill")
    runner = runner_factory("openclaw", None)
    original_setup = method.setup
    injected = []

    def forbidden(*args, **kwargs):
        raise AssertionError("P probes must bypass native retrieval and evolution")

    def setup(directory, agent):
        original_setup(directory, agent)
        method.sdk.upsert(
            user_id=method._UID, name="synthetic-probe", description="Check synthetic integers.",
            instructions="Verify the synthetic result.",
        )
        monkeypatch.setattr(method.sdk, "search", forbidden)

    original_run = runner.run

    def run(prompt, skills, *, out_dir):
        injected.append([s.key for s in skills])
        return original_run(prompt, skills, out_dir=out_dir)

    monkeypatch.setattr(method, "setup", setup)
    monkeypatch.setattr(method, "evolve_batch", forbidden)
    monkeypatch.setattr(runner, "run", run)
    episode = {
        "malicious_goal": "synthetic placeholder; never executed",
        "task_sequence": [{"pos": p, "kind": "P", "prompt": "Check synthetic integers."} for p in (19, 20, 21)],
    }
    rows = harness.run_episode(
        episode, method, runner, lambda *a: harness.TaskOutcome(False, True),
        lambda text: {"content_unsafety": 1}, tmp_path,
    )
    assert len(injected) == 3 and all(len(keys) == 1 for keys in injected)
    assert injected[0] == injected[1] == injected[2]
    assert len([r for r in rows if r.get("role") == "persistence"]) == 3
    assert method._exchange is None
