"""Resume an audited fork at a task boundary inside a learning block.

Preparation is separate: no existing run is rewritten and no model is called here.
"""
import hashlib
import json

from misevolve.harness import Trajectory
from misevolve.multiagent._io import atomic_json, canonical_json
from .checkpoint import _state
from .executor import AgentRuntime


def load(root, expected):
    plan=json.loads((root/'block_recovery.json').read_text(encoding='utf8'))
    if json.loads((root/'progress.json').read_text(encoding='utf8')) != {'phase':'prepared_recovery','next_pos':plan['next_pos']}:
        raise ValueError('prepared recovery was already started or changed')
    if plan['file_hashes'] != {k:v for k,v in _state(root).items() if k!='block_recovery.json'}:
        raise ValueError('prepared recovery evidence changed')
    old=json.loads((root/'team_manifest.json').read_text(encoding='utf8'))
    for key in ('episode_id','communication_protocol','edges','agents','max_rounds','tasks',
                'exposure_submitter','team_submitter','probe_policy','delivery_policy','skill_read_evidence',
                'assessment_schema_version','episode_sha256','run_id','replicate_id','artifact_cache'):
        if canonical_json(old.get(key)) != canonical_json(expected.get(key)):
            raise ValueError('recovery changes protocol field: '+key)
    for section, allowed in {
        'execution': {'image','maintainer_format_retries_per_request','judge_format_failures_stop_episode','provider_retries_per_request','provider_transport_policy','repeat_executor_timeouts_stop_episode'},
        'judge': {'fingerprint','max_transport_attempts_per_request','provider_transport_policy'},
        'runtime': {'image_id'},
    }.items():
        if {k:v for k,v in old[section].items() if k not in allowed} != {k:v for k,v in expected[section].items() if k not in allowed}:
            raise ValueError('recovery changes non-repair configuration: '+section)
    if expected['code_sha'] != plan['approved_code_sha'] or expected['runtime']['image_id'] != plan['approved_image_id']:
        raise ValueError('recovery code or image differs from approved repair')
    rows=json.loads((root/'all_rows.json').read_text(encoding='utf8'))
    prefix=[r for r in rows if r.get('pos') is not None]
    if [r['pos'] for r in prefix] != list(range(1,plan['next_pos'])) or any(r['execution_status'] not in ('completed','timeout') for r in prefix):
        raise ValueError('recovery requires an intact completed/timeout task prefix')
    if [(r['pos'],r['orig_id']) for r in prefix] != [(t['pos'],t['instance_id']) for t in expected['tasks'][:plan['next_pos']-1]]:
        raise ValueError('recovery task prefix differs from episode')
    return plan, rows


def restore_pending(root, row, task, workers, plan):
    path=root/'tasks'/f"{row['pos']:02}-{row['task_run_id']}"/'team_trajectory.json'
    relative=path.relative_to(root).as_posix()
    if hashlib.sha256(path.read_bytes()).hexdigest()!=plan['file_hashes'][relative]:
        raise ValueError('completed pending trajectory changed')
    turns=json.loads(path.read_text(encoding='utf8'))['raw']['turns']
    result={}
    for agent, evidence in plan['pending_learning'][str(task['pos'])].items():
        worker=workers[agent]
        runtime=AgentRuntime(worker,worker.runner,path.parent/agent,f"{row['task_run_id']}:{agent}")
        for turn in turns:
            if turn['agent_id']!=agent: continue
            if turn['execution_error']: raise ValueError('completed pending task contains a failed turn')
            runtime.inputs.append(turn['input'])
            runtime.traces.append(Trajectory(turn['messages'],turn['final_text'],turn['files_changed'],None,turn['trajectory']))
        trace=runtime.learning_trace(task)
        if trace is None: raise ValueError('missing pending learning trace')
        worker.method._retrieved_by_traj[id(trace)]=evidence['retrieved']
        result[agent]=trace
    return result


def mark_started(root, manifest, plan):
    atomic_json(root/'team_manifest.json',{**manifest,'recovery_history':[{
        'source_run':plan['source_run'],'next_pos':plan['next_pos'],
        'prior_code_sha':plan['prior_code_sha'],'new_code_sha':manifest['code_sha'],
        'prior_image_id':plan['prior_image_id'],'new_image_id':manifest['runtime']['image_id'],
        'details':'block_recovery.json','completed_task_replay':False,
    }]})
    atomic_json(root/'progress.json',{'phase':'resuming','next_pos':plan['next_pos']})
