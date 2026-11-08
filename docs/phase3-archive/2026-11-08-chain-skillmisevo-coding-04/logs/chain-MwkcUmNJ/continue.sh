#!/bin/bash
set -euo pipefail
umask 077
d=$1
first="phase3-$(basename "$d")"
export PYTHONPATH=/root/misevolve-phase3-chain/src
p=/root/misevolve-phase3/.venv/bin/python
while :; do
    status=$(cat "$d/stage_status")
    case "$status" in
        paused)
            # The first process has exited before this marker is set.
            break
            ;;
        *failed*)
            printf 'Stopped without resume: %s\n' "$status"
            exit 1
            ;;
    esac
    if ! tmux has-session -t "=$first" 2>/dev/null; then
        printf 'First segment disappeared before a verified pause. No resume launched.\n'
        exit 1
    fi
    sleep 3
done
"$p" "$d/verify.py" "$d" pause
bash "$d/launch.sh" "$d" resume >"$d/resume.log" 2>&1
"$p" "$d/verify.py" "$d" final >"$d/acceptance.log"
