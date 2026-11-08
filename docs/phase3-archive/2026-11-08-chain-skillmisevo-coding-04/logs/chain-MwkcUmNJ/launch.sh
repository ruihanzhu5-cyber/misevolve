#!/bin/bash
set -euo pipefail
umask 077
work=$1
mode=$2
cd /root/misevolve-phase3-chain
export PYTHONPATH="$PWD/src"
p=/root/misevolve-phase3/.venv/bin/python
record_status() {
    printf '%s\n' "$1" >"$work/.stage_status.tmp"
    mv -fT -- "$work/.stage_status.tmp" "$work/stage_status"
}
trap 'status=$?; if ((status)); then record_status "$mode failed exit=$status"; fi' EXIT
args=(--episode skillmisevo-coding-04 --protocol chain --replicate-id "$(basename "$work")" --out "$work/episode" --verify-runtime)
if [ "$mode" = first ]; then
    record_status offline_checks
    /root/misevolve-phase3/.venv/bin/ruff check .
    "$p" -m compileall -q src tests
    git diff --check
    unshare --net bash -ec 'ip link set dev lo up; unset MINIMAX_API_KEY DEEPSEEK_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY; "$1" -m pytest -q' bash "$p"
    args+=(--stop-after-pos 6)
else
    test "$mode" = resume
    test "$(cat "$work/stage_status")" = paused
    args+=(--resume)
fi
record_status "$mode running"
set -a
. /root/.config/misevolve/stage7.env
set +a
export PYTHONUNBUFFERED=1
"$p" -m misevolve.team.cli run "${args[@]}"
phase=$("$p" -c 'import json,sys;print(json.load(open(sys.argv[1]))["phase"])' "$work/episode/progress.json")
if [ "$mode" = first ]; then test "$phase" = paused; else test "$phase" = completed; fi
record_status "$phase"
if [ "$phase" = completed ]; then
    "$p" -m misevolve.team.cli summarize "$work/episode" --out "$work/report"
fi
