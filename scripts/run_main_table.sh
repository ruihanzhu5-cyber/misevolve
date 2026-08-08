#!/usr/bin/env bash

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }
[ -f configs/run_env.sh ] && { set -a; . configs/run_env.sh; set +a; }

eval "$(python3 - <<'PY'
import yaml
g = yaml.safe_load(open('configs/grid.yaml'))
r = g['runner']
conds = [f"{m}|{h}" for m in g['methods'] for h in g['harnesses']]
conds += [f"{c['method']}|{c['harness']}" for c in g.get('extra_conditions', [])]
print(f"GRID_CONDS='{' '.join(conds)}'")
print(f"GRID_EPISODES_JSON='{g['episodes']}'")
print(f"GRID_CONC='{r['concurrency']}'; GRID_TIMEOUT='{r['cell_timeout']}'")
print(f"GRID_PASSES='{r['passes']}'; GRID_OUT='{r['out_root']}'")
PY
)"

BACKBONE="${BACKBONE:-}"
OUT="${OUT_ROOT:-${OUT:-$GRID_OUT}}"
CONC="${CONCURRENCY:-${CONC:-$GRID_CONC}}"
GRID_TIMEOUT="${CELL_TIMEOUT:-$GRID_TIMEOUT}"
MAX_TASKS="${MAX_TASKS:-}"
GOVERNANCE="${GOVERNANCE:-}"
GOVERNANCE_CONFIG="${GOVERNANCE_CONFIG:-}"
SCHEDULE="${SCHEDULE:-}"

if [ -n "${METHODS:-}" ] || [ -n "${HARNESSES:-}" ]; then
  filtered=""
  for c in $GRID_CONDS; do
    m="${c%%|*}"; h="${c##*|}"
    [ -n "${METHODS:-}" ]   && ! grep -qw -- "$m" <<<"$METHODS"   && continue
    [ -n "${HARNESSES:-}" ] && ! grep -qw -- "$h" <<<"$HARNESSES" && continue
    filtered="$filtered $c"
  done
  GRID_CONDS="$filtered"
fi

EPS=$(python3 -c "import json;print(' '.join(e['episode_id'] for e in json.load(open('$GRID_EPISODES_JSON'))['episodes']))")

run_one() {
  IFS='|' read -r ep method hz <<<"$1"
  local out="$OUT/${ep}__${method}__${hz}"
  [ -f "$out/all_rows.json" ] && return 0
  mkdir -p "$out"
  local extra=""
  local py="python3"
  [ -x ".venvs/$method/bin/python" ] && py=".venvs/$method/bin/python"
  [ -n "$MAX_TASKS" ] && extra="--max-tasks $MAX_TASKS"
  [ -n "$BACKBONE" ] && extra="$extra --backbone $BACKBONE"
  [ -n "$GOVERNANCE" ] && extra="$extra --governance $GOVERNANCE"
  [ -n "$GOVERNANCE_CONFIG" ] && extra="$extra --governance-config $GOVERNANCE_CONFIG"
  [ -n "$SCHEDULE" ] && extra="$extra --schedule $SCHEDULE"

  if command -v timeout >/dev/null 2>&1; then
    timeout "$GRID_TIMEOUT" "$py" -m misevolve.run \
      --episode "$ep" --method "$method" --harness "$hz" --out "$out" \
      --episodes-json "$GRID_EPISODES_JSON" $extra >>"$out/cell.log" 2>&1
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$GRID_TIMEOUT" "$py" -m misevolve.run \
      --episode "$ep" --method "$method" --harness "$hz" --out "$out" \
      --episodes-json "$GRID_EPISODES_JSON" $extra >>"$out/cell.log" 2>&1
  else
    "$py" -m misevolve.run \
      --episode "$ep" --method "$method" --harness "$hz" --out "$out" \
      --episodes-json "$GRID_EPISODES_JSON" $extra >>"$out/cell.log" 2>&1
  fi
  echo "done $ep|$method|$hz rc=$?"
}
export -f run_one
export OUT GRID_TIMEOUT GRID_EPISODES_JSON MAX_TASKS BACKBONE
export GOVERNANCE GOVERNANCE_CONFIG SCHEDULE

jobs_left() {
  for c in $GRID_CONDS; do
    m="${c%%|*}"; h="${c##*|}"
    for ep in $EPS; do
      [ -f "$OUT/${ep}__${m}__${h}/all_rows.json" ] && continue
      echo "${ep}|${m}|${h}"
    done
  done
}

total=$(( $(wc -w <<<"$GRID_CONDS") * $(wc -w <<<"$EPS") ))
echo "[main-table $(date +%H:%M:%S)] backbone=${BACKBONE:-<default>} conditions=$(wc -w <<<"$GRID_CONDS") episodes=$(wc -w <<<"$EPS") total=$total conc=$CONC"
for pass in $(seq 1 "$GRID_PASSES"); do
  left=$(jobs_left | grep -c . || true)
  [ "$left" -eq 0 ] && { echo "[main-table] all cells produced all_rows.json"; break; }
  echo "[main-table pass$pass] pending $left/$total"
  jobs_left | xargs -P "$CONC" -I JOB bash -c 'run_one "$@"' _ JOB
done
echo "[main-table $(date +%H:%M:%S)] DONE $(ls -L "$OUT"/*/all_rows.json 2>/dev/null | wc -l | tr -d ' ')/$total"
