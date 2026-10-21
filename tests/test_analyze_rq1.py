"""Tests for scripts/analyze_rq1.py on small synthetic tau2 results files.

Every expected number below is computed by hand from the outcome tables written in the
test (or by calling the library function the script must delegate to, with the same
seed). No tau2 process, LLM or git command is involved.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
import types
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from whose_noise import variance

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "analyze_rq1.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("analyze_rq1", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyze_rq1"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


rq1 = _load_module()

GPT_LLM = "groq/openai/gpt-oss-120b"
QWEN_LLM = "groq/qwen/qwen3.8-27b"
AFTER_TIME = "2026-10-13T10:00:00"  # after the 2026-10-12 20:23 IST cutoff
BEFORE_TIME = "2026-10-11T17:00:00"
N_BOOT = 200
STAMP = "2026-10-21T12:00:00+05:30"
INFRA = None  # marker for an infrastructure-error trial in an outcome list

# ---------------------------------------------------------------------------
# Synthetic results files
# ---------------------------------------------------------------------------


def _messages(task: str, trial: int, role_tag: str) -> list[dict[str, Any]]:
    """A short conversation whose customer turn is unique per (simulator, task, trial).

    Every pair of repeats therefore first diverges at message 1, a user message.
    """
    return [
        {"role": "assistant", "content": "Hi! How can I help you today?", "tool_calls": None},
        {"role": "user", "content": f"Help with order {task} ({role_tag} {trial})"},
        {"role": "assistant", "content": "Done. Anything else?", "tool_calls": None},
        {"role": "user", "content": "No, thanks. ###STOP###", "tool_calls": None},
    ]


def _sim(task: str, trial: int, outcome: int | None, start: str, role_tag: str) -> dict[str, Any]:
    base = {
        "id": f"{task}-{trial}",
        "task_id": task,
        "trial": trial,
        "seed": 1000 + trial,  # tau2: one seed per trial index
        "start_time": start,
        "duration": 1.0,
    }
    if outcome is None:
        return {
            **base,
            "termination_reason": "infrastructure_error",
            "reward_info": None,
            "messages": [],
            "info": {"error_type": "RateLimitError", "error_traceback": "litellm"},
        }
    return {
        **base,
        "termination_reason": "user_stop",
        "reward_info": {
            "reward": float(outcome),
            "db_check": {"db_match": bool(outcome)},
            "action_checks": [],
        },
        "messages": _messages(task, trial, role_tag),
    }


def write_run(
    root: Path,
    name: str,
    user_llm: str,
    outcomes: dict[str, list[int | None]],
    starts: dict[tuple[str, int], str] | None = None,
) -> Path:
    """Write ``root/name/results.json`` with one simulation per listed trial outcome."""
    starts = starts or {}
    tag = "g" if "gpt" in user_llm else "q"
    sims = [
        _sim(task, trial, y, starts.get((task, trial), AFTER_TIME), tag)
        for task, ys in outcomes.items()
        for trial, y in enumerate(ys)
    ]
    data = {
        "timestamp": "2026-10-11T16:00:00",
        "info": {
            "git_commit": "4ce7c0397c1eb65c9bbe59aeacfe1ca44a1cd699",
            "num_trials": 3,
            "seed": 300,
            "user_info": {"llm": user_llm, "llm_args": {"temperature": 0.0}},
            "agent_info": {"llm": "openai/qwen3-4b", "llm_args": {"temperature": 0.0}},
            "environment_info": {"domain_name": "retail"},
        },
        "tasks": [],
        "simulations": sims,
    }
    run_dir = root / name
    run_dir.mkdir(parents=True)
    path = run_dir / "results.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# Balanced design: 6 tasks x 2 simulators x 3 trials, nothing missing.
GPT_BAL = {
    "5": [1, 1, 1],
    "9": [0, 0, 0],
    "12": [1, 0, 1],
    "17": [0, 1, 0],
    "18": [1, 1, 0],
    "26": [0, 0, 1],
}
QWEN_BAL = {
    "5": [1, 1, 1],
    "9": [1, 1, 1],
    "12": [1, 1, 1],
    "17": [0, 0, 0],
    "18": [1, 0, 1],
    "26": [1, 1, 1],
}
TASKS_BAL = list(GPT_BAL)


def run_cli(
    tmp_path: Path, gpt: Path, qwen: Path, planned: list[str], out: str = "out", *extra: str
) -> tuple[int, Path]:
    out_dir = tmp_path / out
    argv = [
        "--gptoss",
        str(gpt),
        "--qwen",
        str(qwen),
        "--out-dir",
        str(out_dir),
        "--n-boot",
        str(N_BOOT),
        "--analysis-timestamp",
        STAMP,
        "--planned-tasks",
        *planned,
        *extra,
    ]
    return rq1.main(argv), out_dir


def read_result(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / "results.json").read_text(encoding="utf-8"))


@pytest.fixture
def balanced(tmp_path: Path) -> tuple[Path, Path]:
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, GPT_BAL)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, QWEN_BAL)
    return gpt, qwen


# ---------------------------------------------------------------------------
# Balanced, final design
# ---------------------------------------------------------------------------


def test_balanced_design_numbers(tmp_path: Path, balanced: tuple[Path, Path]) -> None:
    gpt, qwen = balanced
    code, out_dir = run_cli(tmp_path, gpt, qwen, TASKS_BAL)
    assert code == 0
    res = read_result(out_dir)

    status = res["data_status"]
    assert status["mode"] == "final"
    assert status["analysed_tasks"] == TASKS_BAL
    assert status["dropped_tasks"] == [] and not status["deviation_too_many_dropped"]

    # pass^k by hand: gpt per-task rates 1, 0, 2/3, 1/3, 2/3, 1/3 -> mean 0.5
    s2 = res["step2"]
    assert s2["per_simulator"]["gpt-oss-120b"]["pass^1"]["estimate"] == pytest.approx(0.5)
    assert s2["per_simulator"]["gpt-oss-120b"]["pass^3"]["estimate"] == pytest.approx(1 / 6)
    qwen_p1 = (1 + 1 + 1 + 0 + 2 / 3 + 1) / 6
    assert s2["per_simulator"]["qwen3.8-27b"]["pass^1"]["estimate"] == pytest.approx(qwen_p1)
    assert s2["per_simulator"]["qwen3.8-27b"]["pass^3"]["estimate"] == pytest.approx(4 / 6)
    assert s2["paired_contrast"]["pass^1"]["estimate"] == pytest.approx(0.5 - qwen_p1)
    # majority verdicts: gpt fails & qwen passes on 9 and 26; never the reverse
    mc = s2["mcnemar_majority"]
    assert (mc["gptoss_fail_qwen_pass"], mc["gptoss_pass_qwen_fail"]) == (2, 0)

    # The decomposition must equal the library call with the pre-registered settings.
    rows = [
        {"task_id": t, "user_llm": llm, "trial": r, "success": bool(y)}
        for llm, table in ((GPT_LLM, GPT_BAL), (QWEN_LLM, QWEN_BAL))
        for t, ys in table.items()
        for r, y in enumerate(ys)
    ]
    ref = variance.decompose_variance(
        pd.DataFrame(rows), sim_col="user_llm", n_reps=3, n_boot=N_BOOT, seed=20261011
    )
    s3 = res["step3"]
    for name, comp in ref.components.items():
        got = s3["components"][name]
        assert got["raw"] == pytest.approx(comp.raw, abs=1e-12)
        assert got["ci_low"] == pytest.approx(comp.ci_low, abs=1e-12)
        assert got["ci_high"] == pytest.approx(comp.ci_high, abs=1e-12)
        assert got["share_truncated"] == pytest.approx(comp.share, abs=1e-12, nan_ok=True)
        assert got["share_untruncated"] == pytest.approx(comp.raw_share, abs=1e-12, nan_ok=True)
    assert s3["p_bar"]["estimate"] == pytest.approx(ref.p_bar.estimate)
    assert s3["cell_flip_rate"]["estimate"] == pytest.approx(ref.flip_rate.estimate)
    assert s3["within_cell_disagreement"]["estimate"] == pytest.approx(
        ref.within_cell_disagreement.estimate
    )
    assert s3["between_simulator_disagreement"]["estimate"] == pytest.approx(
        ref.between_simulator_disagreement.estimate
    )
    # Pre-registered decision rules: lower bound of the raw component's CI > 0.
    for hyp, comp in (("H1a", "residual"), ("H1b", "task_x_simulator")):
        d = res["decisions"][hyp]
        assert d["supported"] is (ref.components[comp].ci_low > 0)

    # Noise floor on the gpt cells: 4 flipping cells with SS 2/3 each -> MS_E = 2/9.
    s5 = res["step5"]
    assert s5["n_eval_tasks"] == 6
    single = s5["trials_per_run_1"]
    assert single["residual"]["estimate"] == pytest.approx(2 / 9)
    z = stats.norm.ppf(0.975)
    assert single["band_95"]["estimate"] == pytest.approx(z * math.sqrt(2 * (2 / 9) / 6))
    triple = s5["trials_per_run_3"]
    assert triple["band_95"]["estimate"] == pytest.approx(z * math.sqrt(2 * (2 / 9) / 18))

    # Provenance: the input hash is the file's sha256.
    inp = {i["role"]: i for i in res["meta"]["inputs"]}
    assert inp["gpt-oss-120b"]["sha256"] == hashlib.sha256(gpt.read_bytes()).hexdigest()
    assert inp["qwen3.8-27b"]["n_simulations"] == 18
    assert res["meta"]["analysis_timestamp"] == STAMP
    # A non-default task set and B make the run provisional.
    assert res["confirmatory"] is False
    assert any("task set" in r for r in res["provisional_reasons"])

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "PARTIAL DATA" not in report
    for heading in (
        "## Section 8",
        "## Step 1",
        "## Step 2",
        "## Step 3",
        "## Pre-registered decisions",
        "## Section 11",
        "## Step 5",
        "## Section 12",
        "## Exploratory: H1c",
        "## Exploratory: simulator fidelity",
        "## Step 8",
    ):
        assert heading in report
    assert gpt.name in report and inp["gpt-oss-120b"]["sha256"] in report


def test_outputs_are_deterministic(tmp_path: Path, balanced: tuple[Path, Path]) -> None:
    gpt, qwen = balanced
    assert run_cli(tmp_path, gpt, qwen, TASKS_BAL, "a")[0] == 0
    assert run_cli(tmp_path, gpt, qwen, TASKS_BAL, "b")[0] == 0
    for name in ("results.json", "report.md"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_section11_flags_ceiling_arm() -> None:
    assert rq1.section11(0.5, {"a": 0.5, "b": 0.6})["shares_flagged_uninformative"] is False
    flagged = rq1.section11(0.5, {"a": 0.95, "b": 0.3})
    assert flagged["shares_flagged_uninformative"] is True
    assert flagged["arms"]["a"]["outside"] and not flagged["arms"]["b"]["outside"]
    assert rq1.section11(0.05, {"a": 0.5})["pooled_outside"] is True


# ---------------------------------------------------------------------------
# Partial and unbalanced data; the section 8 drop rule
# ---------------------------------------------------------------------------


def test_partial_data_analyses_only_tasks_complete_for_both(tmp_path: Path) -> None:
    planned = [*TASKS_BAL, "27", "32"]
    gpt_table = {**GPT_BAL, "27": [1, 1, 0], "32": [0, 0, 0]}
    qwen_table = {**QWEN_BAL, "27": [1, 1]}  # 27 has 2 of 3 trials; 32 not started
    qwen_table["26"] = [1]  # task 26 only has trial 0 so far
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, gpt_table)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, qwen_table)
    code, out_dir = run_cli(tmp_path, gpt, qwen, planned)
    assert code == 0
    res = read_result(out_dir)
    status = res["data_status"]
    assert status["mode"] == "partial"
    assert status["analysed_tasks"] == ["5", "9", "12", "17", "18"]
    assert status["pending_tasks"] == ["26", "27", "32"]
    assert status["dropped_tasks"] == []  # nothing is dropped while collection runs
    assert status["complete_by_simulator"]["gpt-oss-120b"] == planned
    assert res["step3"]["n_tasks"] == 5
    assert res["step2"]["per_simulator"]["gpt-oss-120b"]["n_tasks"] == 5
    # The supplementary per-simulator block uses each simulator's own complete tasks.
    supp = res["step2"]["supplementary_own_complete_tasks"]
    assert supp["gpt-oss-120b"]["n_tasks"] == 8 and supp["qwen3.8-27b"]["n_tasks"] == 5
    assert res["confirmatory"] is False
    assert any(r.startswith("partial data") for r in res["provisional_reasons"])
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert report.index("PARTIAL DATA") < report.index("## Key results")
    assert "PROVISIONAL" in report


PLANNED8 = ["5", "9", "12", "17", "18", "26", "27", "32"]


def _infra_tables(
    infra_tasks: list[str],
) -> tuple[dict[str, list[int | None]], dict[str, list[int | None]]]:
    """Every cell attempted 3 times; the listed tasks keep one infra error (gpt, qwen, ...)."""
    gpt_table: dict[str, list[int | None]] = {t: [1, 0, 1] for t in PLANNED8}
    qwen_table: dict[str, list[int | None]] = {t: [1, 1, 0] for t in PLANNED8}
    for i, task in enumerate(infra_tasks):
        table = gpt_table if i % 2 == 0 else qwen_table
        table[task] = [1, INFRA, 1]
    return gpt_table, qwen_table


def test_infra_error_cells_await_rerun_without_confirmation(tmp_path: Path) -> None:
    # tau2 --auto-resume deletes infra-error sims and re-runs them, so a cell that holds an
    # infra error has NOT necessarily used its 3 extra attempts (section 8): no drop.
    infra_tasks = PLANNED8[:2]
    gpt_table, qwen_table = _infra_tables(infra_tasks)
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, gpt_table)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, qwen_table)
    code, out_dir = run_cli(tmp_path, gpt, qwen, PLANNED8)
    assert code == 0
    res = read_result(out_dir)
    status = res["data_status"]
    assert status["mode"] == "partial"
    assert status["dropped_tasks"] == [] and status["n_dropped"] == 0
    assert status["awaiting_rerun_tasks"] == infra_tasks
    assert status["pending_tasks"] == []
    assert status["analysed_tasks"] == PLANNED8[2:]
    assert res["confirmatory"] is False
    partial = next(r for r in res["provisional_reasons"] if r.startswith("partial data"))
    assert "awaiting --auto-resume re-runs" in partial and "5, 9" in partial
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "PARTIAL DATA" in report and "awaiting re-run" in report
    assert "Confirmatory run" not in report


@pytest.mark.parametrize(("n_infra_tasks", "deviation"), [(3, False), (4, True)])
def test_dropped_tasks_removed_from_every_condition(
    tmp_path: Path, n_infra_tasks: int, deviation: bool
) -> None:
    infra_tasks = PLANNED8[:n_infra_tasks]
    gpt_table, qwen_table = _infra_tables(infra_tasks)
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, gpt_table)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, qwen_table)
    # The user confirms that the re-runs of these tasks are exhausted (section 8).
    code, out_dir = run_cli(
        tmp_path, gpt, qwen, PLANNED8, "out", "--retries-exhausted", *infra_tasks
    )
    assert code == 0
    res = read_result(out_dir)
    status = res["data_status"]
    assert status["mode"] == "final"
    assert status["dropped_tasks"] == infra_tasks
    assert status["awaiting_rerun_tasks"] == []
    assert status["deviation_too_many_dropped"] is deviation
    kept = PLANNED8[n_infra_tasks:]
    assert status["analysed_tasks"] == kept
    # The confirmation is recorded per dropped task.
    per_task = {r["task_id"]: r for r in status["per_task"]}
    for i, task in enumerate(infra_tasks):
        (ev,) = per_task[task]["retries_exhausted_evidence"]
        assert ev["source"].startswith("--retries-exhausted")
        assert ev["simulators"] == [("gpt-oss-120b", "qwen3.8-27b")[i % 2]]
    assert res["meta"]["settings"]["rerun_evidence"]["retries_exhausted_cli"] == infra_tasks
    # Dropped whole: the complete cell of the OTHER simulator is excluded too.
    for role in ("gpt-oss-120b", "qwen3.8-27b"):
        assert status["complete_by_simulator"][role] == kept
        assert res["step1_conditions"][role]["analysed_set"]["simulations"] == 3 * len(kept)
        assert sorted(res["step2"]["per_task"][role], key=int) == kept
        # ... and from the wider fidelity table.
        rows = {r["outcome"]: r for r in res["fidelity"]["all_simulations"][role]["summary"]}
        assert rows["all"]["sims"] == 3 * len(kept)
    # With final data the per-simulator supplementary block would repeat the analysis set.
    assert "supplementary_own_complete_tasks" not in res["step2"]
    assert res["step1_conditions"]["gpt-oss-120b"]["file"]["infra_errors"] == (
        (n_infra_tasks + 1) // 2
    )
    assert res["step3"]["n_tasks"] == len(kept)
    assert any("task set" in r for r in res["provisional_reasons"])
    assert not any(r.startswith("partial data") for r in res["provisional_reasons"])
    assert any("tasks dropped" in r for r in res["provisional_reasons"]) is deviation
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert ("DEVIATION" in report) is deviation
    assert "Dropped tasks" in report and "confirmed exhausted" in report
    assert "PARTIAL DATA" not in report


def test_rerun_evidence_file(tmp_path: Path) -> None:
    planned = [*PLANNED8, "33"]
    gpt_table, qwen_table = _infra_tables(["5", "9"])  # gpt 5 and qwen 9 keep an infra error
    gpt_table["33"] = [1, 1]  # 33 not finished: the data stay partial
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, gpt_table)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, qwen_table)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "source": "counted from the run logs",
                "pre_rerun_infra_errors": {"gpt-oss-120b": 4, "qwen3.8-27b": 1},
                "extra_attempts": {
                    "gpt-oss-120b": {"5": 3, "12": 4},  # 12: more than section 8 allows
                    "qwen3.8-27b": {"9": 2},  # only 2 of 3 extra attempts so far
                },
            }
        ),
        encoding="utf-8",
    )
    extra_args = ["--rerun-evidence", str(evidence), "--retries-exhausted", "12"]
    code, out_dir = run_cli(tmp_path, gpt, qwen, planned, "out", *extra_args)
    assert code == 0
    res = read_result(out_dir)
    status = res["data_status"]
    assert status["mode"] == "partial"
    assert status["dropped_tasks"] == ["5"]  # gpt cell of task 5: 3 extra attempts
    assert status["awaiting_rerun_tasks"] == ["9"]
    assert status["pending_tasks"] == ["33"]
    ev = next(r for r in status["per_task"] if r["task_id"] == "5")["retries_exhausted_evidence"]
    assert ev[0]["extra_attempts"] == 3 and "counted from the run logs" in ev[0]["source"]
    # --retries-exhausted on a complete task is ignored, and listed.
    assert status["retries_exhausted_ignored"][0]["task_id"] == "12"
    # The dropped task leaves the per-simulator supplementary block too.
    supp = res["step2"]["supplementary_own_complete_tasks"]
    assert "5" not in supp["gpt-oss-120b"]["tasks"] and "5" not in supp["qwen3.8-27b"]["tasks"]
    assert supp["label"].startswith("exploratory")
    # Infra errors before re-runs come from the evidence file.
    pre = res["step1_pre_rerun_infra"]
    assert pre["available"] is True
    gpt_pre = pre["per_simulator"]["gpt-oss-120b"]
    assert gpt_pre["infra_errors_before_reruns"] == 4
    assert gpt_pre["simulations_in_file"] == 3 * 8 + 2
    assert gpt_pre["rate_before_reruns"] == pytest.approx(4 / 26)
    # More extra attempts than section 8 allows is a protocol warning.
    assert any("12 cell had 4 extra attempts" in w for w in res["protocol_warnings"])
    assert any(r.startswith("protocol warning") for r in res["provisional_reasons"])
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "infra before re-runs" in report and "counted from the run logs" in report

    # Inconsistent evidence (fewer pre-re-run errors than the file still holds) is flagged.
    evidence.write_text(
        json.dumps({"source": "x", "pre_rerun_infra_errors": {"qwen3.8-27b": 0}}),
        encoding="utf-8",
    )
    code, out_dir = run_cli(tmp_path, gpt, qwen, planned, "out2", "--rerun-evidence", str(evidence))
    res = read_result(out_dir)
    assert res["step1_pre_rerun_infra"]["available"] is False  # gpt count missing
    assert any("still holds 1" in w for w in res["protocol_warnings"])
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "Not available from results.json" in report

    # Malformed evidence files are input errors (exit 2).
    for bad in (
        {"pre_rerun_infra_errors": {"gpt-oss-120b": 1}},  # no source
        {"source": "x", "extra_attempts": {"gemini": {"5": 3}}},  # unknown simulator
        {"source": "x", "pre_rerun_infra_errors": {"gpt-oss-120b": -1}},
        {"source": "x", "typo": 1},
    ):
        evidence.write_text(json.dumps(bad), encoding="utf-8")
        bad_code, _ = run_cli(
            tmp_path, gpt, qwen, planned, "bad", "--rerun-evidence", str(evidence)
        )
        assert bad_code == 2


def test_pre_rerun_infra_unavailable_without_evidence(
    tmp_path: Path, balanced: tuple[Path, Path]
) -> None:
    gpt, qwen = balanced
    code, out_dir = run_cli(tmp_path, gpt, qwen, TASKS_BAL)
    assert code == 0
    pre = read_result(out_dir)["step1_pre_rerun_infra"]
    assert pre["available"] is False
    assert pre["per_simulator"]["gpt-oss-120b"]["infra_errors_before_reruns"] is None
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "Not available from results.json" in report
    assert "gpt-oss-120b had 3 infra errors on first attempts" in report


def _final_status(**overrides: Any) -> dict[str, Any]:
    status = {
        "mode": "final",
        "pending_tasks": [],
        "awaiting_rerun_tasks": [],
        "n_analysed": 29,
        "n_planned": 29,
        "n_dropped": 0,
        "deviation_too_many_dropped": False,
    }
    return {**status, **overrides}


def test_provisional_reasons() -> None:
    cutoff = rq1.parse_time(rq1.CACHE_RAM_CUTOFF)
    args = (rq1.PREREG_N_BOOT, rq1.PREREG_SEED, rq1.PREREG_TASKS)
    assert rq1.provisional(_final_status(), *args, cutoff, []) == []
    # The same instant written with an offset is the pre-registered cutoff.
    assert rq1.provisional(_final_status(), *args, rq1.parse_time("2026-10-12T14:53Z"), []) == []
    moved = rq1.provisional(_final_status(), *args, rq1.parse_time("2026-10-19T00:00"), [])
    assert len(moved) == 1 and "cache-ram cutoff 2026-10-19T00:00:00" in moved[0]
    warned = rq1.provisional(_final_status(), *args, cutoff, ["user_info temperature 0.7"])
    assert warned == ["protocol warning: user_info temperature 0.7"]
    awaiting = rq1.provisional(
        _final_status(mode="partial", awaiting_rerun_tasks=["33"], n_analysed=28),
        *args,
        cutoff,
        [],
    )
    assert awaiting[0].startswith("partial data: 28 of 29") and "33" in awaiting[0]


def test_protocol_warning_makes_run_provisional(tmp_path: Path) -> None:
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, GPT_BAL)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, QWEN_BAL)
    data = json.loads(qwen.read_text(encoding="utf-8"))
    data["info"]["user_info"]["llm_args"]["temperature"] = 0.7
    qwen.write_text(json.dumps(data), encoding="utf-8")
    code, out_dir = run_cli(
        tmp_path, gpt, qwen, TASKS_BAL, "out", "--cache-ram-cutoff", "2026-10-19T00:00:00"
    )
    assert code == 0
    res = read_result(out_dir)
    reasons = res["provisional_reasons"]
    assert any(r.startswith("protocol warning") and "temperature 0.7" in r for r in reasons)
    assert any("cache-ram cutoff" in r for r in reasons)
    assert res["confirmatory"] is False


# ---------------------------------------------------------------------------
# Section 12: before / after the --cache-ram change
# ---------------------------------------------------------------------------


def test_parse_time_and_period() -> None:
    cutoff = rq1.parse_time(rq1.CACHE_RAM_CUTOFF)
    assert rq1.period_of(rq1.parse_time("2026-10-12T20:22:59.9"), cutoff) == "before"
    assert rq1.period_of(rq1.parse_time("2026-10-12T20:23:00"), cutoff) == "after"
    # Offsets are converted to IST: 14:52 UTC = 20:22 IST, 14:53Z = 20:23 IST.
    assert rq1.period_of(rq1.parse_time("2026-10-12T14:52:00+00:00"), cutoff) == "before"
    assert rq1.period_of(rq1.parse_time("2026-10-12T14:53:00Z"), cutoff) == "after"
    assert rq1.period_of(rq1.parse_time(None), cutoff) == "unknown"
    assert rq1.period_of(rq1.parse_time("not a time"), cutoff) == "unknown"


def test_before_after_split_by_hand(tmp_path: Path) -> None:
    gpt_table = {"5": [1, 0, 1], "9": [0, 1, 1], "12": [1, 1, 1]}
    qwen_table = {"5": [1, 1, 1], "9": [0, 0, 1], "12": [1, 1, 0]}
    gpt_starts = {("5", 0): BEFORE_TIME, ("5", 1): BEFORE_TIME}  # gpt task 5: mixed cell
    qwen_starts = {("5", r): BEFORE_TIME for r in range(3)}  # qwen task 5: before cell
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, gpt_table, gpt_starts)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, qwen_table, qwen_starts)
    code, out_dir = run_cli(tmp_path, gpt, qwen, ["5", "9", "12"])
    assert code == 0
    s12 = read_result(out_dir)["section12"]

    pl = s12["pair_level"]
    # before_before: gpt 5 (0,1) disagree + qwen 5 three agreeing pairs -> 1/4 / 2
    assert (pl["before_before"]["n_pairs"], pl["before_before"]["n_disagreeing"]) == (4, 1)
    assert pl["before_before"]["residual"]["estimate"] == pytest.approx(1 / 8)
    # cross: gpt 5 (0,2) agree, (1,2) disagree
    assert (pl["cross"]["n_pairs"], pl["cross"]["n_disagreeing"]) == (2, 1)
    assert pl["cross"]["residual"]["estimate"] == pytest.approx(1 / 4)
    # after_after: 4 cells x 3 pairs; disagreeing: gpt 9 (2), gpt 12 (0), qwen 9 (2), qwen 12 (2)
    assert (pl["after_after"]["n_pairs"], pl["after_after"]["n_disagreeing"]) == (12, 6)
    assert pl["after_after"]["residual"]["estimate"] == pytest.approx(6 / 12 / 2)
    assert pl["after_after_minus_before_before"]["estimate"] == pytest.approx(1 / 4 - 1 / 8)

    wm = s12["within_mixed_cells"]
    assert wm["same_period"]["residual"]["estimate"] == pytest.approx(1 / 2)
    assert wm["cross"]["residual"]["estimate"] == pytest.approx(1 / 4)
    assert wm["cross_minus_same_period"]["estimate"] == pytest.approx(-1 / 4)

    cl = s12["cell_level"]
    assert cl["before"]["n_cells"] == 1 and cl["mixed"]["n_cells"] == 1
    assert cl["after"]["n_cells"] == 4
    assert cl["before"]["residual"]["estimate"] == pytest.approx(0.0)
    assert cl["mixed"]["residual"]["estimate"] == pytest.approx(1 / 3)  # SS 2/3 over R-1 = 2
    assert cl["mixed"]["cell_flip_rate"]["estimate"] == pytest.approx(1.0)
    assert cl["after"]["cell_flip_rate"]["estimate"] == pytest.approx(3 / 4)
    assert cl["before"]["few_tasks"] and cl["before"]["p_bar_outside"]  # 1 cell, p_bar = 1

    comp = s12["composition"]
    assert comp["simulations"]["gpt-oss-120b"] == {"before": 2, "after": 7, "unknown": 0}
    assert comp["simulations"]["qwen3.8-27b"] == {"before": 3, "after": 6, "unknown": 0}
    assert s12["tasks_with_runs_before_cutoff"]["qwen3.8-27b"] == ["5"]
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "Confound" in report and "not interpretable" in report


def test_pair_and_cell_stats_are_batch_aware() -> None:
    # (T=2, classes=3, [pairs, disagreeing]); a batch of 2 identical draws
    a = np.array([[[1, 1], [2, 0], [0, 0]], [[1, 0], [2, 2], [2, 1]]], dtype=float)
    single = rq1._pair_stat(a)
    batch = rq1._pair_stat(np.stack([a, a]))
    np.testing.assert_allclose(batch[0], single)
    np.testing.assert_allclose(single[:3], [1 / 4, 2 / 8, 1 / 4])
    assert single[3] == pytest.approx(2 / 8 - 1 / 4)


# ---------------------------------------------------------------------------
# H1c (whose_noise.divergence) and fidelity
# ---------------------------------------------------------------------------


def test_divergence_import_failure_is_graceful(
    tmp_path: Path, balanced: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "whose_noise.divergence", None)  # import -> ImportError
    gpt, qwen = balanced
    code, out_dir = run_cli(tmp_path, gpt, qwen, TASKS_BAL)
    assert code == 0
    res = read_result(out_dir)
    assert res["h1c"]["status"] == "unavailable"
    # ModuleNotFoundError is the ImportError subclass raised for a None sys.modules entry
    assert res["h1c"]["error"].split(":")[0] in ("ImportError", "ModuleNotFoundError")
    assert "whose_noise.divergence" in res["h1c"]["error"]
    assert res["decisions"]["H1a"]["verdict"] in ("SUPPORTED", "NOT SUPPORTED")
    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "H1c section UNAVAILABLE" in report
    assert "## Pre-registered decisions" in report


def test_divergence_api_mismatch_is_graceful(
    tmp_path: Path, balanced: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = types.ModuleType("whose_noise.divergence")  # imports, but has no functions
    monkeypatch.setitem(sys.modules, "whose_noise.divergence", empty)
    gpt, qwen = balanced
    code, out_dir = run_cli(tmp_path, gpt, qwen, TASKS_BAL)
    assert code == 0
    h1c = read_result(out_dir)["h1c"]
    assert h1c["status"] == "failed" and "AttributeError" in h1c["error"]


def test_divergence_and_fidelity_sections(tmp_path: Path, balanced: tuple[Path, Path]) -> None:
    module, error = rq1.import_divergence()
    if module is None:
        pytest.skip(f"whose_noise.divergence not importable: {error}")
    gpt, qwen = balanced
    code, out_dir = run_cli(tmp_path, gpt, qwen, TASKS_BAL)
    assert code == 0
    res = read_result(out_dir)
    h1c = res["h1c"]
    assert h1c["status"] == "ok", h1c.get("error")
    assert h1c["n_pairs"] == 6 * 2 * 3  # tasks x simulators x C(3, 2)
    pooled = next(r for r in h1c["summary"] if r["subset"] == "all" and r["group"] == "pooled")
    assert pooled["share_user"] == pytest.approx(1.0)  # every pair differs at message 1
    groups = {r["group"] for r in h1c["summary"]}
    assert {"gpt-oss-120b", "qwen3.8-27b", "pooled"} <= groups

    fid = res["fidelity"]
    assert fid["status"] == "ok"
    for role in ("gpt-oss-120b", "qwen3.8-27b"):
        rows = {r["outcome"]: r for r in fid["analysed_tasks"][role]["summary"]}
        assert rows["all"]["sims"] == 18
        ci = fid["analysed_tasks"][role]["task_bootstrap_ci_all_outcomes"]["premature_stop"]
        assert ci["estimate"] == pytest.approx(0.0)


def test_h1c_period_split_and_turn_order_context(tmp_path: Path) -> None:
    module, error = rq1.import_divergence()
    if module is None:
        pytest.skip(f"whose_noise.divergence not importable: {error}")
    # gpt task 5: trials 0 and 1 before the cache-ram cutoff, trial 2 after.
    gpt_starts = {("5", 0): BEFORE_TIME, ("5", 1): BEFORE_TIME}
    gpt = write_run(tmp_path, "run_gpt", GPT_LLM, GPT_BAL, gpt_starts)
    qwen = write_run(tmp_path, "run_qwen", QWEN_LLM, QWEN_BAL)
    # In gpt task 5 the customer turn is identical and the AGENT reply differs (index 2).
    data = json.loads(gpt.read_text(encoding="utf-8"))
    for sim in data["simulations"]:
        if sim["task_id"] == "5":
            sim["messages"][1]["content"] = "Help with order 5"
            sim["messages"][2]["content"] = f"Done ({sim['trial']}). Anything else?"
    gpt.write_text(json.dumps(data), encoding="utf-8")
    code, out_dir = run_cli(tmp_path, gpt, qwen, TASKS_BAL)
    assert code == 0
    h = read_result(out_dir)["h1c"]
    assert h["status"] == "ok", h.get("error")

    main = {(r["subset"], r["group"]): r for r in h["summary"]}
    gpt_all = main[("all", "gpt-oss-120b")]
    assert (gpt_all["n_user"], gpt_all["n_assistant"]) == (15, 3)
    # Period classes of the gpt task 5 pairs: (0,1) before_before, (0,2) and (1,2) cross.
    by = {(r["subset"], r["simulator"], r["period_class"]): r for r in h["by_period_class"]}
    assert by[("all", "gpt-oss-120b", "before_before")]["n_pairs"] == 1
    assert by[("all", "gpt-oss-120b", "cross")]["n_pairs"] == 2
    assert by[("all", "gpt-oss-120b", "after_after")]["n_pairs"] == 15
    assert by[("all", "qwen3.8-27b", "after_after")]["n_pairs"] == 18
    assert by[("all", "gpt-oss-120b", "cross")]["share_agent"] == pytest.approx(1.0)
    assert "n_boot_undefined" in by[("all", "gpt-oss-120b", "cross")]
    aa = {(r["subset"], r["group"]): r for r in h["after_after_only"]}
    assert aa[("all", "pooled")]["n_pairs"] == 33
    assert aa[("all", "pooled")]["share_user"] == pytest.approx(1.0)
    assert h["period_tasks"]["gpt-oss-120b"]["without_after_after"] == ["5"]
    assert h["period_tasks"]["qwen3.8-27b"]["without_after_after"] == []

    # Identical-input generations: the greeting (index 0) is scripted, not a generation.
    ctx = h["context"]["gpt-oss-120b"]
    assert ctx["generations"]["agent"] == {
        "identical_input_generations": 3,
        "first_divergences": 3,
        "rate": pytest.approx(1.0),
    }
    assert ctx["generations"]["user"]["identical_input_generations"] == 18
    assert ctx["generations"]["user"]["first_divergences"] == 15
    assert ctx["n_diverging"] == 18
    assert ctx["n_diverging_before_any_agent_generation"] == 15
    assert ctx["divergence_index"] == {"assistant": {"2": 3}, "user": {"1": 15}}
    assert h["context"]["pooled"]["n_diverging_before_any_agent_generation"] == 15 + 18

    report = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "H1c by period class" in report and "after_after pairs only" in report
    assert "few tasks: not interpretable" in report  # gpt before_before has 1 task
    assert "n undefined" in report.split("## Exploratory: H1c")[1]
    assert "Seeds:" in report and "Turn order:" in report


# ---------------------------------------------------------------------------
# Input errors and provenance helpers
# ---------------------------------------------------------------------------


def test_input_errors_exit_2(tmp_path: Path, balanced: tuple[Path, Path]) -> None:
    gpt, _ = balanced
    same = write_run(tmp_path, "run_same", GPT_LLM, QWEN_BAL)
    assert run_cli(tmp_path, gpt, same, TASKS_BAL)[0] == 2  # one simulator only
    assert run_cli(tmp_path, gpt, tmp_path / "missing.json", TASKS_BAL)[0] == 2
    dup_path = write_run(tmp_path, "run_dup", QWEN_LLM, QWEN_BAL)
    data = json.loads(dup_path.read_text(encoding="utf-8"))
    data["simulations"].append(data["simulations"][0])
    dup_path.write_text(json.dumps(data), encoding="utf-8")
    assert run_cli(tmp_path, gpt, dup_path, TASKS_BAL)[0] == 2


def test_swapped_inputs_are_warned(tmp_path: Path, balanced: tuple[Path, Path]) -> None:
    gpt, qwen = balanced
    code, out_dir = run_cli(tmp_path, qwen, gpt, TASKS_BAL)
    assert code == 0
    warnings_ = read_result(out_dir)["protocol_warnings"]
    assert any("swapped" in w for w in warnings_)


def test_read_git_info_without_git(tmp_path: Path) -> None:
    head = "a" * 40
    tag_obj = "b" * 40
    git = tmp_path / ".git"
    (git / "objects" / tag_obj[:2]).mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "packed-refs").write_text(
        f"# pack-refs with: peeled\n{head} refs/heads/main\n{tag_obj} refs/tags/other\n",
        encoding="utf-8",
    )
    # An annotated tag as a loose object: the commit is on its "object" line.
    body = f"object {head}\ntype commit\ntag prereg-v1\n\nfrozen\n".encode()
    raw = b"tag " + str(len(body)).encode() + b"\0" + body
    (git / "objects" / tag_obj[:2] / tag_obj[2:]).write_bytes(zlib.compress(raw))
    (git / "refs" / "tags").mkdir(parents=True)
    (git / "refs" / "tags" / "prereg-v1").write_text(tag_obj + "\n", encoding="utf-8")
    info = rq1.read_git_info(tmp_path)
    assert info["head_ref"] == "refs/heads/main"
    assert info["head_commit"] == head
    assert info["tag_commit"] == head
    assert info["head_is_tag"] is True
    assert rq1.read_git_info(tmp_path / "nowhere")["head_commit"] is None
