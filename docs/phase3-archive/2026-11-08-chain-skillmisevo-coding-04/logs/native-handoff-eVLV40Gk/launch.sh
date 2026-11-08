#!/bin/bash
set -euo pipefail
umask 077
cd /root/misevolve-phase3-verify
unset DEEPSEEK_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY
export MINIMAX_API_KEY=synthetic-no-model-access
export PYTHONPATH=/root/misevolve-phase3-verify/src
export MISEVOLVE_AUTOSKILL_DIR=/root/misevolve-phase3/external/methods/AutoSkill
p=/root/misevolve-phase3/.venv/bin/python
unshare --net "$p" -u "$1/probe.py" "$1" store
"$p" -u "$1/probe.py" "$1" binary
printf '%s\n' completed >"$1/status"
