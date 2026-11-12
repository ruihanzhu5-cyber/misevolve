import copy
import json

import pytest

from misevolve import harness
from misevolve.multiagent._io import read_jsonl
from misevolve.team import ChainRouter, TeamEpisodeScheduler, TeamWorkerPool
from misevolve.team.executor import TeamTaskExecutionError
from misevolve.team.recovery import validate_first_block
from test_team_phase3 import ScriptedRunner, coding_episode


@pytest.mark.parametrize('failed_pos', [2, 3])
def test_recovery_preserves_rows_and_learning_inputs_without_replaying_tasks(
    tmp_path, synthetic_llm, method_factory, monkeypatch, failed_pos,
):
    episode = coding_episode()
    executed, judged, learned = [], [], []
    recovering = False

    class Runner(ScriptedRunner):
        def open_task_session(self, **kwargs):
            executed.append(kwargs['task_id'])
            self.fail_agent = 'A' if not recovering and kwargs['task_id'].endswith(f'M{failed_pos:02}') else None
            super().open_task_session(**kwargs)

    def factory(*args, **kwargs):
        method = method_factory(*args, **kwargs)
        original = method.evolve_batch
        def evolve(traces, outcomes, **context):
            synthetic_llm(method)
            learned.extend(copy.deepcopy(t.messages) for t in traces)
            return original(traces, outcomes, **context)
        monkeypatch.setattr(method, 'evolve_batch', evolve)
        return method

    def judge(trace, *_):
        judged.append(trace.raw['task_run_id'])
        return harness.TaskOutcome(False, True)

    metadata = {'code_sha': 'original', 'execution': {}, 'judge': {}}
    def pool(resume=False):
        return TeamWorkerPool(tmp_path, runner_factory=lambda *_: Runner('chain'),
                              method_factory=factory, resume=resume)
    with pytest.raises(TeamTaskExecutionError):
        TeamEpisodeScheduler(pool(), ChainRouter, judge=judge, run_metadata=metadata).run(episode)
    prior = json.loads((tmp_path / 'all_rows.json').read_text())
    assert len(prior) == failed_pos and prior[-1]['execution_status'] == 'failed'
    assert not learned
    expected = []
    for row in prior[:-1]:
        path = tmp_path / 'tasks' / f"{row['pos']:02}-{row['task_run_id']}" / 'team_trajectory.json'
        messages = []
        for turn in json.loads(path.read_text())['raw']['turns']:
            from misevolve.multiagent._io import canonical_json
            messages.append({'role': 'user', 'content': canonical_json(turn['input'])})
            messages.extend(turn['messages'])
            if turn['final_text']:
                messages.append({'role': 'assistant', 'content': turn['final_text']})
        expected.append(messages)
    original_manifest = json.loads((tmp_path / 'team_manifest.json').read_text())
    changed = copy.deepcopy(original_manifest)
    changed['episode_sha256'] = 'changed'
    with pytest.raises(ValueError, match='episode_sha256'):
        validate_first_block(tmp_path, changed)
    recovering = True
    result = TeamEpisodeScheduler(pool(True), ChainRouter, judge=judge,
        run_metadata={**metadata, 'code_sha': 'repaired'}, recover_initial_block=True,
        stop_after_pos=3).run(episode)
    assert result[:failed_pos] == prior
    assert len(executed) == len(set(executed)) == 3
    assert len(judged) == len(set(judged)) == 2  # failed turn has no callable legacy Judge trace
    assert learned[:len(expected)] == expected
    assert len(learned) == 2
    assert json.loads((tmp_path / 'progress.json').read_text())['phase'] == 'paused'
    audit = json.loads((tmp_path / 'recovery_manifest.json').read_text())
    assert audit['task_replay'] is audit['judge_replay'] is False
    assert [r['pos'] for r in result if r['pos'] is not None] == [1, 2, 3]
    assert len([e for e in read_jsonl(tmp_path / 'team_events.jsonl') if e['event_type'] == 'TASK_COMPLETED']) == 3
