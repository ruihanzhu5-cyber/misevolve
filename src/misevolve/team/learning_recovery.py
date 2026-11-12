"""Finish a partially committed learning block in a separate audited fork."""
import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from misevolve.multiagent._io import atomic_json, append_jsonl, canonical_json, read_jsonl
from misevolve.multiagent.registry import GlobalSkillRegistry
from misevolve.multiagent.provenance import ProvenanceTracker
from .block_recovery import restore_pending
from .checkpoint import _state, save
from .router import TeamEventLogger
from .skills import SkillTransferService


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def prepare(source, target, code_sha):
    source,target=Path(source).resolve(),Path(target).resolve()
    if target.exists(): raise FileExistsError('learning recovery requires a new fork')
    progress=read(source/'progress.json')
    if progress['phase']!='native_evolution': raise ValueError('not interrupted native evolution')
    rows=read(source/'all_rows.json');tasks=[r for r in rows if r.get('pos') is not None]
    last=tasks[-1]['pos']
    if last not in (3,6,9,12,15,18) or [r['pos'] for r in tasks]!=list(range(1,last+1)):
        raise ValueError('task prefix is not a complete block')
    if any(r['execution_status']!='completed' for r in tasks[-3:]):
        raise ValueError('pending block contains incomplete execution')
    shutil.copytree(source,target)
    plan={'source_run':str(source),'approved_code_sha':code_sha,'last_pos':last,
          'source_hashes':_state(source),'successful_task_replay':False,'successful_ingest_replay':False}
    atomic_json(target/'progress.json',{'phase':'prepared_learning_recovery','last_pos':last})
    plan['fork_hashes']=_state(target)
    atomic_json(target/'learning_recovery_plan.json',plan)


def recover(scheduler, episode):
    root=scheduler.pool.run_dir;plan=read(root/'learning_recovery_plan.json')
    last=plan['last_pos'];first=last-2;block_id=f'block-{last//3}'
    if read(root/'progress.json')!={'phase':'prepared_learning_recovery','last_pos':last}:
        raise ValueError('prepared learning recovery already started')
    if plan['fork_hashes']!={k:v for k,v in _state(root).items() if k!='learning_recovery_plan.json'}:
        raise ValueError('prepared learning evidence changed')
    old=read(root/'team_manifest.json');metadata=scheduler.run_metadata
    if metadata['code_sha']!=plan['approved_code_sha']: raise ValueError('unapproved code revision')
    for section in ('execution','judge','runtime'):
        allowed={'maintainer_format_retries_per_request'} if section=='execution' else set()
        if {k:v for k,v in old[section].items() if k not in allowed}!={k:v for k,v in metadata[section].items() if k not in allowed}:
            raise ValueError('learning recovery changed configuration: '+section)
    import hashlib
    if old['episode_sha256']!=hashlib.sha256(canonical_json(episode).encode()).hexdigest():
        raise ValueError('episode changed')
    rows=read(root/'all_rows.json');original_tasks=[r for r in rows if r.get('pos') is not None]
    histories={a:read_jsonl(root/'workers'/a/'evolution_events.jsonl') for a in 'ABC'}
    all_events=read_jsonl(root/'team_events.jsonl')
    completed_agents={e['agent_id'] for e in all_events if e['event_type']=='NATIVE_MAINTENANCE' and e.get('block_id')==block_id}
    pending={};failed={};retained={}
    for a,events in histories.items():
        bad=[e for e in events if not e['success']]
        if len(bad)>1 or (bad and (bad[0]!=events[-1] or bad[0]['task']['pos']<first)):
            raise ValueError('ambiguous failed learning history')
        failed[a]=bad[0] if bad else None
        retained[a]=events[:-1] if bad else events
        done={e['task']['pos'] for e in retained[a] if e['task']['pos']>=first}
        pending[a]=[p for p in range(first,last+1) if p not in done]
        if a in completed_agents and pending[a]: raise ValueError('completed block has missing ingests')
        if bad:
            archive=root/'recovery_archive'/f'{a}-failed-ingest.json'
            atomic_json(archive,bad[0])
            path=root/'workers'/a/'evolution_events.jsonl'
            path.write_text(''.join(canonical_json(e)+'\n' for e in retained[a]),encoding='utf8')
    audit={'source_run':plan['source_run'],'pending_learning':pending,'preserved_completed_agents':sorted(completed_agents),
           'successful_task_replay':False,'successful_ingest_replay':False,'failed_ingest_rollback':[],
           'import_metadata_from_native_disk':[]}
    workers=scheduler.pool.setup_workers()
    try:
        from autoskill.models import Skill,SkillExample,SkillStatus
        known_plan=read(root/'block_recovery.json') if (root/'block_recovery.json').exists() else None
        before_learning={}
        for a,worker in workers.items():
            method=worker.method;exchange=method._exchange
            current=exchange.snapshot_store()
            if failed[a]:
                desired=copy.deepcopy(failed[a]['before'])
                audit['failed_ingest_rollback'].append({'agent':a,'pos':failed[a]['task']['pos'],'event_id':failed[a]['event_id']})
            elif any(e['task']['pos']>=first for e in retained[a]):
                desired=copy.deepcopy(retained[a][-1]['after'])
            else:
                desired=copy.deepcopy(current)
                known=known_plan['stores'][a]['skills'] if known_plan else retained[a][-1]['after']
                for local,bundle in known.items():
                    if current[local]['skill_files']!=bundle['skill_files']: raise ValueError('unlearned Store bytes changed')
                    desired[local]=copy.deepcopy(bundle)
                for local in set(current)-set(known):
                    if current[local]['origin']!='imported': raise ValueError('unobserved native skill mutation')
                    audit['import_metadata_from_native_disk'].append({'agent':a,'local_id':local})
            if set(current)!=set(desired): raise ValueError('rollback would require deleting a native skill')
            for local,bundle in desired.items():
                meta=bundle['native_metadata']
                method.sdk.store.upsert(Skill(**{**meta,'status':SkillStatus(meta['status']),
                    'examples':[SkillExample(**e) for e in meta['examples']]}),raw=meta)
            exchange.origins={k:{'origin':b['origin'],'imported_from_global_id':b.get('imported_from_global_id'),
                                  'content_hash':b['content_hash']} for k,b in desired.items()}
            for record in exchange.imports.values():
                b=desired[record['local_id']];record['current_content_hash']=b['content_hash']
                record['receiver_locally_updated']=b['skill_files']!=record['imported_files']
            exchange.save()
            if canonical_json(exchange.snapshot_store())!=canonical_json(desired): raise ValueError('native Store restoration mismatch')
            # Restore native merge preference from the last successful upsert in this block.
            for event in retained[a]:
                for local in event['after']:
                    method._prov.setdefault(local,(event['task']['kind'],event['task']['round']))
                if event['task']['pos']>=first and event.get('returned_skills'):
                    method.sdk.maintainer._record_last_upserted_skill_id(user_id=method._UID,skill_id=event['returned_skills'][-1]['id'])
            last_event=retained[a][-1]
            method._last_kind,method._last_round=last_event['task']['kind'],last_event['task']['round']
            block_events=[e for e in histories[a] if e['task']['pos']>=first]
            before_learning[a]=block_events[0]['before'] if block_events else desired
        # Recover pending trace inputs without executing or scoring a task again.
        pending_plan={'pending_learning':{},'file_hashes':plan['fork_hashes']}
        for pos in range(first,last+1):
            evidence={}
            for a in 'ABC':
                if pos not in pending[a]: continue
                previous=next((e for e in histories[a] if e['task']['pos']==pos),None)
                if previous:
                    bundles=previous['retrieved']
                else:
                    refs={}
                    for event in read_jsonl(root/'workers'/a/'task_events.jsonl'):
                        if event.get('pos')==pos and event['event']=='task_retrieval':
                            for ref in event['retrieved']: refs[(ref['local_skill_id'],ref['content_hash'])]=ref
                    bundles=[]
                    for (local,digest),ref in refs.items():
                        b=before_learning[a][local]
                        if b['content_hash']!=digest: raise ValueError('retrieved Skill version cannot be reconstructed')
                        bundles.append(b)
                evidence[a]={'retrieved':bundles}
            pending_plan['pending_learning'][str(pos)]=evidence
        traces={a:[] for a in 'ABC'}
        tasks={t['pos']:t for t in episode['task_sequence']}
        for pos in range(first,last+1):
            row=next(r for r in rows if r.get('pos')==pos)
            for a,trace in restore_pending(root,row,tasks[pos],workers,pending_plan).items(): traces[a].append(trace)
        log=TeamEventLogger(root/'team_events.jsonl',old['episode_id'],old['communication_protocol'])
        router=scheduler.router_type(log);registry=GlobalSkillRegistry(root/'registry')
        transfers=SkillTransferService(registry,workers,router)
        executor=SimpleNamespace(router=router,transfers=transfers)
        manifest={**old,**metadata,'recovery_history':old.get('recovery_history',[])+[
            {'source_run':plan['source_run'],'prior_code_sha':old['code_sha'],'new_code_sha':metadata['code_sha'],
             'details':'learning_recovery_audit.json','after_recorded_pos':last}]}
        atomic_json(root/'team_manifest.json',manifest)
        atomic_json(root/'learning_recovery_audit.json',audit)
        atomic_json(root/'progress.json',{'phase':'native_evolution','block_id':block_id,'last_task_id':tasks[last]['instance_id']})
        scheduler.configure_workers(workers)
        scored={r['global_skill_id'] for r in rows if r.get('kind')=='artifact'}
        for a in 'ABC':
            worker=workers[a]
            if traces[a]:
                worker.method.evolve_batch(traces[a],[],kind=tasks[first]['kind'],round_idx=tasks[first]['round'])
                scheduler.ensure_healthy()
            events=[e for e in read_jsonl(worker.method._exchange.evolution_path) if e['task']['pos']>=first]
            if [e['task']['pos'] for e in events]!=list(range(first,last+1)) or any(not e['success'] for e in events):
                raise ValueError('learning block remains incomplete')
            if a not in completed_agents:
                log.record('NATIVE_MAINTENANCE',tasks[last]['instance_id'],agent_id=a,block_id=block_id,
                           ingest_event_ids=[e['event_id'] for e in events])
                artifacts=registry.register_evolution_events(a,events,block_id=block_id)
            else:
                gids={r['global_skill_id'] for r in read_jsonl(registry.journal) if r['owner_agent']==a and r['block_id']==block_id}
                artifacts=[registry.get_skill(gid) for gid in gids]
            for artifact in artifacts:
                worker.remember_local_version(artifact.local_skill_key,artifact.global_skill_id)
                if a not in completed_agents:
                    log.record('SKILL_UPDATED' if artifact.origin=='locally_updated' else 'SKILL_CREATED',
                               tasks[last]['instance_id'],agent_id=a,block_id=block_id,**transfers.identity(artifact))
                if artifact.global_skill_id not in scored:
                    rows.append(scheduler._artifact_row(executor,episode,artifact,tasks[first]['kind']))
                    scored.add(artifact.global_skill_id)
                    scheduler.ensure_healthy()
            atomic_json(root/'all_rows.json',rows)
        if [r for r in rows if r.get('pos') is not None]!=original_tasks: raise ValueError('task rows changed')
        ProvenanceTracker(root,registry).sync()
        if scheduler.before_checkpoint: scheduler.before_checkpoint()
        log.record('LEARNING_RECOVERED',None,last_completed_pos=last,next_pos=last+1)
        save(root,last,scheduler._snapshots(workers))
        return rows
    finally:
        scheduler.pool.teardown()
