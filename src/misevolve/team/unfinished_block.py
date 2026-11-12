"""Prepare continuation after recorded tasks, before this block's evolution began."""
import copy
import json
import shutil
from pathlib import Path

from misevolve.multiagent._io import atomic_json,read_jsonl,canonical_json
from .checkpoint import _state
from .executor import TeamWorkerPool
from .episode import TeamEpisodeScheduler


def prepare(source,target,*,next_pos,code_sha,runner_factory,method_factory=None,backbone=None):
    source,target=Path(source).resolve(),Path(target).resolve()
    if target.exists():raise FileExistsError('continuation needs a new fork')
    first=(next_pos-1)//3*3+1
    if first==next_pos or not 2<=next_pos<=18:raise ValueError('expected an unfinished learning block')
    read=lambda p:json.loads(p.read_text(encoding='utf8'))
    manifest=read(source/'team_manifest.json');rows=read(source/'all_rows.json')
    prefix=[r for r in rows if r.get('pos') is not None]
    if [r['pos'] for r in prefix]!=list(range(1,next_pos)) or any(r['execution_status'] not in ('completed','timeout') for r in prefix):
        raise ValueError('recorded prefix has unexpected outcomes')
    histories={a:read_jsonl(source/'workers'/a/'evolution_events.jsonl') for a in 'ABC'}
    if any(not e['success'] or e['task']['pos']>=first for h in histories.values() for e in h):
        raise ValueError('pending block already has native learning or unhandled failed learning')
    shutil.copytree(source,target)
    pool=TeamWorkerPool(target,runner_factory=runner_factory,method_factory=method_factory,backbone=backbone,resume=True)
    workers=pool.setup_workers();pending={};reload_imports=[]
    try:
        from autoskill.models import Skill,SkillExample,SkillStatus
        for a,w in workers.items():
            exchange=w.method._exchange;current=exchange.snapshot_store()
            known=histories[a][-1]['after'] if histories[a] else {}
            for local,bundle in known.items():
                if local not in current or current[local]['skill_files']!=bundle['skill_files']:
                    raise ValueError('Store changed outside observed learning/imports')
                meta=bundle['native_metadata']
                w.method.sdk.store.upsert(Skill(**{**meta,'status':SkillStatus(meta['status']),
                    'examples':[SkillExample(**e) for e in meta['examples']]}),raw=meta)
                current[local]=copy.deepcopy(bundle)
            for local in set(current)-set(known):
                if current[local]['origin']!='imported':raise ValueError('unobserved native mutation')
                reload_imports.append({'agent':a,'local_id':local})
            if canonical_json(exchange.snapshot_store())!=canonical_json(current):raise ValueError('Store restore mismatch')
            w.method._prov={}
            for event in histories[a]:
                for local in event['after']:
                    w.method._prov.setdefault(local,(event['task']['kind'],event['task']['round']))
            if histories[a]:
                task=histories[a][-1]['task'];w.method._last_kind,w.method._last_round=task['kind'],task['round']
            task_events=read_jsonl(w.episode_dir/'task_events.jsonl')
            for pos in range(first,next_pos):
                row=prefix[pos-1]
                if row['execution_status']=='timeout':continue
                aggregate_id=f"{row['task_run_id']}:{a}"
                invocations=[e for e in task_events if e['event']=='skill_invocation' and e.get('pos')==pos]
                for event in invocations:
                    if not any(e['task_run_id']==aggregate_id and e['skill']==event['skill'] and e['evidence']==event['evidence'] for e in invocations):
                        raise ValueError('completed learning trace lacks durable invocation mapping')
                refs={}
                for event in task_events:
                    if event['event']=='task_retrieval' and event.get('pos')==pos:
                        for ref in event['retrieved']:refs[(ref['local_skill_id'],ref['content_hash'])]=ref
                bundles=[]
                for (local,digest),ref in refs.items():
                    bundle=current[local]
                    if bundle['content_hash']!=digest:raise ValueError('retrieval version changed within pending block')
                    bundles.append(bundle)
                if a=='A' or row['task_kind']!='M':
                    pending.setdefault(str(pos),{})[a]={'retrieved':bundles}
        snapshots=TeamEpisodeScheduler._snapshots(workers)
    finally:
        pool.teardown()
    plan={'source_run':str(source),'next_pos':next_pos,'stores':snapshots,'pending_learning':pending,
          'prior_code_sha':manifest['code_sha'],'prior_image_id':manifest['runtime']['image_id'],
          'approved_code_sha':code_sha,'approved_image_id':manifest['runtime']['image_id'],
          'completed_task_replay':False,'excluded_original_positions':[],
          'preserved_timeout_positions':[r['pos'] for r in prefix if r['execution_status']=='timeout'],
          'import_metadata_from_native_disk':reload_imports,'historical_call_ledgers_preserved':True}
    atomic_json(target/'progress.json',{'phase':'prepared_recovery','next_pos':next_pos})
    plan['file_hashes']={k:v for k,v in _state(target).items() if k!='block_recovery.json'}
    atomic_json(target/'block_recovery.json',plan)
    return {'next_pos':next_pos,'preserved_tasks':len(prefix),'pending_learning_positions':list(pending),
            'preserved_timeout_positions':plan['preserved_timeout_positions']}
