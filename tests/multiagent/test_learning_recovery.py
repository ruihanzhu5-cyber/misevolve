import json
import pytest
from misevolve import harness
from misevolve.multiagent._io import read_jsonl
from misevolve.team import PeerToPeerRouter, TeamEpisodeScheduler, TeamWorkerPool
from misevolve.team.learning_recovery import prepare
from test_team_phase3 import ScriptedRunner, coding_episode


def test_partial_learning_preserves_completed_work_and_resumes_failed_ingest(tmp_path,synthetic_llm,method_factory,monkeypatch):
    source=tmp_path/'source';target=tmp_path/'fork'
    recovering=False;executed=[];judged=[];learned=[]
    class Runner(ScriptedRunner):
        def open_task_session(self,**kwargs):
            executed.append(kwargs['task_id']);super().open_task_session(**kwargs)
    def factory(*args,**kwargs):
        method=method_factory(*args,**kwargs);original=method.evolve_batch
        def evolve(traces,outcomes,**context):
            synthetic_llm(method)
            exchange=method._exchange
            def before(task):
                learned.append((exchange.worker_id,task['pos']))
                def after():
                    if not recovering and exchange.worker_id=='B' and task['pos']==12:
                        raise RuntimeError('synthetic invalid maintenance response')
                exchange.after_ingest=after
            exchange.before_ingest=before
            return original(traces,outcomes,**context)
        monkeypatch.setattr(method,'evolve_batch',evolve)
        return method
    def pool(root): return TeamWorkerPool(root,runner_factory=lambda *_:Runner('peer_to_peer'),method_factory=factory,resume=root==target)
    def judge(trace,*_):
        judged.append(trace.raw['task_run_id']);return harness.TaskOutcome(False,True)
    metadata={'code_sha':'old','execution':{},'judge':{},'runtime':{}}
    episode=coding_episode()
    with pytest.raises(RuntimeError,match='synthetic invalid'):
        TeamEpisodeScheduler(pool(source),PeerToPeerRouter,judge=judge,run_metadata=metadata,stop_after_pos=12).run(episode)
    prior=(source/'all_rows.json').read_bytes()
    prepare(source,target,'fixed')
    recovering=True;executed.clear();judged.clear();learned.clear()
    rows=TeamEpisodeScheduler(pool(target),PeerToPeerRouter,judge=judge,
        run_metadata={**metadata,'code_sha':'fixed','execution':{'maintainer_format_retries_per_request':2}},
        configure_workers=lambda _:None,ensure_healthy=lambda:None,recover_learning=True).run(episode)
    assert learned==[('B',12),('C',10),('C',11),('C',12)]
    assert executed==judged==[]
    assert [r for r in rows if r.get('pos') is not None]==[r for r in json.loads(prior) if r.get('pos') is not None]
    assert (source/'all_rows.json').read_bytes()==prior
    assert json.loads((target/'progress.json').read_text())=={'phase':'paused','last_completed_pos':12,'next_pos':13}
    assert (target/'recovery_archive/B-failed-ingest.json').exists()
    for a in 'ABC':
        events=[e for e in read_jsonl(target/'workers'/a/'evolution_events.jsonl') if e['task']['pos']>=10]
        assert [e['task']['pos'] for e in events]==[10,11,12]
        assert all(e['success'] for e in events)
