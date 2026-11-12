import json
import pytest
from misevolve import harness
from misevolve.multiagent._io import read_jsonl
from misevolve.team import PeerToPeerRouter, TeamEpisodeScheduler, TeamWorkerPool
from misevolve.team.recovery_prepare import prepare
from test_team_phase3 import ScriptedRunner, coding_episode


def test_fork_before_task11_keeps_prefix_skills_and_only_executes_11_12(tmp_path, synthetic_llm, method_factory, monkeypatch):
    source=tmp_path/'source'; target=tmp_path/'resumed'
    executed=[]; judged=[]; learned=[]
    class Runner(ScriptedRunner):
        def open_task_session(self, **kwargs):
            executed.append(kwargs['task_id'])
            super().open_task_session(**kwargs)
    def runner_factory(*_): return Runner('peer_to_peer')
    def native_factory(*args,**kwargs):
        method=method_factory(*args,**kwargs); original=method.evolve_batch
        def evolve(traces,outcomes,**context):
            synthetic_llm(method)
            learned.extend(method._task_by_traj[id(t)]['pos'] for t in traces)
            return original(traces,outcomes,**context)
        monkeypatch.setattr(method,'evolve_batch',evolve)
        return method
    def judge(trace,*_):
        judged.append(trace.raw['task_run_id'])
        return harness.TaskOutcome(False,True)
    metadata={'code_sha':'old','execution':{'image':'old-tag'},'judge':{},'runtime':{'image_id':'old-image'}}
    episode=coding_episode()
    pool=TeamWorkerPool(source,runner_factory=runner_factory,method_factory=native_factory)
    original=TeamEpisodeScheduler(pool,PeerToPeerRouter,judge=judge,run_metadata=metadata,stop_after_pos=12).run(episode)
    original_bytes=(source/'all_rows.json').read_bytes()
    result=prepare(source,target,next_pos=11,code_sha='fixed',image_id='new-image',runner_factory=runner_factory,method_factory=native_factory)
    assert result['preserved_tasks']==10
    assert result['native_ingest_counts']=={'A':9,'B':3,'C':3}
    assert (source/'all_rows.json').read_bytes()==original_bytes
    prefix=[r for r in original if r.get('pos') is not None and r['pos']<=10]
    assert [r for r in json.loads((target/'all_rows.json').read_text()) if r.get('pos') is not None]==prefix
    executed.clear();judged.clear();learned.clear()
    restored=TeamWorkerPool(target,resume=True,runner_factory=runner_factory,method_factory=native_factory)
    resumed=TeamEpisodeScheduler(restored,PeerToPeerRouter,judge=judge,
        run_metadata={**metadata,'code_sha':'fixed','execution':{'image':'new-tag'},'runtime':{'image_id':'new-image'}},
        recover_prepared_block=True,stop_after_pos=12).run(episode)
    assert {x[-2:] for x in executed}=={'11','12'}
    assert len(judged)==2
    assert learned==[10,11,12]*3
    assert [r for r in resumed if r.get('pos') is not None and r['pos']<=10]==prefix
    assert (source/'all_rows.json').read_bytes()==original_bytes
    assert len([e for e in read_jsonl(target/'team_events.jsonl') if e['event_type']=='TASK_COMPLETED'])==12
