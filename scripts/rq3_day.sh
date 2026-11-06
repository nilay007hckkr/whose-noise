#!/usr/bin/env bash
# One RQ3 day (PREREGISTRATION section 14): the next task chunk in both versions with both
# simulators, then the operational checks. Started hourly by the systemd user timer rq3-day.timer
# (docs/SETUP_LINUX.md). It does nothing until 25 h after the last attempt began (Groq's daily
# quota is a rolling 24 h window and one day uses most of gpt-oss-120b's), or once all 29 tasks
# are complete. The next chunk comes from the results files, so a failed day is simply retried.
# RQ3_DRY=1: no gate, no stamp, no agent server; run_grid --dry-run (a test of this script).
set -uo pipefail
R=/home/nj/NILAY/Reaserch
WN=$R/whose-noise
STATE=$R/rq3_state # outside the repo: lock, attempt stamp, one log per day
DRY=${RQ3_DRY:-}
mkdir -p "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0
stamp=$STATE/last_attempt # mtime = start of the last attempt
if [[ -z $DRY && -e $stamp ]] && (($(date +%s) - $(stat -c %Y "$stamp") < 25 * 3600)); then
  exit 0
fi

complete() { # task ids with 3 valid simulations in each of the four RQ3 runs
  "$WN/.venv/bin/python" -I - <<'EOF'
import json
from collections import Counter
from pathlib import Path

root = Path("/home/nj/NILAY/Reaserch")
runs = ("gptoss120b_test-nonl_t3_s300_ce7e638", "qwen3.8-27b_test-nonl_t3_s300_cd30908")
done = []
for v in ("base", "fix"):
    for run in runs:
        path = root / f"tau2-bench-v2-{v}/data/simulations/rq3{v}_qwen4b_{run}/results.json"
        sims = json.loads(path.read_text(encoding="utf-8"))["simulations"]
        valid = Counter(
            str(s["task_id"]) for s in sims if s.get("termination_reason") != "infrastructure_error"
        )
        done.append({t for t, n in valid.items() if n >= 3})
print(" ".join(sorted(set.intersection(*done), key=int)))
EOF
}

before=$(complete) || exit 1
n=$(wc -w <<<"$before")
((n >= 29)) && exit 0
k=$((n / 2 + 1))
log=$STATE/day_$(date +%F)_chunk$k${DRY:+_dry}.log
exec >>"$log" 2>&1
echo "== $(date '+%F %T %Z') chunk $k/15; complete before: $before"

dry_flag=()
if [[ -n $DRY ]]; then
  dry_flag=(--dry-run)
else
  touch "$stamp"
  if curl -sf http://127.0.0.1:8080/health >/dev/null; then
    echo "== port 8080 already serves something; refusing to use a server this script did not start"
    exit 1
  fi
  cd "$R" || exit 1
  ./tools/llama.cpp-b11552/llama-b11552/llama-server -m models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
    --jinja -ngl all -c 16384 --load-mode none --cache-ram 512 --port 8080 --alias qwen3-4b \
    >>llama_server.log 2>&1 &
  server=$!
  trap 'kill $server 2>/dev/null; wait $server 2>/dev/null' EXIT
  curl -sf --retry 120 --retry-connrefused --retry-all-errors --retry-delay 1 \
    http://127.0.0.1:8080/health >/dev/null || { echo "== agent server did not start"; exit 1; }
fi

order=(base fix) # section 14: base first on odd k, fix first on even k
((k % 2 == 0)) && order=(fix base)
cd "$WN" || exit 1
for v in "${order[@]}"; do
  uv run python scripts/run_grid.py --config "configs/rq3_pr702_$v.json" \
    --tau2-dir "../tau2-bench-v2-$v" --task-chunk "$k/15" "${dry_flag[@]}"
  echo "== $v exit $?"
done

after=$(complete)
new=$(comm -13 <(tr ' ' '\n' <<<"$before" | sort) <(tr ' ' '\n' <<<"$after" | sort) | xargs)
echo "== newly complete tasks: ${new:-none}"
# shellcheck disable=SC2086  # one argument per task id
[[ -n $new ]] && uv run python scripts/rq3_daily_check.py $new
echo "== end $(date '+%F %T %Z')"
