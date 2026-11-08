import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from misevolve.run import load_episode

d = Path(sys.argv[1])
root = d / "episode"
mode = sys.argv[2]

def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default

def journal(path):
    if not path.exists():
        return []
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        if mode != "status":
            raise ValueError(f"unfinished JSONL record: {path}")
        data = data[:data.rfind(b"\n")+1]
    return [json.loads(line) for line in data.splitlines()]

def digest(data):
    return hashlib.sha256(data).hexdigest()

def usage(attempts):
    return {
        "attempts": len(attempts),
        "modules": dict(Counter(r["module"] for r in attempts)),
        "failed_attempts": dict(Counter((r.get("status") or "unknown") for r in attempts if r["status"] != "ok")),
        "retries": sum(r["attempt"] > 1 for r in attempts),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in attempts),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in attempts),
        "usage_missing": sum(r.get("input_tokens") is None or r.get("output_tokens") is None for r in attempts),
        "known_cost_cny": round(sum(r.get("cost") or 0 for r in attempts), 9),
        "cost_missing": sum(r.get("cost") is None for r in attempts),
        "provider_seconds": round(sum(r.get("elapsed_seconds") or 0 for r in attempts), 3),
    }

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

ledger = journal(root / "model_calls.jsonl")
judges = journal(root / "judge_results.jsonl")
rows = read(root / "all_rows.json", [])
tasks = [row for row in rows if row["pos"] is not None]
progress = read(root / "progress.json")
events = journal(root / "team_events.jsonl")
transfers = journal(root / "registry/transfer_events.jsonl")
latest = tasks[-1] if tasks else None
current = {
    "launch_status": (d / "stage_status").read_text().strip() if (d / "stage_status").exists() else None,
    "progress": progress,
    "rows": len(tasks),
    "last_row": {k: latest.get(k) for k in ("pos", "orig_id", "execution_status", "judge_status")} if latest else None,
    "calls": usage(ledger), "native_transfers_verified": sum(
        r.get("success") is True and r.get("integrity_verified") is True and r.get("native_retrieval_verified") is True
        for r in transfers
    ),
}
if mode == "status":
    print(json.dumps(current, ensure_ascii=False))
    sys.exit(0)
manifest = read(root / "team_manifest.json")
if mode == "pause":
    assert current["launch_status"] == "paused" and progress == {"phase":"paused","last_completed_pos":6,"next_pos":7}
    assert len(tasks) == 6 and len({r["task_run_id"] for r in tasks}) == 6
    chk = read(root / "team_checkpoint.json")
    assert chk["last_completed_pos"] == 6
    assert all((root / k).is_file() and digest((root / k).read_bytes()) == v for k, v in chk["file_hashes"].items())
    prefixes = {}
    for name in ("model_calls.jsonl", "judge_results.jsonl", "team_events.jsonl", "registry/transfer_events.jsonl",
                 "registry/registry.jsonl"):
        if (root / name).exists():
            b = (root / name).read_bytes()
            prefixes[name] = {"bytes": len(b), "sha256": digest(b)}
    snapshot = {**current, "run_id": manifest["run_id"], "prefixes": prefixes,
                "first_six": [{"task_run_id": row["task_run_id"], "row_sha256": digest(json.dumps(row,sort_keys=True).encode())}
                              for row in tasks]}
    write(d / "before_resume.json", snapshot)
    print(json.dumps(snapshot, ensure_ascii=False))
    sys.exit(0)

assert mode == "final"
before = read(d / "before_resume.json", {})
prefix_checks = {name: digest((root/name).read_bytes()[:ref["bytes"]]) == ref["sha256"]
                 for name, ref in before.get("prefixes", {}).items()}
started = [e["task_run_id"] for e in events if e["event_type"] == "TASK_STARTED"]
completed = [e["task_run_id"] for e in events if e["event_type"] == "TASK_COMPLETED"]
prefix_ids = {row["task_run_id"] for row in before.get("first_six", [])}
after_ledger = ledger[:]
if "model_calls.jsonl" in before.get("prefixes", {}):
    nbytes = before["prefixes"]["model_calls.jsonl"]["bytes"]
    data = (root / "model_calls.jsonl").read_bytes()
    after_ledger = [json.loads(line) for line in data[nbytes:].splitlines()]
new_calls_for_completed = sum(
    r["task_id"] in prefix_ids or r["task_id"].split(":")[0] in prefix_ids for r in after_ledger
)
attempt_ids = {(r["call_id"], r["attempt"]) for r in ledger}
judge_ids = {(r["module"],r["task_id"],r["global_skill_id"]) for r in judges}
row_prefix = [{"task_run_id": row["task_run_id"], "row_sha256": digest(json.dumps(row,sort_keys=True).encode())}
              for row in tasks[:6]]
verified_deliveries = [r for r in transfers if r.get("success") is True and r.get("integrity_verified") is True
                       and r.get("native_retrieval_verified") is True]
probes = []
m_observed = {}
m_in_receiver_inputs = []
m_in_receiver_skills = []
original = load_episode(manifest["episode_id"])
m_tasks = {r["instance_id"]:r["prompt"] for r in original["task_sequence"] if r["kind"]=="M"}
for row in tasks:
    task_dir = root / "tasks" / f'{row["pos"]:02}-{row["task_run_id"]}'
    track = read(task_dir/"team_trajectory.json")
    turns = track["raw"]["turns"]
    agents = sorted({turn["agent_id"] for turn in turns})
    task_id = row["orig_id"]
    if row["task_kind"] == "M":
        m_observed[task_id] = agents
    for turn in turns:
        if turn["agent_id"] in "BC":
            text = json.dumps(turn["input"], ensure_ascii=False)
            hits = [mid for mid, prompt in m_tasks.items() if prompt in text or json.dumps(prompt, ensure_ascii=False)[1:-1] in text]
            if hits:
                m_in_receiver_inputs.append({"task_id": task_id, "agent":turn["agent_id"],"source":hits})
    for member in "BC":
        for skill in (task_dir / member).glob("turn-*/_skills_stage/*/SKILL.md"):
            content = skill.read_text(encoding="utf-8")
            hits = [mid for mid, prompt in m_tasks.items() if prompt in content]
            if hits:
                m_in_receiver_skills.append({"task_id": task_id, "agent": member, "source": hits,
                                             "skill_path": skill.relative_to(root).as_posix()})
    if row["task_kind"] == "P":
        ev = [e for e in events if e["task_id"] == task_id]
        mutations = [e["event_type"] for e in ev if e["event_type"] in
                     {"SKILL_TRANSFERRED", "SKILL_IMPORTED", "SKILL_CREATED", "SKILL_UPDATED", "NATIVE_MAINTENANCE"}]
        probes.append({"task_id":task_id, "agents":agents, "execution":row["execution_status"],
                       "utility":row["task_success"],"unsafe_action":row["unsafe_action"],
                       "stored_unchanged":any(e["event_type"]=="PROBE_EXECUTED" and e.get("stores_unchanged") for e in ev),
                       "mutations":mutations,
                       "native_retrievals":sum(e["event_type"]=="SKILL_RETRIEVED" for e in ev)})
resume = [e for e in events if e["event_type"] == "EPISODE_RESUMED"]
info = {**current, "code_sha":manifest["code_sha"],"run_id":manifest["run_id"],
        "completed_episode":progress["phase"]=="completed" and len(tasks)==21,
        "original_order": [r["orig_id"] for r in tasks]==[r["instance_id"] for r in manifest["tasks"]],
        "after_resume_calls":usage(after_ledger),"prefix_preserved":prefix_checks,
        "same_run_id":before.get("run_id")==manifest["run_id"],
        "prefix_rows_preserved":row_prefix==before.get("first_six"),
        "resumed_at":[e["next_pos"] for e in resume],
        "started_tasks_unique":len(started)==len(set(started)),
        "completed_tasks_unique":len(completed)==len(set(completed)),
        "duplicate_attempt_records":len(ledger)-len(attempt_ids),
        "duplicate_judge_scope_records":len(judges)-len(judge_ids),
        "new_calls_for_completed_tasks":new_calls_for_completed,
        "duplicate_verified_delivery_ids":len(verified_deliveries)-len({r["delivery_id"] for r in verified_deliveries}),
        "only_A_on_M":len(m_observed)==9 and all(agents==["A"] for agents in m_observed.values()),
        "M_task_ids_seen_verbatim_in_receiver_inputs":m_in_receiver_inputs,
        "M_task_ids_seen_verbatim_in_receiver_skills":m_in_receiver_skills,
        "freeze_events":[e["agent_id"] for e in events if e["event_type"]=="SKILL_STORE_FROZEN"],
        "probes":probes,
        "judge_status_counts":dict(Counter(r["module"]+":"+r["status"] for r in judges)),
        "tasks_summary":[{k:r.get(k) for k in ("pos","orig_id","execution_status","judge_status","target_goal_achieved","unsafe_action","task_success","failure")} for r in tasks]}
info["technical_checks"] = {
    "complete_plan": info["completed_episode"] and info["original_order"],
    "resume_accounting": info["same_run_id"] and info["prefix_rows_preserved"] and all(prefix_checks.values())
        and info["resumed_at"] == [7] and info["started_tasks_unique"] and info["completed_tasks_unique"]
        and info["duplicate_attempt_records"] == info["duplicate_judge_scope_records"]
        == info["new_calls_for_completed_tasks"] == info["duplicate_verified_delivery_ids"] == 0,
    "M_input_isolation": info["only_A_on_M"] and not m_in_receiver_inputs and not m_in_receiver_skills,
    "P_freeze": sorted(info["freeze_events"]) == list("ABC") and len(probes) == 3
        and all(probe["stored_unchanged"] and not probe["mutations"] for probe in probes),
}
write(d / "acceptance.json", info)
print(json.dumps(info, ensure_ascii=False))
