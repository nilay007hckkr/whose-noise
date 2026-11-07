"""Build the case file for RQ1 step 8 (failure analysis, PREREGISTRATION section 7.8).

One record per failed RQ1 simulation (reward < 1), in the format of the first audits in
results/audit/failed_cases_rq1_*.json (whose builder was not saved; this one reproduces them, see
--check-old). Unlike them it keeps tool results in full (they cut them at 1200 characters, which
can hide what the agent misread). contrast_passing = a passing run of the same task, from the other
simulator if one exists, else from the same simulator; lowest trial first.

    uv run python scripts/build_step8_cases.py --check-old --human-sample 20
"""

import argparse
import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIMS = ROOT.parent / "tau2-bench/data/simulations"
RUNS = {
    "gpt-oss-120b": SIMS / "rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638/results.json",
    "qwen3.8-27b": SIMS / "rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908/results.json",
}
OUT = ROOT / "results/audit/rq1_step8"
OLD = [
    ROOT / "results/audit/failed_cases_rq1_chunk1.json",
    ROOT / "results/audit/failed_cases_rq1_chunk2_3.json",
]
SAMPLE_SEED = 20261107
LABELS = ("AGENT_ERROR", "SIMULATOR_ERROR", "SHARED", "TASK_OR_GRADER_ISSUE", "UNCLEAR")


def transcript(messages, cut=None):
    """cut: shorten tool results to this many characters, as the first audits did (1200)."""
    lines = []
    for m in messages:
        tag = {"assistant": "[AGENT]", "user": "[CUSTOMER]", "tool": "[TOOL RESULT]"}[m["role"]]
        text = (m.get("content") or "").strip()
        if cut and m["role"] == "tool" and len(text) > cut:
            text = f"{text[:cut]}...[+{len(text) - cut} chars]"
        line = f"{tag} {text}"
        if m.get("tool_calls"):
            calls = [[c["name"], c["arguments"]] for c in m["tool_calls"]]
            line += f" TOOL_CALL {json.dumps(calls, ensure_ascii=False)}"
        lines.append(line)
    return "\n".join(lines)


def passed(sim):
    return (sim.get("reward_info") or {}).get("reward", 0) >= 1


def build(cut=None):
    runs = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in RUNS.items()}
    tasks = {str(t["id"]): t for r in runs.values() for t in r["tasks"]}
    sims = {n: sorted(r["simulations"], key=lambda s: int(s["trial"])) for n, r in runs.items()}
    cases = []
    for name, own in sims.items():
        other = next(n for n in sims if n != name)
        for s in own:
            if passed(s):
                continue
            tid, task = str(s["task_id"]), tasks[str(s["task_id"])]
            contrast = next(
                (
                    c
                    for n in (other, name)
                    for c in sims[n]
                    if str(c["task_id"]) == tid and passed(c)
                ),
                None,
            )
            ri = s.get("reward_info") or {}
            cases.append(
                {
                    "case_id": f"{name}__task{tid}__trial{s['trial']}",
                    "simulator": name,
                    "task_id": tid,
                    "trial": int(s["trial"]),
                    "scenario_instructions": task["user_scenario"]["instructions"],
                    "gold_actions": [
                        [a["name"], a["arguments"]] for a in task["evaluation_criteria"]["actions"]
                    ],
                    "action_checks": [
                        {
                            "name": c["action"]["name"],
                            "args": c["action"]["arguments"],
                            "matched": c["action_match"],
                        }
                        for c in ri.get("action_checks") or []
                    ],
                    "db_match": (ri.get("db_check") or {}).get("db_match"),
                    "transcript": transcript(s["messages"], cut),
                    "contrast_passing": contrast
                    and {
                        "simulator": next(n for n in sims if contrast in sims[n]),
                        "trial": int(contrast["trial"]),
                        "transcript": transcript(contrast["messages"], cut),
                    },
                }
            )
    return sorted(cases, key=lambda c: (c["simulator"], int(c["task_id"]), c["trial"]))


def check_old():
    by_id = {c["case_id"]: c for c in build(cut=1200)}
    old = [c for path in OLD for c in json.loads(path.read_text(encoding="utf-8"))]
    # an old null contrast may predate the passing run (chunk 2-3's file was written before task
    # 53's other runs existed), so only a non-null old contrast must match
    bad = [
        (o["case_id"], k)
        for o in old
        for k in o
        if by_id.get(o["case_id"], {}).get(k) != o[k]
        and not (k == "contrast_passing" and o[k] is None)
    ]
    assert not bad, f"differs from the first audits' case files: {bad[:5]}"
    print(
        f"check-old: the {len(old)} cases of the first audits are reproduced exactly (with their"
        " 1200-character cut of tool results; cases.json keeps tool results in full)"
    )


def human_sample(cases, n):
    rng = random.Random(SAMPLE_SEED)
    pick = []
    for name in RUNS:  # stratified by simulator, n/2 each
        pick += rng.sample([c for c in cases if c["simulator"] == name], n // 2)
    rng.shuffle(pick)
    blind = {c["case_id"]: f"H{i + 1:02d}" for i, c in enumerate(pick)}
    md = [
        "# RQ1 step 8: human labels (fill in human_labels.csv)",
        "",
        "Label each case with one of: " + ", ".join(f"`{x}`" for x in LABELS) + ".",
        "Rules (RESEARCH_NOTES section 7, in step 8's names):",
        "",
        "- `SIMULATOR_ERROR`: the customer broke its instructions so that success became"
        " impossible or much harder (wrong IDs or details, a request not in the scenario,"
        " `###STOP###` before the goal, information that contradicts the scenario).",
        "- `AGENT_ERROR`: the customer behaved correctly and the agent made a wrong or missing"
        " tool call, broke the policy, made an extra write, or ran out of steps.",
        "- `SHARED`: both of the above, and either one alone would have caused the failure.",
        "- `TASK_OR_GRADER_ISSUE`: the task or its gold actions are wrong or ambiguous, or a"
        " reasonable outcome was graded as a failure.",
        "- `UNCLEAR`: cannot tell from the transcript; write one sentence on why.",
        "",
        "Do not open results/audit/ (the judges' labels) before you have labelled all cases. The"
        " simulator is visible in the transcript style, so blinding is only partial.",
        "",
    ]
    for c in pick:
        md += [
            f"## {blind[c['case_id']]}",
            "",
            "**Scenario:** " + json.dumps(c["scenario_instructions"], ensure_ascii=False),
            "",
            "**Gold actions:** " + json.dumps(c["gold_actions"], ensure_ascii=False),
            "",
            "**Action checks:** "
            + ", ".join(
                f"{a['name']}={'ok' if a['matched'] else 'MISSED'}" for a in c["action_checks"]
            ),
            f"**DB match:** {c['db_match']}",
            "",
            "```text",
            c["transcript"],
            "```",
            "",
        ]
    OUT.joinpath("human_sheet.md").write_text("\n".join(md), encoding="utf-8")
    with OUT.joinpath("human_labels.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["blind_id", "label", "notes"])
        w.writerows([b, "", ""] for b in sorted(blind.values()))
    key = {b: cid for cid, b in blind.items()}
    OUT.joinpath("human_key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(f"human sample: {len(pick)} cases (seed {SAMPLE_SEED}) -> human_sheet.md + labels csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check-old", action="store_true", help="assert the first audits' cases match")
    ap.add_argument("--human-sample", type=int, metavar="N", help="also export N cases to label")
    a = ap.parse_args()
    if a.check_old:
        check_old()
    cases = build()
    OUT.mkdir(parents=True, exist_ok=True)
    OUT.joinpath("cases.json").write_text(
        json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    per_sim = {n: sum(c["simulator"] == n for c in cases) for n in RUNS}
    print(f"{len(cases)} failed simulations {per_sim} -> {OUT / 'cases.json'}")
    if a.human_sample:
        human_sample(cases, a.human_sample)


if __name__ == "__main__":
    main()
