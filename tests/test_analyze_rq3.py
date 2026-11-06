"""Tests for scripts/analyze_rq3.py on small synthetic tau2 results files.

Expected numbers are worked out by hand from the outcome tables below (or by calling the
library function the script must delegate to, with the same seed). No tau2 process, LLM
or git command is involved, and no real RQ3 data is read.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from whose_noise import metrics, variance

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "analyze_rq3.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("analyze_rq3", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyze_rq3"] = module
    spec.loader.exec_module(module)
    return module


rq3 = _load_module()

GPT_LLM, QWEN_LLM = "groq/openai/gpt-oss-120b", "groq/qwen/qwen3.8-27b"
COMMIT = {"base": "7aad81c37a3a78b4ed3e4866769f851400c64415",
          "fix": "2fb1ca5c2046ac6650d40ddb8c0c870a4696d6fa"}  # fmt: skip
N_BOOT, SEED = 200, 20261011
STAMP = "2026-11-21T12:00:00+05:30"
ASK = 'The exchange is ready. Please confirm with a "yes" if you\'d like me to proceed.'

# Outcome codes: 1 pass, 0 fail, "S" fail with a strict premature stop ("Yes.###STOP###"
# right after a confirmation request), "M" fail with a merged customer turn, None infra error.
TABLES: dict[str, dict[str, list[Any]]] = {
    "base/gpt-oss-120b": {"5": ["S", 1, 1], "9": [0, "S", "S"], "12": [1, 1, 0],
                          "17": ["M", 0, 1], "18": [1, 1, 1], "26": [0, 0, "S"]},
    "fix/gpt-oss-120b": {"5": [1, 1, 1], "9": [0, 0, "S"], "12": [1, 1, 1],
                         "17": [0, "M", 1], "18": [1, 1, 1], "26": [0, 1, 0]},
    "base/qwen3.8-27b": {"5": [1, 1, 1], "9": [1, 1, 1], "12": [1, 1, 1],
                         "17": [1, 0, 1], "18": [1, 1, 1], "26": [1, 1, 0]},
    "fix/qwen3.8-27b": {"5": [1, 1, 1], "9": [1, 1, 0], "12": [1, 1, 1],
                        "17": [1, 1, 1], "18": [1, 1, 1], "26": [1, 1, 1]},
}  # fmt: skip
TASKS = ("5", "9", "12", "17", "18", "26")


def _msg(role: str, text: str | None, *calls: str) -> dict[str, Any]:
    tool_calls = [{"id": f"c{i}", "name": n, "arguments": {}} for i, n in enumerate(calls)]
    return {"role": role, "content": text, "tool_calls": tool_calls or None}


def _messages(code: Any) -> list[dict[str, Any]]:
    opening = [
        _msg("assistant", "Hi! How can I help you today?"),
        _msg("user", "I want to exchange an item from order #W2378156."),
        _msg("assistant", None, "get_order_details"),
        {"role": "tool", "content": '{"order_id": "#W2378156"}', "tool_calls": None},
    ]
    if code == "S":
        return [*opening, _msg("assistant", ASK), _msg("user", "Yes.###STOP###")]
    last = (
        "I don't have my email handy.Okay, thanks. ###STOP###"
        if code == "M"
        else "No, thanks. ###STOP###"
    )
    return [*opening, _msg("assistant", "Done. Anything else?"), _msg("user", last)]


def _sim(task: str, trial: int, code: Any) -> dict[str, Any]:
    base = {"id": f"{task}-{trial}", "task_id": task, "trial": trial, "seed": 1000 + trial,
            "start_time": "2026-11-08T02:10:00", "duration": 1.0}  # fmt: skip
    if code is None:
        return {**base, "termination_reason": "infrastructure_error", "reward_info": None,
                "messages": [], "info": {"error_type": "RateLimitError"}}  # fmt: skip
    reward = 1.0 if code == 1 else 0.0
    return {**base, "termination_reason": "user_stop", "messages": _messages(code),
            "reward_info": {"reward": reward, "db_check": {"db_match": bool(reward)},
                            "action_checks": []}}  # fmt: skip


def write_run(
    root: Path, cell: str, table: dict[str, list[Any]], commit: str | None = None
) -> Path:
    version, sim = cell.split("/")
    llm = GPT_LLM if sim == "gpt-oss-120b" else QWEN_LLM
    data = {
        "info": {
            "git_commit": commit or COMMIT[version],
            "num_trials": 3,
            "seed": 300,
            "user_info": {"llm": llm, "llm_args": {"temperature": 0.0}},
            "agent_info": {"llm": "openai/qwen3-4b", "llm_args": {"temperature": 0.0}},
            "environment_info": {"domain_name": "retail"},
        },
        "tasks": [],
        "simulations": [_sim(t, i, c) for t, codes in table.items() for i, c in enumerate(codes)],
    }
    run_dir = root / f"rq3{version}_{sim}"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(json.dumps(data), encoding="utf-8")
    return run_dir / "results.json"


def write_all(root: Path, tables: dict[str, dict[str, list[Any]]] = TABLES) -> dict[str, Path]:
    return {cell: write_run(root, cell, tables[cell]) for cell in rq3.CELLS}


def run_cli(paths: dict[str, Path], out_dir: Path, *extra: str) -> int:
    argv = [a for c in rq3.CELLS for a in (rq3.INPUT_FLAGS[c], str(paths[c]))]
    return rq3.main([*argv, "--out-dir", str(out_dir), "--n-boot", str(N_BOOT),
                     "--analysis-timestamp", STAMP, *extra])  # fmt: skip


def read_result(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / "results.json").read_text(encoding="utf-8"))


def outcomes(table: dict[str, list[Any]], fn) -> dict[str, list[bool]]:
    return {t: [fn(c) for c in codes if c is not None] for t, codes in table.items()}


@pytest.fixture(autouse=True)
def six_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rq3, "TASKS", TASKS)


def test_confirmatory_numbers_by_hand(tmp_path: Path) -> None:
    assert run_cli(write_all(tmp_path), tmp_path / "out") == 0
    res = read_result(tmp_path / "out")
    assert res["section8"]["mode"] == "final"
    assert res["section8"]["analysed_tasks"] == list(TASKS)
    # B differs from the pre-registered 10,000, so the label must be provisional
    assert res["label"] == "PROVISIONAL" and len(res["provisional_reasons"]) == 1
    conf = res["confirmatory"]

    # H3a: strict stops gpt-oss base 4/18 (tasks 5, 9, 9, 26), fix 1/18 (task 9)
    h3a = conf["H3a"]
    assert h3a["rate_base"] == pytest.approx(4 / 18) and h3a["rate_fix"] == pytest.approx(1 / 18)
    assert h3a["estimate"] == pytest.approx(-3 / 18)
    is_stop = lambda c: c == "S"  # noqa: E731
    fix_stop = outcomes(TABLES["fix/gpt-oss-120b"], is_stop)
    base_stop = outcomes(TABLES["base/gpt-oss-120b"], is_stop)
    _, lo, hi = metrics.paired_bootstrap_diff(
        fix_stop, base_stop, k=1, n_boot=N_BOOT, alpha=0.05, seed=SEED
    )
    assert (h3a["ci_low"], h3a["ci_high"]) == pytest.approx((lo, hi))
    assert h3a["supported"] == (hi < 0)

    # pass^1: base gpt 8/18, fix gpt 11/18, base qwen 16/18, fix qwen 17/18
    h3b = conf["H3b"]
    assert h3b["gap_base"] == pytest.approx(16 / 18 - 8 / 18)
    assert h3b["gap_fix"] == pytest.approx(17 / 18 - 11 / 18)
    assert h3b["estimate"] == pytest.approx(2 / 18)
    succ = {c: outcomes(TABLES[c], lambda x: x == 1) for c in rq3.CELLS}
    p = {c: np.array([np.mean(succ[c][t]) for t in TASKS]) for c in rq3.CELLS}
    d = (p["base/qwen3.8-27b"] - p["base/gpt-oss-120b"]) - (
        p["fix/qwen3.8-27b"] - p["fix/gpt-oss-120b"]
    )
    idx = np.random.default_rng(SEED).integers(0, d.size, size=(N_BOOT, d.size))
    assert (h3b["ci_low"], h3b["ci_high"]) == pytest.approx(
        tuple(np.percentile(d[idx].mean(axis=1), [2.5, 97.5]))
    )
    assert conf["H3c"]["gpt-oss-120b"]["estimate"] == pytest.approx(3 / 18)
    assert conf["H3c"]["qwen3.8-27b"]["estimate"] == pytest.approx(1 / 18)
    assert res["pass_k"]["fix/qwen3.8-27b"]["pass^1"]["estimate"] == pytest.approx(17 / 18)


def test_screens_decomposition_and_exploratory(tmp_path: Path) -> None:
    assert run_cli(write_all(tmp_path), tmp_path / "out") == 0
    res = read_result(tmp_path / "out")
    d = res["H3d"]["base/gpt-oss-120b"]
    assert d["valid_simulations"] == 18
    assert (d["premature_stop"]["count"], d["merged_turns"]["count"]) == (4, 1)
    assert d["stop_after_unexecuted_confirmation"]["count"] >= 4  # loose includes strict
    assert (d["premature_stop"]["ci_low"], d["premature_stop"]["ci_high"]) == pytest.approx(
        metrics.wilson_ci(4, 18)
    )
    assert res["H3d"]["fix/qwen3.8-27b"]["premature_stop"]["count"] == 0

    # H3e delegates to variance.compare_decompositions with the same seed and B
    diff = res["H3e"]["difference_fix_minus_base"]["task_x_simulator"]
    runs, _, _ = rq3.load({c: str(p) for c, p in write_all(tmp_path / "again").items()})
    valid, _ = rq3.analysis_set(runs, list(TASKS))
    by = {v: valid[valid["role"].str.startswith(v + "/")] for v in ("base", "fix")}
    expected = variance.compare_decompositions(
        by["base"], by["fix"], design="crossed", labels=("base", "fix"), n_boot=N_BOOT, seed=SEED,
        sim_col="user_llm", n_reps=3, on_unbalanced="raise",
    ).loc["task_x_simulator"]  # fmt: skip
    assert diff["difference"] == pytest.approx(expected["difference"])
    assert diff["ci_low"] == pytest.approx(expected["ci_low"])

    # Without merged turns: (17, 0) base and (17, 1) fix leave; task 17 keeps [0, 1] in both
    ex = res["exploratory_without_merged"]
    assert ex["excluded_per_cell"] == {"base/gpt-oss-120b": 1, "base/qwen3.8-27b": 0,
                                       "fix/gpt-oss-120b": 1, "fix/qwen3.8-27b": 0}  # fmt: skip
    assert ex["H3a"]["estimate"] == pytest.approx(-1 / 6)
    assert ex["H3c"]["gpt-oss-120b"]["pass1_base"] == pytest.approx(17 / 36)  # mean of task rates
    assert ex["H3c"]["gpt-oss-120b"]["estimate"] == pytest.approx(1 / 6)  # mean of task diffs
    assert ex["H3b"]["estimate"] == pytest.approx(1 / 9)
    assert "## H3e" in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")


def test_section8_pending_awaiting_and_dropped(tmp_path: Path) -> None:
    tables = {c: dict(t) for c, t in TABLES.items()}
    tables["fix/qwen3.8-27b"]["26"] = [1, None, 1]  # infra error still in the file
    del tables["base/qwen3.8-27b"]["18"]  # not run yet
    paths = write_all(tmp_path, tables)
    assert run_cli(paths, tmp_path / "refused") == 2  # outcomes wait for complete data
    assert run_cli(paths, tmp_path / "a", "--allow-partial") == 0
    s8 = read_result(tmp_path / "a")["section8"]
    assert s8["mode"] == "partial"
    assert (s8["pending_tasks"], s8["awaiting_rerun_tasks"]) == (["18"], ["26"])
    assert s8["analysed_tasks"] == ["5", "9", "12", "17"]
    assert run_cli(paths, tmp_path / "b", "--retries-exhausted", "26", "--allow-partial") == 0
    s8 = read_result(tmp_path / "b")["section8"]
    assert s8["dropped_tasks"] == ["26"] and s8["mode"] == "partial"  # 18 still pending


def test_swapped_versions_are_warned(tmp_path: Path) -> None:
    paths = write_all(tmp_path)
    swapped = {**paths, "base/gpt-oss-120b": paths["fix/gpt-oss-120b"],
               "fix/gpt-oss-120b": paths["base/gpt-oss-120b"]}  # fmt: skip
    assert run_cli(swapped, tmp_path / "out") == 0
    res = read_result(tmp_path / "out")
    assert any("swapped" in w for w in res["protocol_warnings"])
    assert "protocol warnings (see below)" in res["provisional_reasons"]


def test_blind_sheet_round_trip(tmp_path: Path) -> None:
    paths = write_all(tmp_path)
    sheet_dir = tmp_path / "sheet"
    assert run_cli(paths, tmp_path / "unused", "--export-blind-sheet", str(sheet_dir)) == 0
    text = (sheet_dir / "blind_sheet.md").read_text(encoding="utf-8")
    assert "base/" not in text and "fix/" not in text  # version hidden
    key = json.loads((sheet_dir / "KEY_open_after_labelling.json").read_text(encoding="utf-8"))
    flagged = {
        c: sum(code == "S" for codes in TABLES[c].values() for code in codes) for c in rq3.CELLS
    }
    assert len(key) == sum(flagged.values()) == 5

    labels = sheet_dir / "blind_labels.csv"
    with open(labels, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r.update(expected_action="yes", deciding="likely")
    rows[0]["deciding"] = ""  # one left unlabelled
    with open(labels, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    key_path = str(sheet_dir / "KEY_open_after_labelling.json")
    assert (
        run_cli(paths, tmp_path / "out", "--hand-labels", str(labels), "--blind-key", key_path) == 0
    )
    hand = read_result(tmp_path / "out")["hand_read"]
    assert {c: v["flagged"] for c, v in hand["per_cell"].items()} == flagged
    assert sum(v["labelled"] for v in hand["per_cell"].values()) == 4
    assert hand["unlabelled"] == [rows[0]["blind_id"]] and hand["key_matches_current_flags"]

    rows[0]["deciding"] = "maybe"
    with open(labels, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert (
        run_cli(paths, tmp_path / "bad", "--hand-labels", str(labels), "--blind-key", key_path) == 2
    )
    assert run_cli(paths, tmp_path / "bad2", "--hand-labels", str(labels)) == 2


def test_outputs_are_deterministic(tmp_path: Path) -> None:
    paths = write_all(tmp_path)
    assert run_cli(paths, tmp_path / "a") == 0 and run_cli(paths, tmp_path / "b") == 0
    a, b = read_result(tmp_path / "a"), read_result(tmp_path / "b")
    assert a == b
    assert (tmp_path / "a" / "report.md").read_text() == (tmp_path / "b" / "report.md").read_text()
