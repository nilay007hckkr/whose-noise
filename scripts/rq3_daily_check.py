"""RQ3 daily operational checks for given task ids: counts, termination reasons, merged turns,
first-turn stops. Deliberately prints no reward/success or premature-stop numbers
(PREREGISTRATION section 14: outcomes are analysed at the end).

Usage: uv run python scripts/rq3_daily_check.py TASK_ID [TASK_ID ...]  (from the whose-noise root)
Checked on 2026-11-07: tasks 5 9 reproduce the day-1 numbers in docs/RESULTS_LOG.md.
"""

import sys
from collections import Counter
from pathlib import Path

from whose_noise.fidelity import simulation_fidelity
from whose_noise.tau2_results import load_runs, read_results

ROOT = Path.cwd().parent  # run from whose-noise; the tau2 checkouts are its siblings
RUNS = ("gptoss120b_test-nonl_t3_s300_ce7e638", "qwen3.8-27b_test-nonl_t3_s300_cd30908")

tasks = set(sys.argv[1:])
assert tasks, "give task ids"
for version in ("base", "fix"):
    for run in RUNS:
        sims_dir = ROOT / f"tau2-bench-v2-{version}" / "data" / "simulations"
        path = sims_dir / f"rq3{version}_qwen4b_{run}" / "results.json"
        load_runs(path)  # raises on duplicate (task, trial) or mixed seeds
        data = read_results(path)
        sims = [s for s in data["simulations"] if str(s["task_id"]) in tasks]
        ends = Counter(s.get("termination_reason") for s in sims)
        fid = [simulation_fidelity(s, data["info"]) for s in sims]
        merged = sum(f["merged_turns"] for f in fid)
        first_stop = sum(f["first_turn_stop"] for f in fid)
        commit = data["info"].get("git_commit", "")[:7]
        print(
            f"{version:4} {run.split('_')[0]:12} sims {len(sims):2} | ends {dict(ends)} | "
            f"merged_turns {merged}/{len(sims)} | first_turn_stop {first_stop}/{len(sims)} | "
            f"commit {commit} | total in file {len(data['simulations'])}"
        )
        print("     cells:", sorted((str(s["task_id"]), s["trial"]) for s in sims))
