"""RQ1 step 8 report (PREREGISTRATION section 7, step 8; section 12 row of 2026-11-07).

    uv run python scripts/analyze_step8.py

Input: judges_raw.json = the output file of a finished run of scripts/step8_judges.workflow.js
(same layout as results/audit/audit_rq1_*_raw.json), copied unchanged.
Until every case of human_labels.csv is labelled, report.md holds only completion, raw agreement
and Cohen's kappa (A vs B); after that, label counts, the comparison with the first audits and
judge accuracy against the human labels (Wilson 95% intervals).
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from whose_noise.analyze import wilson_interval  # noqa: E402

D = ROOT / "results/audit/rq1_step8"
OLD = [
    ROOT / "results/audit/audit_rq1_chunk1_raw.json",
    ROOT / "results/audit/audit_rq1_chunk2_3_raw.json",
]
CAUSES = ("AGENT_ERROR", "SIMULATOR_ERROR", "SHARED", "TASK_OR_GRADER_ISSUE")


def cohen_kappa(x, y):
    """Cohen's kappa for two raters' nominal labels (paired lists); nan if chance agreement is 1."""
    n = len(x)
    if not n:
        return float("nan")
    po = sum(a == b for a, b in zip(x, y, strict=True)) / n
    cx, cy = Counter(x), Counter(y)
    pe = sum(cx[k] * cy[k] for k in cx) / n**2
    return float("nan") if pe == 1 else (po - pe) / (1 - pe)


def rate(k, n):
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} = {k / n:.2f} [{lo:.2f}, {hi:.2f}]" if n else "0/0"


def table(rows, cols):
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
    return "\n".join([head] + ["| " + " | ".join(str(v) for v in r) + " |" for r in rows])


def report(judged, cases, human):
    sim = {c["case_id"]: c["simulator"] for c in cases}
    done = [j for j in judged if j.get("a") and j.get("b")]
    missing = sorted(set(sim) - {j["id"] for j in judged if j.get("final")})
    a = [j["a"]["primary_cause"] for j in done]
    b = [j["b"]["primary_cause"] for j in done]
    agree = sum(x == y for x, y in zip(a, b, strict=True))
    out = [
        "# RQ1 step 8: failure analysis",
        "",
        f"Cases: {len(sim)} failed RQ1 simulations; judged by both A and B: {len(done)}; "
        f"with a final label: {len(sim) - len(missing)}; missing: {', '.join(missing) or 'none'}.",
        "",
        f"Raw agreement A vs B (primary cause): {rate(agree, len(done))}; "
        f"Cohen's kappa {cohen_kappa(a, b):.3f}; tiebreaks: {len(done) - agree}.",
        "",
    ]
    for name in sorted(set(sim.values())):
        sub = [j for j in done if sim[j["id"]] == name]
        xa = [j["a"]["primary_cause"] for j in sub]
        xb = [j["b"]["primary_cause"] for j in sub]
        k = sum(p == q for p, q in zip(xa, xb, strict=True))
        out.append(f"- {name}: agreement {rate(k, len(sub))}, kappa {cohen_kappa(xa, xb):.3f}")
    if human is None:
        out += [
            "",
            "Label counts, the comparison with the first audits and judge accuracy are "
            "withheld until every case of human_labels.csv is labelled (section 12).",
        ]
        return "\n".join(out) + "\n"

    final = {j["id"]: j["final"] for j in judged if j.get("final")}
    out += [
        "",
        "## Final cause by simulator",
        "",
        table(
            [
                [n]
                + [sum(final.get(c) == k for c in final if sim[c] == n) for k in CAUSES]
                + [sum(sim[c] == n for c in final)]
                for n in sorted(set(sim.values()))
            ],
            ["simulator", *CAUSES, "total"],
        ),
    ]
    old = {
        r["id"]: r["final"]
        for p in OLD
        for r in json.loads(p.read_text(encoding="utf-8"))["result"]["results"]
    }
    both = [c for c in old if c in final]
    out += [
        "",
        "## Against the first audits (old prompts, 1200-character tool results)",
        "",
        f"Same final label: {rate(sum(old[c] == final[c] for c in both), len(both))}; "
        f"kappa {cohen_kappa([old[c] for c in both], [final[c] for c in both]):.3f}.",
    ]
    lab = {c: h for c, h in human.items() if h in CAUSES}
    out += [
        "",
        "## Against the human labels",
        "",
        f"Human labels: {len(human)} ({len(human) - len(lab)} UNCLEAR, excluded below).",
    ]
    by = {
        "final": final,
        "judge A": {j["id"]: j["a"]["primary_cause"] for j in done},
        "judge B": {j["id"]: j["b"]["primary_cause"] for j in done},
    }
    for who, label_of in by.items():
        got = [label_of.get(c) for c in lab]
        hits = sum(g == h for g, h in zip(got, lab.values(), strict=True))
        out.append(
            f"- {who}: accuracy {rate(hits, len(lab))}, "
            f"kappa {cohen_kappa(list(lab.values()), got):.3f}"
        )
    return "\n".join(out) + "\n"


def read_human():
    key = json.loads(D.joinpath("human_key.json").read_text(encoding="utf-8"))
    with D.joinpath("human_labels.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    labels = {key[r["blind_id"]]: r["label"].strip().upper() for r in rows}
    bad = [
        b
        for b, v in zip((r["blind_id"] for r in rows), labels.values(), strict=True)
        if v and v not in (*CAUSES, "UNCLEAR")
    ]
    if bad:
        raise SystemExit(f"unknown labels in human_labels.csv: {bad}")
    return labels if all(labels.values()) else None


def main():
    judged = json.loads(D.joinpath("judges_raw.json").read_text(encoding="utf-8"))["result"]
    cases = json.loads(D.joinpath("cases.json").read_text(encoding="utf-8"))
    text = report(judged, cases, read_human())
    D.joinpath("report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
