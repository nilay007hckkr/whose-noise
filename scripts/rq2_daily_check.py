"""RQ2 daily operational checks for given base task ids, per arm: simulations, termination
reasons, merged turns, first-turn stops and, for the Hindi arms, script leakage. Prints no
outcomes (PREREGISTRATION section 10: the H2 contrasts wait until all three arms are complete).

Usage: uv run python scripts/rq2_daily_check.py [--experiment NAME] TASK_ID [TASK_ID ...]
(from the whose-noise root). The arms' config hashes are those of the pilots, so
`--experiment rq2pilot2 53 56` and `--experiment rq2pilot3 53 55 56 61 64 65` re-check the pilots
(checked on 2026-11-07 against docs/RESULTS_LOG.md, 2026-10-29).
"""

import math
import sys
from collections import Counter
from pathlib import Path

from whose_noise.fidelity import simulation_fidelity
from whose_noise.tau2_results import load_runs, read_results

SIMS_DIR = Path.cwd().parent / "tau2-bench-multilingual" / "data" / "simulations"
ARMS = {"en-plain": "c1a8dad", "hi-romanized": "c9f6a09", "hi-code-mixed": "c0da38e"}

args = sys.argv[1:]
experiment = "rq2a"
if args[:1] == ["--experiment"]:
    experiment, args = args[1], args[2:]
tasks = set(args)
assert tasks, "give base task ids (without _hi)"
for arm, cfg_hash in ARMS.items():
    found = sorted(SIMS_DIR.glob(f"{experiment}_qwen4b_gptoss120b_{arm}_*_s300_{cfg_hash}"))
    if not found:
        print(f"{arm:13} no results")
        continue
    path = found[0] / "results.json"
    load_runs(path)  # raises on duplicate (task, trial) or mixed seeds
    data = read_results(path)
    sims = [s for s in data["simulations"] if str(s["task_id"]).removesuffix("_hi") in tasks]
    fid = [simulation_fidelity(s, data["info"]) for s in sims]
    ends = Counter(s.get("termination_reason") for s in sims)
    merged = sum(f["merged_turns"] for f in fid)
    first_stop = sum(f["first_turn_stop"] for f in fid)
    leaks = [x for f in fid if (x := f["script_leak_share"]) is not None and not math.isnan(x)]
    leak = " ".join(f"{x:.2f}" for x in leaks) or "-"
    print(
        f"{arm:13} sims {len(sims):2} | ends {dict(ends)} | merged_turns {merged}/{len(sims)} | "
        f"first_turn_stop {first_stop}/{len(sims)} | script leak share {leak} | "
        f"total in file {len(data['simulations'])}"
    )
    print("     cells:", sorted((str(s["task_id"]), s["trial"]) for s in sims))
