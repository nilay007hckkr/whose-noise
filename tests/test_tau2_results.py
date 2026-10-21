"""Tests for whose_noise.tau2_results (loader + outcome grouping)."""

from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

import pandas as pd
import pytest

from whose_noise.tau2_results import (
    COLUMNS,
    DuplicateTrialError,
    DuplicateTrialWarning,
    is_successful_reward,
    load_runs,
    outcomes_by_task,
    trial_integrity_problems,
)

# ----------------------------------------------------------------- real (trimmed) fixture


def test_fixture_is_trimmed_and_redacted(mini_results: Path) -> None:
    text = mini_results.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["info"]["agent_info"]["llm_args"]["api_key"] == "REDACTED"
    assert data["info"]["environment_info"]["policy"] is None
    assert all(s["policy"] is None for s in data["simulations"])
    assert len(data["simulations"]) == 3
    assert "# Retail agent policy" not in text
    # No provider organisation id (even a truncated one) may be committed.
    assert re.findall(r"org_(?!REDACTED)[A-Za-z0-9]+", text) == []


def _fixture_maker():
    path = Path(__file__).parent / "fixtures" / "make_mini_results.py"
    spec = importlib.util.spec_from_file_location("make_mini_results", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_trimmer_redacts_org_ids_before_truncation() -> None:
    maker = _fixture_maker()
    # Shaped like a Groq RateLimitError: the org id starts at offset 145, inside the
    # 160-character truncation window, so truncation alone would leak a partial id.
    msg = "x" * 145 + "org_01abcdefghijklmnopqrstuvwx" + " Limit 200000, Used 199392" * 3
    sim = {"info": {"error": msg, "error_traceback": "File src/tau2/a.py org_99zz"}}
    out = maker.trim(sim)
    assert "org_01" not in json.dumps(out)
    assert out["info"]["error"].startswith("x" * 145 + "org_REDACTED")
    assert out["info"]["error_traceback"] == "File src/tau2/a.py org_REDACTED"


def test_load_mini_columns_and_order(mini_results: Path) -> None:
    df = load_runs(mini_results)
    assert list(df.columns) == list(COLUMNS)
    assert list(df["task_id"]) == ["0", "1", "2"]
    assert set(df["run_name"]) == {"fixtures"}
    assert set(df["agent_llm"]) == {"openai/qwen3-4b"}
    assert set(df["user_llm"]) == {"groq/openai/gpt-oss-120b"}
    assert set(df["domain"]) == {"retail"}
    assert df["trial"].tolist() == [0, 0, 0]


def test_load_mini_task0_success(mini_results: Path) -> None:
    row = load_runs(mini_results).set_index("task_id").loc["0"]
    assert row["reward"] == 1.0
    assert bool(row["success"]) is True
    assert bool(row["db_match"]) is True
    assert not row["is_infra_error"]
    assert row["termination_reason"] == "user_stop"
    assert row["failed_write_actions"] == ()
    assert pd.isna(row["first_failed_action"])
    # Counted from the raw file: 26 messages, 13 assistant / 7 user, 6 tool calls.
    assert (row["n_messages"], row["n_agent_turns"], row["n_user_turns"]) == (26, 13, 7)
    assert row["n_tool_calls"] == 6
    assert row["user_prompt_tokens"] + row["user_completion_tokens"] == 8210 + 966
    assert row["agent_prompt_tokens"] == 90842
    assert row["duration_s"] == pytest.approx(69.153, abs=1e-3)


def test_load_mini_task1_failure(mini_results: Path) -> None:
    row = load_runs(mini_results).set_index("task_id").loc["1"]
    assert row["reward"] == 0.0
    assert bool(row["success"]) is False
    assert bool(row["db_match"]) is False
    assert row["first_failed_action"] == "exchange_delivered_order_items"
    assert row["failed_write_actions"] == ("exchange_delivered_order_items",)


def test_load_mini_task2_infra_error(mini_results: Path) -> None:
    df = load_runs(mini_results)
    row = df.set_index("task_id").loc["2"]
    assert row["is_infra_error"]
    assert math.isnan(row["reward"])
    assert pd.isna(row["success"]) and pd.isna(row["db_match"])
    assert row["n_messages"] == 0 and row["agent_prompt_tokens"] == 0
    assert outcomes_by_task(df) == {"0": [True], "1": [False]}
    # The real error: tau2's NL-assertion judge (an OpenAI model) had no API key,
    # i.e. the conversation finished but scoring crashed.
    assert row["error_type"] == "AuthenticationError"
    assert row["error_stage"] == "evaluation"
    # valid rows carry no error info
    assert df["error_stage"].isna().sum() == 2


def test_directory_without_results_json_raises(mini_results: Path) -> None:
    # fixtures/ holds mini_results.json, not results.json
    with pytest.raises(FileNotFoundError):
        load_runs(mini_results.parent)


# ----------------------------------------------------------------- synthetic ground truth


def test_synthetic_sorting_and_counts(synthetic_results: Path) -> None:
    df = load_runs(synthetic_results.parent)  # directory form
    assert df["run_name"].unique().tolist() == ["synth_run"]
    # numeric ids sort numerically ("2" before "10"), non-numeric last; trials ascending
    assert list(zip(df["task_id"], df["trial"], strict=True)) == [
        ("2", 0),
        ("2", 1),
        ("10", 0),
        ("10", 1),
        ("x", 0),
        ("x", 1),
        ("x", 2),
    ]
    valid = df[~df["is_infra_error"]]
    assert (valid["agent_prompt_tokens"] == 300).all()
    assert (valid["agent_completion_tokens"] == 30).all()
    assert (valid["user_prompt_tokens"] == 110).all()
    assert (valid["user_completion_tokens"] == 11).all()
    assert (valid["n_agent_turns"] == 2).all() and (valid["n_user_turns"] == 2).all()
    assert valid["n_tool_calls"].tolist() == [1, 1, 1, 1, 1, 2]  # ("x", 1) has an extra call
    assert (valid["n_messages"] == 5).all()
    assert int(df["is_infra_error"].sum()) == 1


def test_synthetic_failed_actions(synthetic_results: Path) -> None:
    df = load_runs(synthetic_results).set_index(["task_id", "trial"])
    assert df.loc[("10", 0), "failed_write_actions"] == ("cancel_pending_order",)
    assert df.loc[("10", 0), "first_failed_action"] == "cancel_pending_order"
    # reward 0 but every expected action matched -> nothing to blame at action level
    assert df.loc[("x", 0), "failed_write_actions"] == ()
    assert pd.isna(df.loc[("x", 0), "first_failed_action"])
    assert df.loc[("x", 0), "termination_reason"] == "max_steps"
    # all gold actions matched for ("x", 1) but the agent made an unrequested write
    assert df.loc[("x", 1), "extra_tool_calls"] == ("modify_user_address",)
    assert df.loc[("x", 0), "extra_tool_calls"] == ()
    assert df.loc[("x", 2), "error_stage"] == "evaluation"
    assert df.loc[("x", 2), "error_type"] == "AuthenticationError"


def test_outcomes_by_task_orders_by_trial(synthetic_results: Path) -> None:
    df = load_runs(synthetic_results)
    assert outcomes_by_task(df) == {
        "2": [True, True],
        "10": [False, True],
        "x": [False, False],
    }
    # Pessimistic convention: infra error counted as a failure.
    assert outcomes_by_task(df, exclude_infra=False)["x"] == [False, False, False]


def test_load_multiple_runs(synthetic_results: Path, mini_results: Path) -> None:
    df = load_runs([synthetic_results, mini_results])
    assert len(df) == 7 + 3
    assert set(df["run_name"]) == {"synth_run", "fixtures"}


def test_success_uses_tau2_tolerance(tmp_path: Path) -> None:
    # PREREGISTRATION section 3: y = 1 iff |reward - 1| <= 1e-6 (tau2's is_successful).
    rewards = {"0": 0.9999999, "1": 1.0000005, "2": 1.0, "3": 0.99999, "4": 0.0, "5": None}
    sims = [
        {
            "id": f"s{task}",
            "task_id": task,
            "trial": 0,
            "seed": 1,
            "termination_reason": "user_stop",
            "reward_info": {"reward": reward, "db_check": {"db_match": True}},
            "messages": [],
        }
        for task, reward in rewards.items()
    ]
    run = tmp_path / "tol_run"
    run.mkdir()
    (run / "results.json").write_text(
        json.dumps({"info": {}, "simulations": sims}), encoding="utf-8"
    )
    df = load_runs(run).set_index("task_id")
    got = {t: bool(df.loc[t, "success"]) for t in rewards}
    assert got == {"0": True, "1": True, "2": True, "3": False, "4": False, "5": False}
    assert not df["is_infra_error"].any()  # a null reward is a failure, not missing
    assert is_successful_reward(1 - 1e-6) and not is_successful_reward(float("nan"))


def test_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_runs(tmp_path / "nope.json")
    bad = tmp_path / "results.json"
    bad.write_text('{"info": {}, "simulations": [', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_runs(bad)


# ----------------------------------------------------------------- duplicated trials


def _with_resumed_duplicates(src: Path, dst_dir: Path) -> Path:
    """Copy of the synthetic run with task "2" re-run under a different seed.

    This is what a tau2 ``--auto-resume`` after a seed change appends: the same
    (task, trial) pairs again, now with seed 12345.
    """
    data = json.loads(src.read_text(encoding="utf-8"))
    extra = [dict(s, seed=12345, id=s["id"] + "-dup") for s in data["simulations"]]
    data["simulations"] += [s for s in extra if s["task_id"] == "2"]
    dst_dir.mkdir()
    out = dst_dir / "results.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def test_duplicated_trials_are_refused_by_default(synthetic_results: Path, tmp_path: Path) -> None:
    path = _with_resumed_duplicates(synthetic_results, tmp_path / "dup_run")
    with pytest.raises(DuplicateTrialError, match=r"\('2', 0\), \('2', 1\)"):
        load_runs(path)
    with pytest.warns(DuplicateTrialWarning, match="more than one seed"):
        df = load_runs(path, on_duplicates="warn")
    # Silently kept, the duplicates would count as extra trials of task "2".
    assert outcomes_by_task(df)["2"] == [True, True, True, True]
    problems = trial_integrity_problems(df)
    assert len(problems) == 2
    assert "2 (task_id, trial) pair(s)" in problems[0]
    assert "trial index(es) [0, 1]" in problems[1]
    assert len(load_runs(path, on_duplicates="ignore")) == 9
    with pytest.raises(ValueError, match="on_duplicates"):
        load_runs(path, on_duplicates="skip")  # type: ignore[arg-type]


def test_clean_files_have_no_integrity_problems(
    synthetic_results: Path, mini_results: Path
) -> None:
    # Two different files may reuse (task, trial) pairs: checks are per results file.
    assert trial_integrity_problems(load_runs([synthetic_results, mini_results])) == []
