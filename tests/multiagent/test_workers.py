from pathlib import Path

import pytest

from misevolve.multiagent.models import WorkerRuntime
from misevolve.multiagent.orchestrator import MultiAgentOrchestrator


def test_four_native_stores_are_isolated(tmp_path, runner_factory, seed_skill):
    pool = MultiAgentOrchestrator(tmp_path, runner_factory=runner_factory)
    workers = pool.setup_workers()
    assert list(workers) == list("ABCD")
    for attr in ("runner", "method"):
        assert len({id(getattr(w, attr)) for w in workers.values()}) == 4
    assert len({id(w.method.sdk) for w in workers.values()}) == 4
    assert len({id(w.method.sdk.store) for w in workers.values()}) == 4
    assert len({id(w.method.sdk.maintainer) for w in workers.values()}) == 4
    assert len({Path(w.method.sdk.store.path) for w in workers.values()}) == 4
    seed_skill(workers["A"])
    for wid in "BCD":
        assert workers[wid].method.sdk.list(user_id="skillmisevo") == []
        assert workers[wid].method.sdk.search("synthetic arithmetic", user_id="skillmisevo") == []
    seed_skill(workers["A"], instructions="Updated arithmetic check.")
    for wid in "BCD":
        assert workers[wid].method.sdk.get("synthetic-source") is None
    runners = [w.runner for w in workers.values()]
    pool.teardown()
    assert all(r.closed for r in runners)


def test_production_initialization_never_launches_docker(tmp_path):
    pool = MultiAgentOrchestrator(tmp_path)
    workers = pool.setup_workers()
    assert all(w.runner.container is None for w in workers.values())
    assert all(type(w.runner).__name__ == "OpenClawRunner" for w in workers.values())
    pool.teardown()


def test_partial_setup_failure_cleans_current_and_earlier_workers(tmp_path, runner_factory, method_factory):
    runners, methods = [], []

    def make_runner(*args):
        runner = runner_factory(*args)
        runners.append(runner)
        return runner

    def make_method(*args, **kwargs):
        method = method_factory(*args, **kwargs)
        methods.append(method)
        method.closed = False

        def teardown():
            method.closed = True
            if method is methods[0]:
                raise RuntimeError("synthetic cleanup failure")

        method.teardown = teardown
        if len(methods) == 3:
            def fail(*args):
                raise RuntimeError("synthetic setup failure")

            method.setup = fail
        return method

    pool = MultiAgentOrchestrator(tmp_path, runner_factory=make_runner, method_factory=make_method)
    with pytest.raises(RuntimeError, match="synthetic setup failure") as caught:
        pool.setup_workers()
    assert all(r.closed for r in runners)
    assert all(m.closed for m in methods)
    assert len(runners) == 3
    assert not pool.workers
    assert "cleanup failed" in " ".join(caught.value.__notes__)


def test_nonempty_state_requires_explicit_resume(tmp_path, runner_factory, seed_skill):
    pool = MultiAgentOrchestrator(tmp_path, runner_factory=runner_factory)
    workers = pool.setup_workers()
    seed_skill(workers["A"])
    workers["A"].remember_local_version("synthetic-source", "global-test-id")
    pool.teardown()
    with pytest.raises(FileExistsError):
        pool.setup_workers()
    restored = MultiAgentOrchestrator(tmp_path, runner_factory=runner_factory, resume=True)
    workers = restored.setup_workers()
    assert workers["A"].method.sdk.get("synthetic-source") is not None
    assert workers["A"].local_to_global["synthetic-source"] == "global-test-id"
    assert workers["B"].method.sdk.list(user_id="skillmisevo") == []
    restored.teardown()


def test_worker_path_and_count_validation(tmp_path):
    with pytest.raises(ValueError):
        WorkerRuntime("../escape", None, None, tmp_path)
    with pytest.raises(ValueError):
        MultiAgentOrchestrator(tmp_path, worker_ids=("A", "A", "C", "D"))
