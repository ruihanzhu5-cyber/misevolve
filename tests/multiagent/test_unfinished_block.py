import json
from dataclasses import replace
import pytest
from misevolve import harness
from misevolve.team import PeerToPeerRouter,TeamEpisodeScheduler,TeamWorkerPool,TeamExecutor
from misevolve.team.unfinished_block import prepare
from test_team_phase3 import ScriptedRunner,coding_episode


def test_continue_after_timeout_keeps_rows_and_only_learns_completed_tasks(tmp_path,method_factory,synthetic_llm,monkeypatch):
    source=tmp_path/'source';target=tmp_path/'fork';executed=[];learned=[];judged=[]
    original=TeamExecutor.run
    def execute(self,task,**kwargs):
        executed.append(task['pos']);result=original(self,task,**kwargs)
        return replace(result,execution_status='timeout',failure={'phase':'turn','stop_reason':'task_timeout'}) if task['pos']==5 else result
    monkeypatch.setattr(TeamExecutor,'run',execute)
    def factory(*args,**kwargs):
        method=method_factory(*args,**kwargs);evolve=method.evolve_batch
        def learning(traces,outcomes,**context):
            synthetic_llm(method)
            learned.extend((method._exchange.worker_id,method._task_by_traj[id(t)]['pos']) for t in traces)
            return evolve(traces,outcomes,**context)
        monkeypatch.setattr(method,'evolve_batch',learning);return method
    runner=lambda *_:ScriptedRunner('peer_to_peer')
    def judge(trace,*_):judged.append(trace.raw['task_run_id']);return harness.TaskOutcome(False,True)
    def stop():
        p=source/'all_rows.json'
        if p.exists() and any(r.get('pos')==5 for r in json.loads(p.read_text())):
            raise RuntimeError('old aggregate Judge circuit')
    metadata={'code_sha':'old','execution':{},'judge':{},'runtime':{'image_id':'same'}}
    ep=coding_episode()
    with pytest.raises(RuntimeError,match='old aggregate'):
        TeamEpisodeScheduler(TeamWorkerPool(source,runner_factory=runner,method_factory=factory),PeerToPeerRouter,
            judge=judge,run_metadata=metadata,ensure_healthy=stop).run(ep)
    before=(source/'all_rows.json').read_bytes();prefix=json.loads(before)
    summary=prepare(source,target,next_pos=6,code_sha='fixed',runner_factory=runner,method_factory=factory)
    assert summary['preserved_timeout_positions']==[5]
    assert summary['pending_learning_positions']==['4']
    executed.clear();learned.clear();judged.clear()
    result=TeamEpisodeScheduler(TeamWorkerPool(target,resume=True,runner_factory=runner,method_factory=factory),
        PeerToPeerRouter,judge=judge,run_metadata={**metadata,'code_sha':'fixed','execution':{'judge_format_failures_stop_episode':False}},
        recover_prepared_block=True,stop_after_pos=6).run(ep)
    assert executed==[6] and len(judged)==1
    assert learned==[(a,p) for a in 'ABC' for p in (4,6)]
    assert result[:len(prefix)]==prefix
    assert (source/'all_rows.json').read_bytes()==before
