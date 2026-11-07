#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
    echo "usage: bash scripts/run_stage72.sh PYTHON PREFLIGHT_REPORT CREDENTIALS TARIFF_BAND" >&2
    exit 2
fi
python_bin=$1
proof=$2
credentials=$3
band=$4

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
umask 077
run_root="$PWD/results/stage72"
mkdir -p -- "$run_root"
exec 9>"$run_root/.launch.lock"
if ! flock -n 9; then
    echo "A Stage 7.2 pilot is already running." >&2
    exit 1
fi

run_dir=$(mktemp -d -- "$run_root/stage72-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
link="$run_root/.latest.${run_dir##*/}"
ln -s -- "$run_dir" "$link"
mv -Tf -- "$link" "$run_root/latest"
printf 'Stage 7.2 output directory %s\n' "$run_dir"

set +e
PYTHONPATH="$PWD/src" "$python_bin" -m misevolve.stage7_pilot run \
    --output "$run_dir" --preflight-report "$proof" --credentials "$credentials" \
    --deepseek-band "$band" 2>&1 | tee "$run_dir/run.log"
code=${PIPESTATUS[0]}
set -e
printf '%s\n' "$code" > "$run_dir/.run.exit"
mv -T -- "$run_dir/.run.exit" "$run_dir/run.exit"
exit "$code"
