"""Isolated worker lifecycle for the multi-agent episode layer."""

from __future__ import annotations

from pathlib import Path

from misevolve.adapters import build_runner
from misevolve.methods import build_method

from .models import WorkerRuntime


class MultiAgentOrchestrator:
    def __init__(
        self, run_dir: Path, *, backbone: str | None = None,
        worker_ids=("A", "B", "C", "D"), resume: bool = False,
        runner_factory=None, method_factory=None,
    ):
        if len(worker_ids) != 4 or len(set(worker_ids)) != 4:
            raise ValueError("the pilot requires four distinct workers")
        # Validate path components before creating directories or resources.
        for worker_id in worker_ids:
            WorkerRuntime(worker_id, None, None, Path(run_dir) / "workers" / worker_id)
        self.run_dir = Path(run_dir).resolve()
        self.backbone = backbone
        self.worker_ids = tuple(worker_ids)
        self.resume = resume
        self.runner_factory = runner_factory or build_runner
        self.method_factory = method_factory or build_method
        self.workers: dict[str, WorkerRuntime] = {}

    def setup_workers(self) -> dict[str, WorkerRuntime]:
        if self.workers:
            raise RuntimeError("workers are already initialized")
        roots = [self.run_dir / "workers" / wid for wid in self.worker_ids]
        for root in roots:
            if not self.resume and root.exists() and any(root.iterdir()):
                raise FileExistsError(f"nonempty worker directory; use explicit resume: {root}")
            if root.is_symlink() or not root.resolve().is_relative_to(self.run_dir):
                raise ValueError(f"worker path escapes run directory: {root}")
        runner = method = None
        try:
            for worker_id, root in zip(self.worker_ids, roots, strict=True):
                runner = self.runner_factory("openclaw", self.backbone)
                method = self.method_factory("autoskill", backbone=self.backbone)
                root.mkdir(parents=True, exist_ok=True)
                runner.isolate(root)
                method.setup(root, runner)
                method.enable_skill_exchange(worker_id)
                worker = WorkerRuntime(worker_id, runner, method, root)
                worker.restore_state()
                worker.save_state()
                self.workers[worker_id] = worker
                runner = method = None
        except BaseException as exc:
            errors = self._cleanup_objects(method, runner)
            errors.extend(self._teardown_all())
            for error in errors:
                exc.add_note(f"cleanup failed: {error!r}")
            raise
        return self.workers

    @staticmethod
    def _cleanup_objects(*objects) -> list[Exception]:
        errors = []
        for obj in objects:
            if obj is not None:
                try:
                    obj.teardown()
                except Exception as exc:
                    errors.append(exc)
        return errors

    def _teardown_all(self) -> list[Exception]:
        errors = []
        for worker in reversed(list(self.workers.values())):
            errors.extend(self._cleanup_objects(worker.method, worker.runner))
        self.workers.clear()
        return errors

    def teardown(self) -> None:
        errors = self._teardown_all()
        if errors:
            raise ExceptionGroup("worker cleanup failed", errors)
