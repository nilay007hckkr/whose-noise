#!/usr/bin/env bash
# One study day: the next RQ3 chunk (PREREGISTRATION section 14) or, once all 29 RQ3 tasks are
# complete, the next RQ2 task with all three arms (sections 6, 10, 13), then the operational
# checks. Started hourly by the systemd user timer study-day.timer (docs/SETUP_LINUX.md). It acts
# only 24 h after the last run ended (Groq's gpt-oss-120b quota is a rolling 24 h window and a day
# of either study uses most of it) and stops when both studies are complete. The next chunk comes
# from the results files, so a failed day is simply retried.
# shortcut: a task that never completes (dropped under section 8) is retried every day and blocks
# the queue; the daily write-up flags a chunk retried 3 times and the author decides.
# STUDY_DRY=1: no gate, no stamp, no agent server; run_grid --dry-run (a test of this script).
set -uo pipefail
R=/home/nj/NILAY/Reaserch
WN=$R/whose-noise
STATE=$R/rq3_state # outside the repo: lock, run stamp, one log per day
DRY=${STUDY_DRY:-}
mkdir -p "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0
stamp=$STATE/last_attempt # mtime = end of the last run (start, if it never ended)
if [[ -z $DRY && -e $stamp ]] && (($(date +%s) - $(stat -c %Y "$stamp") < 24 * 3600)); then
  exit 0
fi

complete() { # $1 = rq3 | rq2: base task ids with all planned valid simulations in every cell
  "$WN/.venv/bin/python" -I - "$1" <<'EOF'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path("/home/nj/NILAY/Reaserch")
if sys.argv[1] == "rq3":
    runs = ("gptoss120b_test-nonl_t3_s300_ce7e638", "qwen3.8-27b_test-nonl_t3_s300_cd30908")
    cells = [root / f"tau2-bench-v2-{v}/data/simulations/rq3{v}_qwen4b_{r}/results.json"
             for v in ("base", "fix") for r in runs]
    need = 3
else:
    sims_dir = root / "tau2-bench-multilingual/data/simulations"
    cells = [sims_dir / f"rq2a_qwen4b_gptoss120b_{arm}_test-nonl-first12_t2_s300_{h}/results.json"
             for arm, h in (("en-plain", "c1a8dad"), ("hi-romanized", "c9f6a09"),
                            ("hi-code-mixed", "c0da38e"))]
    need = 2
done = []
for path in cells:
    sims = json.loads(path.read_text(encoding="utf-8"))["simulations"] if path.exists() else []
    valid = Counter(str(s["task_id"]).removesuffix("_hi") for s in sims
                    if s.get("termination_reason") != "infrastructure_error")
    done.append({t for t, n in valid.items() if n >= need})
print(" ".join(sorted(set.intersection(*done), key=int)))
EOF
}

before=$(complete rq3) || exit 1
n=$(wc -w <<<"$before")
if ((n < 29)); then
  study=rq3 k=$((n / 2 + 1))
  order=(base fix) # section 14: base first on odd k, fix first on even k
  ((k % 2 == 0)) && order=(fix base)
else
  before=$(complete rq2) || exit 1
  n=$(wc -w <<<"$before")
  ((n >= 12)) && exit 0
  study=rq2 k=$((n + 1))
fi
log=$STATE/day_$(date +%F)_${study}_chunk$k${DRY:+_dry}.log
exec >>"$log" 2>&1
echo "== $(date '+%F %T %Z') $study chunk $k; complete before: ${before:-none}"

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
  trap 'kill $server 2>/dev/null; wait $server 2>/dev/null; touch "$stamp"' EXIT
  curl -sf --retry 120 --retry-connrefused --retry-all-errors --retry-delay 1 \
    http://127.0.0.1:8080/health >/dev/null || { echo "== agent server did not start"; exit 1; }
fi

cd "$WN" || exit 1
if [[ $study == rq3 ]]; then
  for v in "${order[@]}"; do
    uv run python scripts/run_grid.py --config "configs/rq3_pr702_$v.json" \
      --tau2-dir "../tau2-bench-v2-$v" --task-chunk "$k/15" "${dry_flag[@]}"
    echo "== $v exit $?"
  done
else # all three arms of the task in one grid (section 10: same day)
  uv run python scripts/run_grid.py --config configs/rq2_option_a.json --task-chunk "$k/12" \
    "${dry_flag[@]}"
  echo "== rq2 exit $?"
fi

after=$(complete "$study")
new=$(comm -13 <(tr ' ' '\n' <<<"$before" | sort) <(tr ' ' '\n' <<<"$after" | sort) | xargs)
echo "== newly complete tasks: ${new:-none}"
# shellcheck disable=SC2086  # one argument per task id
[[ -n $new ]] && uv run python "scripts/${study}_daily_check.py" $new
echo "== end $(date '+%F %T %Z')"
