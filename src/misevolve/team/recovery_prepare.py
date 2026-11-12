"""Build a new recovery directory from durable observations, without model calls."""
import copy
import json
import shutil
from pathlib import Path

from misevolve.multiagent._io import atomic_json, append_jsonl, canonical_json, read_jsonl
from .assessment import behavior_summary
from .checkpoint import _state
from .episode import TeamEpisodeScheduler
from .executor import TeamWorkerPool


def prepare(source, target, *, next_pos, code_sha, image_id, runner_factory, method_factory=None):
    source,target=Path(source).resolve(),Path(target).resolve()
    if target.exists(): raise FileExistsError('recovery must use a new directory')
    if next_pos%3==1 or not 2<=next_pos<=18:
        raise ValueError('this recovery is for a task boundary inside a learning block')
    block_start=(next_pos-1)//3*3+1
    manifest=json.loads((source/'team_manifest.json').read_text())
    all_rows=json.loads((source/'all_rows.json').read_text())
    task_rows=[r for r in all_rows if r.get('pos') is not None and r['pos']<next_pos]
    if [r['pos'] for r in task_rows]!=list(range(1,next_pos)) or any(r['execution_status']!='completed' for r in task_rows):
        raise ValueError('completed prefix is not intact')
    task_positions={t['instance_id']:t['pos'] for t in manifest['tasks']}
    events=read_jsonl(source/'team_events.jsonl')
    cutoff=next(i for i,e in enumerate(events) if e['event_type']=='TASK_STARTED' and task_positions.get(e.get('task_id'))==next_pos)
    prefix_events=events[:cutoff]
    # Native evolution is block-based: only completed blocks before this block belong in the restored Store.
    registry=[r for r in read_jsonl(source/'registry/registry.jsonl') if int(r['block_id'].split('-')[-1]) < (block_start-1)//3+1]
    gids={r['global_skill_id'] for r in registry}
    retained=[r for r in all_rows if (r.get('pos') is not None and r['pos']<next_pos)
              or (r.get('kind')=='artifact' and r['global_skill_id'] in gids)]
    deliveries={e.get('message_id') for e in prefix_events if e['event_type'] in ('SKILL_TRANSFERRED','SKILL_IMPORTED','SKILL_TRANSFER_REPLAYED')}
    transfers=[r for r in read_jsonl(source/'registry/transfer_events.jsonl') if r.get('delivery_id') in deliveries]
    valid_transfers=[r for r in transfers if r['event']=='skill_transfer' and r.get('success')]
    target.mkdir(parents=True)
    for name in ('model_calls.jsonl','judge_results.jsonl','request_rejections.jsonl','late_executor_attempts.jsonl','provider_retries.jsonl'):
        if (source/name).exists(): shutil.copy2(source/name,target/name)
    if (source/'judge_responses').exists(): shutil.copytree(source/'judge_responses',target/'judge_responses')
    for row in task_rows:
        name=f"{row['pos']:02}-{row['task_run_id']}"
        shutil.copytree(source/'tasks'/name,target/'tasks'/name)
    for gid in gids:
        shutil.copytree(source/'registry/artifacts'/gid,target/'registry/artifacts'/gid)
    for name,records in [('team_events.jsonl',prefix_events),('registry/registry.jsonl',registry),('registry/transfer_events.jsonl',transfers)]:
        for row in records: append_jsonl(target/name,row)
    atomic_json(target/'all_rows.json',retained)
    atomic_json(target/'behavior_summary.json',behavior_summary(retained))
    atomic_json(target/'team_manifest.json',manifest)
    desired={}; pending={str(pos):{} for pos in range(block_start,next_pos)}
    provenance={}; last_learning={}
    for agent in 'ABC':
        base=source/'workers'/agent
        ingests=read_jsonl(base/'evolution_events.jsonl')
        retained_ingests=[e for e in ingests if e['task']['pos']<block_start]
        if any(not e['success'] for e in retained_ingests): raise ValueError('prefix contains failed native evolution')
        skills=copy.deepcopy(retained_ingests[-1]['after'] if retained_ingests else {})
        local_prov={}
        for e in retained_ingests:
            for local in e['after']: local_prov.setdefault(local,[e['task']['kind'],e['task']['round']])
        last_learning[agent]=[retained_ingests[-1]['task']['kind'],retained_ingests[-1]['task']['round']] if retained_ingests else ['M',0]
        provenance[agent]=local_prov
        current_block=next((e for e in ingests if e['task']['pos']==block_start),None)
        if current_block is None: raise ValueError('missing full pre-learning Store observation for '+agent)
        for local,bundle in skills.items():
            if canonical_json(bundle)!=canonical_json(current_block['before'].get(local)):
                raise ValueError('existing Skill changed outside block evolution: '+agent)
        selected={r['global_skill_id']:r['target_local_id'] for r in valid_transfers if r['target_agent']==agent}
        for gid,local in selected.items():
            if local in skills: continue
            bundle=current_block['before'].get(local)
            if not bundle or bundle.get('imported_from_global_id')!=gid: raise ValueError('missing imported Skill snapshot')
            skills[local]=copy.deepcopy(bundle)
        if any(s.get('resource_files') for s in skills.values()): raise ValueError('resource-bearing recovery needs explicit file snapshots')
        desired[agent]=skills
        for pos in pending:
            matches=[e for e in ingests if e['task']['pos']==int(pos)]
            if len(matches)!=1: raise ValueError('pending learning evidence missing or ambiguous')
            pending[pos][agent]={'retrieved':matches[0]['retrieved']}
        dst=target/'workers'/agent; dst.mkdir(parents=True)
        for e in retained_ingests: append_jsonl(dst/'evolution_events.jsonl',e)
        for e in read_jsonl(base/'task_events.jsonl'):
            if e.get('pos',999)<next_pos: append_jsonl(dst/'task_events.jsonl',e)
        exchange=json.loads((base/'exchange_state.json').read_text())
        exchange['imports']={gid:record for gid,record in exchange['imports'].items() if gid in selected}
        exchange['origins']={local:{'origin':b['origin'],'imported_from_global_id':b.get('imported_from_global_id'),'content_hash':b['content_hash']} for local,b in skills.items()}
        for gid,record in exchange['imports'].items():
            bundle=skills[record['local_id']]
            record['current_content_hash']=bundle['content_hash']
            record['receiver_locally_updated']=bundle['skill_files']!=record['imported_files']
        # Setup first with no mappings, then populate only observed prefix identities.
        atomic_json(dst/'exchange_state.json',{**exchange,'imports':{},'origins':{}})
        desired[agent]=(skills,exchange,selected)
    pool=TeamWorkerPool(target,resume=True,runner_factory=runner_factory,method_factory=method_factory)
    workers=pool.setup_workers()
    try:
        from autoskill.models import Skill,SkillExample,SkillStatus
        for agent,worker in workers.items():
            skills,exchange,selected=desired[agent]
            for local,bundle in skills.items():
                meta=bundle['native_metadata']
                native=Skill(**{**meta,'status':SkillStatus(meta['status']),
                    'examples':[SkillExample(**e) for e in meta['examples']]})
                worker.method.sdk.store.upsert(native,raw=meta)
            worker.method._exchange.origins=exchange['origins']; worker.method._exchange.imports=exchange['imports']
            worker.method._exchange.save()
            worker.imported_global_to_local=selected
            worker.local_to_global={r['local_skill_key']:r['global_skill_id'] for r in registry if r['owner_agent']==agent and r['local_skill_key'] in skills}
            for gid,local in selected.items(): worker.local_to_global.setdefault(local,gid)
            worker.save_state()
            worker.method._prov={k:tuple(v) for k,v in provenance[agent].items()}
            worker.method._last_kind,worker.method._last_round=last_learning[agent]
            if canonical_json(worker.method._exchange.snapshot_store())!=canonical_json(skills):
                raise ValueError('restored native Store differs from original observations: '+agent)
        snapshots=TeamEpisodeScheduler._snapshots(workers)
    finally: pool.teardown()
    # API and Judge journals include abandoned attempts for complete cost/audit history;
    # only retained task rows, prefix transfers and restored Stores drive execution.
    plan={'source_run':str(source),'next_pos':next_pos,'stores':snapshots,'pending_learning':pending,
          'prior_code_sha':manifest['code_sha'],'prior_image_id':manifest['runtime']['image_id'],
          'approved_code_sha':code_sha,'approved_image_id':image_id,'completed_task_replay':False,
          'excluded_original_positions':[r['pos'] for r in all_rows if r.get('pos') is not None and r['pos']>=next_pos],
          'historical_call_ledgers_preserved':True}
    atomic_json(target/'progress.json',{'phase':'prepared_recovery','next_pos':next_pos})
    plan['file_hashes']=_state(target)
    atomic_json(target/'block_recovery.json',plan)
    return {'next_pos':next_pos,'preserved_tasks':len(task_rows),'skill_counts':{a:len(s['skills']) for a,s in snapshots.items()},
            'native_ingest_counts':{a:s['ingests'] for a,s in snapshots.items()},'target':str(target)}
