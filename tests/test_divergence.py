"""Tests for whose_noise.divergence (first-divergence attribution, H1c).

Expected labels are written out by hand next to each synthetic conversation. The
fixture-based expectations were checked by printing both message lists up to the
first differing index (every earlier message is equal; the message at the index is a
differently worded customer turn).
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from whose_noise import divergence as dv
from whose_noise.tau2_results import DuplicateTrialError, load_runs
from whose_noise.variance import UnbalancedDesignWarning, task_bootstrap_indices

FIXTURES = Path(__file__).parent / "fixtures"
FID_GPTOSS = FIXTURES / "fidelity" / "fid_rq1_gptoss120b"

# ---------------------------------------------------------------------------
# Synthetic conversations
# ---------------------------------------------------------------------------


def _m(role: str, content: str | None = "", calls: list[dict[str, Any]] | None = None) -> dict:
    """A tau2-shaped message with the metadata that normalisation must ignore."""
    return {
        "role": role,
        "content": content,
        "tool_calls": calls,
        "turn_idx": 0,
        "timestamp": "2026-10-12T20:00:00",
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        "cost": 0.0,
        "raw_data": {"provider": "x"},
        "generation_time_seconds": 1.5,
    }


def _call(name: str, arguments: Any, call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": arguments, "requestor": "assistant"}


BASE = [
    _m("assistant", "Hi! How can I help you today?"),  # 0
    _m("user", "I want to cancel order #1."),  # 1
    _m("assistant", "", [_call("get_order_details", {"order_id": "#1"})]),  # 2
    _m("tool", '{"status": "pending"}'),  # 3
    _m("assistant", "Order #1 is pending. Shall I cancel it?"),  # 4
    _m("user", "Yes, please. ###STOP###"),  # 5
]


def _variant(index: int, **changes: Any) -> list[dict[str, Any]]:
    msgs = copy.deepcopy(BASE)
    msgs[index].update(changes)
    return msgs


def _metadata_only() -> list[dict[str, Any]]:
    """BASE with different ids/timestamps/usage, padded content and string arguments."""
    msgs = copy.deepcopy(BASE)
    for i, msg in enumerate(msgs):
        msg.update(
            timestamp=f"2026-10-13T0{i}:00:00",
            usage={"prompt_tokens": 99, "completion_tokens": i},
            generation_time_seconds=None,
            cost=1.0,
            raw_data=None,
            turn_idx=i + 7,
        )
        if isinstance(msg["content"], str):
            msg["content"] = f"  {msg['content']}\n"
    msgs[2]["tool_calls"] = [_call("get_order_details", '{"order_id": "#1"}', call_id="other")]
    msgs[2]["content"] = None
    return msgs


USER_DIV = _variant(1, content="I'd like to cancel my order #1.")
AGENT_DIV = _variant(4, content="Your order #1 is pending. Cancel it?")
ARGS_DIV = _variant(2, tool_calls=[_call("get_order_details", {"order_id": "#2"})])
TOOL_DIV = _variant(3, content='{"status": "delivered"}')
SHORT = copy.deepcopy(BASE[:4])
INFRA = None  # marker for an infrastructure error


# ---------------------------------------------------------------------------
# normalize / canonical_arguments / diff_fields
# ---------------------------------------------------------------------------


def test_canonical_arguments_sorts_keys_and_keeps_unicode() -> None:
    assert dv.canonical_arguments({"b": 1, "a": "é"}) == '{"a": "é", "b": 1}'
    # A JSON string is parsed first; escaped and literal unicode agree.
    assert dv.canonical_arguments('{"b": 1, "a": "\\u00e9"}') == '{"a": "é", "b": 1}'
    # Nested dicts are sorted at every level; list order is kept.
    assert dv.canonical_arguments({"x": {"b": 1, "a": [2, 1]}}) == '{"x": {"a": [2, 1], "b": 1}}'
    # A string that is not JSON is returned as is.
    assert dv.canonical_arguments("not json {") == "not json {"
    assert dv.canonical_arguments(None) == "null"


def test_normalize_ignores_metadata_and_outer_whitespace() -> None:
    for a, b in zip(BASE, _metadata_only(), strict=True):
        assert dv.normalize(a) == dv.normalize(b)
    assert dv.normalize(BASE[1]) == ("user", "I want to cancel order #1.", ())
    assert dv.normalize(BASE[2]) == (
        "assistant",
        "",
        (("get_order_details", '{"order_id": "#1"}'),),
    )


def test_normalize_argument_key_order_and_string_vs_dict() -> None:
    as_dict = _m("assistant", "", [_call("f", {"a": 1, "b": {"y": 2, "x": 3}})])
    reordered = _m("assistant", "", [_call("f", {"b": {"x": 3, "y": 2}, "a": 1})])
    as_string = _m("assistant", "", [_call("f", '{"b": {"x": 3, "y": 2}, "a": 1}')])
    assert dv.normalize(as_dict) == dv.normalize(reordered) == dv.normalize(as_string)
    # A different value, call order or name is a real difference.
    other_value = _m("assistant", "", [_call("f", {"a": 2, "b": {"y": 2, "x": 3}})])
    assert dv.normalize(as_dict) != dv.normalize(other_value)
    two = [_call("f", {}), _call("g", {})]
    assert dv.normalize(_m("assistant", "", two)) != dv.normalize(_m("assistant", "", two[::-1]))
    assert dv.normalize(_m("assistant", "", [_call("g", {})])) != dv.normalize(
        _m("assistant", "", [_call("f", {})])
    )
    # A list inside the arguments keeps its order.
    assert dv.normalize(_m("assistant", "", [_call("f", {"ids": [1, 2]})])) != dv.normalize(
        _m("assistant", "", [_call("f", {"ids": [2, 1]})])
    )


def test_normalize_whitespace_none_and_empty() -> None:
    assert dv.normalize(_m("user", "  hi\n\t")) == dv.normalize(_m("user", "hi"))
    # Inner whitespace is generated text and counts.
    assert dv.normalize(_m("user", "hi there")) != dv.normalize(_m("user", "hi  there"))
    assert dv.normalize(_m("user", "a\nb")) != dv.normalize(_m("user", "a b"))
    # None and "" content, None and [] tool calls are the same.
    assert dv.normalize(_m("assistant", None, None)) == dv.normalize(_m("assistant", "", []))
    # Different roles never compare equal.
    assert dv.normalize(_m("user", "x")) != dv.normalize(_m("assistant", "x"))


def test_normalize_openai_function_shape() -> None:
    flat = _m("assistant", "", [_call("f", {"a": 1})])
    nested = _m("assistant", "", [{"id": "z", "function": {"name": "f", "arguments": '{"a":1}'}}])
    assert dv.normalize(flat) == dv.normalize(nested)


def test_diff_fields() -> None:
    assert dv.diff_fields(BASE[1], USER_DIV[1]) == "content"
    assert dv.diff_fields(BASE[2], ARGS_DIV[2]) == "tool_calls"
    both = _m("assistant", "Let me check.", [_call("get_order_details", {"order_id": "#2"})])
    assert dv.diff_fields(BASE[2], both) == "content+tool_calls"
    assert dv.diff_fields(_m("user", "x"), _m("assistant", "x")) == "role"
    assert dv.diff_fields(BASE[2], _metadata_only()[2]) is None


# ---------------------------------------------------------------------------
# first_divergence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (BASE, (None, "identical")),
        (_metadata_only(), (None, "identical")),
        (USER_DIV, (1, "user")),
        (AGENT_DIV, (4, "assistant")),
        (ARGS_DIV, (2, "assistant")),  # same text, different tool-call arguments
        (TOOL_DIV, (3, "tool")),  # identical tool call, different result
        (SHORT, (4, "length")),
        (BASE + [_m("assistant", "Anything else?")], (6, "length")),
        (_variant(5, role="system"), (5, "user")),  # label is the role in msgs_a
    ],
)
def test_first_divergence_labels(other: list[dict[str, Any]], expected: tuple) -> None:
    assert dv.first_divergence(BASE, other) == expected


def test_first_divergence_symmetry_and_edge_cases() -> None:
    assert dv.first_divergence(SHORT, BASE) == (4, "length")
    assert dv.first_divergence([], []) == (None, "identical")
    assert dv.first_divergence([], BASE) == (0, "length")
    # An unexpected role in msgs_a becomes "other" rather than an error.
    assert dv.first_divergence(_variant(5, role="system"), BASE) == (5, "other")
    # Only the FIRST divergence counts: a later difference does not move the index.
    both = _variant(1, content="Hello, cancel #1 please.")
    both[4]["content"] = "Something else entirely."
    assert dv.first_divergence(BASE, both) == (1, "user")


def test_first_divergence_on_real_fixture_messages(mini_results: Path) -> None:
    data = json.loads(mini_results.read_text(encoding="utf-8"))
    msgs = data["simulations"][0]["messages"]
    assert len(msgs) == 26
    assert dv.first_divergence(msgs, copy.deepcopy(msgs)) == (None, "identical")
    # Re-serialise every argument dict as a JSON string with reversed key order, drop
    # the metadata and pad the content: still identical.
    noisy = copy.deepcopy(msgs)
    for msg in noisy:
        msg.update(timestamp=None, usage=None, raw_data=None, cost=None, turn_idx=-1)
        if isinstance(msg.get("content"), str):
            msg["content"] = " " + msg["content"] + " \n"
        for call in msg.get("tool_calls") or []:
            call["id"] = "changed"
            call["arguments"] = json.dumps(dict(reversed(list(call["arguments"].items()))))
    assert any(m.get("tool_calls") for m in noisy)
    assert dv.first_divergence(msgs, noisy) == (None, "identical")
    assert dv.first_divergence(msgs, msgs[:-3]) == (23, "length")
    first_user = next(i for i, m in enumerate(msgs) if m["role"] == "user")
    edited = copy.deepcopy(msgs)
    edited[first_user]["content"] += " (edited)"
    assert dv.first_divergence(msgs, edited) == (first_user, "user")


# ---------------------------------------------------------------------------
# Synthetic runs on disk
# ---------------------------------------------------------------------------


def _write_run(
    root: Path,
    run_name: str,
    user_llm: str,
    cells: dict[str, list[tuple[list[dict[str, Any]] | None, float | None]]],
) -> Path:
    """Write a results.json; ``cells[task_id][trial] = (messages, reward)``; None = infra."""
    sims = []
    for task_id, trials in cells.items():
        for trial, (messages, reward) in enumerate(trials):
            infra = messages is None
            sims.append(
                {
                    "id": f"{run_name}-{task_id}-{trial}",
                    "task_id": task_id,
                    "trial": trial,
                    "seed": 100 + trial,
                    "duration": 1.0,
                    "termination_reason": "infrastructure_error" if infra else "user_stop",
                    "reward_info": None
                    if infra
                    else {"reward": reward, "db_check": {"db_match": reward == 1.0}},
                    "messages": [] if infra else messages,
                }
            )
    data = {
        "timestamp": "t",
        "info": {
            "user_info": {"llm": user_llm},
            "agent_info": {"llm": "agent"},
            "environment_info": {"domain_name": "retail"},
        },
        "simulations": sims,
    }
    run_dir = root / run_name
    run_dir.mkdir()
    path = run_dir / "results.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def two_sim_runs(tmp_path: Path) -> list[Path]:
    """2 simulators x 4 tasks x 3 trials with hand-designed first divergences.

    simA  task 1: BASE(1), metadata-only(1), USER_DIV(0)
          task 2: BASE(1), AGENT_DIV(0), ARGS_DIV(0)
          task 3: BASE x3 (1, 1, 1)
          task 4: BASE, BASE, USER_DIV          <- simB never ran task 4
    simB  task 1: BASE(1), SHORT(1), USER_DIV(1)
          task 2: BASE x3 (0, 0, 0)
          task 3: BASE(1), infra error, USER_DIV(1)  <- 2 valid repeats only
    (reward in brackets). Tasks 3 and 4 are incomplete, so the pre-registered rule
    keeps tasks 1 and 2 only.
    """
    a = _write_run(
        tmp_path,
        "runA",
        "simA",
        {
            "1": [(BASE, 1.0), (_metadata_only(), 1.0), (USER_DIV, 0.0)],
            "2": [(BASE, 1.0), (AGENT_DIV, 0.0), (ARGS_DIV, 0.0)],
            "3": [(BASE, 1.0), (BASE, 1.0), (BASE, 1.0)],
            "4": [(BASE, 1.0), (BASE, 1.0), (USER_DIV, 1.0)],
        },
    )
    b = _write_run(
        tmp_path,
        "runB",
        "simB",
        {
            "1": [(BASE, 1.0), (SHORT, 1.0), (USER_DIV, 1.0)],
            "2": [(BASE, 0.0), (BASE, 0.0), (BASE, 0.0)],
            "3": [(BASE, 1.0), (INFRA, None), (USER_DIV, 1.0)],
        },
    )
    return [a, b]


EXPECTED_DROP = [
    # simulator, task, trial_i, trial_j, index, label, verdict_differs
    ("simA", "1", 0, 1, None, "identical", False),
    ("simA", "1", 0, 2, 1, "user", True),
    ("simA", "1", 1, 2, 1, "user", True),
    ("simA", "2", 0, 1, 4, "assistant", True),
    ("simA", "2", 0, 2, 2, "assistant", True),
    ("simA", "2", 1, 2, 2, "assistant", False),
    ("simB", "1", 0, 1, 4, "length", False),
    ("simB", "1", 0, 2, 1, "user", False),
    ("simB", "1", 1, 2, 1, "user", False),
    ("simB", "2", 0, 1, None, "identical", False),
    ("simB", "2", 0, 2, None, "identical", False),
    ("simB", "2", 1, 2, None, "identical", False),
]


def _as_tuples(pairs: pd.DataFrame) -> list[tuple]:
    return [
        (
            r["simulator"],
            r["task"],
            r["trial_i"],
            r["trial_j"],
            None if pd.isna(r["index"]) else int(r["index"]),
            r["label"],
            bool(r["verdict_differs"]),
        )
        for r in pairs.to_dict("records")
    ]


def test_load_messages_keys_and_duplicates(two_sim_runs: list[Path], tmp_path: Path) -> None:
    msgs = dv.load_messages(two_sim_runs)
    assert len(msgs) == 12 + 9
    assert msgs[("runA", "2", 1)][4]["content"] == AGENT_DIV[4]["content"]
    assert msgs[("runB", "3", 1)] == []  # infra error: no conversation
    assert dv.load_messages(two_sim_runs[0].parent) == {
        k: v for k, v in msgs.items() if k[0] == "runA"
    }
    # Two results files in directories with the same name would collide.
    other = tmp_path / "elsewhere"
    other.mkdir()
    clash = _write_run(other, "runA", "simA", {"1": [(BASE, 1.0)]})
    with pytest.raises(DuplicateTrialError, match="more than once"):
        dv.load_messages([two_sim_runs[0], clash])


def test_task_completeness_lists_incomplete_tasks(two_sim_runs: list[Path]) -> None:
    comp = dv.task_completeness(load_runs(two_sim_runs))
    assert list(comp.columns) == list(dv.COMPLETENESS_COLUMNS)
    got = {(r["simulator"], r["task"]): r for r in comp.to_dict("records")}
    assert len(got) == 2 * 4  # task 4 appears for simB with zero runs
    assert (got[("simB", "4")]["n_runs"], got[("simB", "4")]["n_valid"]) == (0, 0)
    assert (got[("simB", "3")]["n_infra"], got[("simB", "3")]["n_valid"]) == (1, 2)
    assert set(comp["target_reps"]) == {3}
    complete = comp.groupby("task")["task_complete"].first().to_dict()
    assert complete == {"1": True, "2": True, "3": False, "4": False}
    assert comp["cell_complete"].sum() == 6  # simA 1-4 and simB 1-2
    # An explicit lower target makes task 3 complete; task 4 stays incomplete.
    comp2 = dv.task_completeness(load_runs(two_sim_runs), n_reps=2)
    assert bool(comp2.groupby("task")["task_complete"].first()["3"])


def test_divergence_pairs_drop_rule(two_sim_runs: list[Path]) -> None:
    runs = load_runs(two_sim_runs)
    with pytest.warns(UnbalancedDesignWarning, match=r"2 of 4 task\(s\)") as rec:
        pairs = dv.divergence_pairs(runs, sim_col="user_llm")
        # Messages passed explicitly give the same frame.
        explicit = dv.divergence_pairs(runs, dv.load_messages(two_sim_runs))
    pd.testing.assert_frame_equal(pairs, explicit)
    message = str(rec[0].message)
    assert "3 (simA=3, simB=2)" in message and "4 (simA=3, simB=0)" in message
    assert list(pairs.columns) == list(dv.PAIR_COLUMNS)
    assert _as_tuples(pairs) == EXPECTED_DROP
    assert str(pairs["index"].dtype) == "Int64"
    assert set(pairs["run_name"]) == {"runA", "runB"}
    assert pairs["condition"].isna().all()
    by_key = pairs.set_index(["simulator", "task", "trial_i", "trial_j"])
    assert by_key.loc[("simA", "2", 0, 2), "diff_fields"] == "tool_calls"
    assert by_key.loc[("simA", "2", 0, 1), "diff_fields"] == "content"
    assert pd.isna(by_key.loc[("simB", "1", 0, 1), "diff_fields"])  # length
    assert by_key.loc[("simB", "1", 0, 1), ["n_messages_i", "n_messages_j"]].tolist() == [6, 4]


def test_divergence_pairs_keep_mode_never_pairs_infra_errors(two_sim_runs: list[Path]) -> None:
    runs = load_runs(two_sim_runs)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # keep mode drops nothing, so it must not warn
        pairs = dv.divergence_pairs(runs, on_incomplete="keep")
    assert len(pairs) == 12 + 3 + 3 + 1  # tasks 1-2, simA tasks 3-4, simB task 3
    sim_b_3 = pairs[(pairs["simulator"] == "simB") & (pairs["task"] == "3")]
    assert _as_tuples(sim_b_3) == [("simB", "3", 0, 2, 1, "user", False)]
    assert 1 not in set(sim_b_3["trial_i"]) | set(sim_b_3["trial_j"])


def test_divergence_pairs_truncates_extra_repeats(tmp_path: Path) -> None:
    path = _write_run(
        tmp_path,
        "runX",
        "simX",
        {
            "1": [(BASE, 1.0), (BASE, 1.0), (BASE, 1.0), (USER_DIV, 0.0)],
            "2": [(BASE, 1.0), (AGENT_DIV, 1.0), (BASE, 1.0)],
        },
    )
    pairs = dv.divergence_pairs(load_runs(path), n_reps=3)
    assert len(pairs) == 6
    assert pairs["trial_j"].max() == 2  # trial 3 of task 1 is cut (first 3 by trial)
    # Without a target, the largest cell (4 repeats) sets it and task 2 is dropped.
    with pytest.warns(UnbalancedDesignWarning, match="fewer than 4 valid repeats"):
        auto = dv.divergence_pairs(load_runs(path))
    assert set(auto["task"]) == {"1"} and len(auto) == 6


def test_divergence_pairs_condition_and_stripped_task_ids(tmp_path: Path) -> None:
    en = _write_run(
        tmp_path, "arm_en", "sim", {"1_en": [(BASE, 1.0), (USER_DIV, 1.0)], "2_en": [(BASE, 1.0)]}
    )
    hi = _write_run(
        tmp_path,
        "arm_hi",
        "sim",
        {"1_hi": [(BASE, 1.0), (AGENT_DIV, 0.0)], "2_hi": [(BASE, 0.0), (BASE, 0.0)]},
    )
    runs = load_runs([en, hi])
    runs["task_key"] = runs["task_id"].str.split("_").str[0]
    # Without a condition column one (task, simulator) cell would mix both runs.
    with pytest.raises(ValueError, match="more than one run"):
        dv.divergence_pairs(runs, task_col="task_key")
    with pytest.warns(UnbalancedDesignWarning, match=r"2 \(sim/arm_en=1, sim/arm_hi=2\)"):
        pairs = dv.divergence_pairs(runs, task_col="task_key", condition_col="run_name")
    assert pairs[["task", "task_id", "condition", "label"]].values.tolist() == [
        ["1", "1_en", "arm_en", "user"],
        ["1", "1_hi", "arm_hi", "assistant"],
    ]


def test_divergence_pairs_carry_and_errors(two_sim_runs: list[Path]) -> None:
    runs = load_runs(two_sim_runs)
    runs["period"] = np.where(runs["trial"] == 2, "after", "before")
    pairs = dv.divergence_pairs(runs, on_incomplete="keep", carry=["period"])
    assert list(pairs.columns[-2:]) == ["period_i", "period_j"]
    row = pairs[(pairs["trial_i"] == 1) & (pairs["trial_j"] == 2)].iloc[0]
    assert (row["period_i"], row["period_j"]) == ("before", "after")
    with pytest.raises(KeyError, match="no messages"):
        dv.divergence_pairs(runs, {}, on_incomplete="keep")
    with pytest.raises(KeyError, match="missing columns"):
        dv.divergence_pairs(runs, sim_col="nope")
    with pytest.raises(ValueError, match="on_incomplete"):
        dv.divergence_pairs(runs, on_incomplete="skip")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="n_reps"):
        dv.divergence_pairs(runs, n_reps=1)


def test_tool_divergence_and_grader_mismatch_are_flagged(tmp_path: Path) -> None:
    path = _write_run(
        tmp_path,
        "runT",
        "simT",
        {
            "1": [(BASE, 1.0), (TOOL_DIV, 1.0)],
            "2": [(BASE, 1.0), (_metadata_only(), 0.0)],
        },
    )
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        pairs = dv.divergence_pairs(load_runs(path))
    categories = [w.category for w in rec]
    assert dv.ToolDivergenceWarning in categories
    assert dv.IdenticalVerdictMismatchWarning in categories
    tool_msg = next(str(w.message) for w in rec if w.category is dv.ToolDivergenceWarning)
    assert "runT task 1 trials 0/1 at message 3" in tool_msg
    assert dv.tool_divergences(pairs)["task"].tolist() == ["1"]


# ---------------------------------------------------------------------------
# Fixture-based (trimmed real transcripts)
# ---------------------------------------------------------------------------


def test_fixture_pairs_real_gptoss_transcripts() -> None:
    # The fixture holds tasks 12, 17, 18 with two trials each and 9, 27, 32 with one.
    runs = load_runs(FID_GPTOSS)
    with pytest.warns(UnbalancedDesignWarning, match=r"3 of 6 task\(s\).*fewer than 2"):
        pairs = dv.divergence_pairs(runs)
    # Checked by hand: every earlier message is equal; the customer reworded its turn.
    assert _as_tuples(pairs) == [
        ("groq/openai/gpt-oss-120b", "12", 1, 2, 1, "user", False),
        ("groq/openai/gpt-oss-120b", "17", 1, 2, 3, "user", True),
        ("groq/openai/gpt-oss-120b", "18", 0, 1, 11, "user", True),
    ]
    assert set(pairs["diff_fields"]) == {"content"}
    # Task 18 diverges only after three identical tool round trips.
    assert pairs.set_index("task").loc["18", ["n_messages_i", "n_messages_j"]].tolist() == [20, 24]
    comp = dv.task_completeness(runs)
    assert comp.loc[comp["task_complete"], "task"].tolist() == ["12", "17", "18"]
    # With the RQ1 target of 3 repeats nothing in the fixture is complete.
    with pytest.warns(UnbalancedDesignWarning):
        assert dv.divergence_pairs(runs, n_reps=3).empty


# ---------------------------------------------------------------------------
# Summary and bootstrap
# ---------------------------------------------------------------------------


def _drop_pairs(paths: list[Path]) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnbalancedDesignWarning)
        return dv.divergence_pairs(load_runs(paths))


def test_summary_counts_and_shares(two_sim_runs: list[Path]) -> None:
    summ = dv.summarize_divergence(_drop_pairs(two_sim_runs), n_boot=200).set_index(
        ["subset", "group"]
    )
    # simA: identical 1, user 2, assistant 3 -> 5 diverging
    a = summ.loc[("all", "simA")]
    assert (a["n_pairs"], a["n_identical"], a["n_diverging"]) == (6, 1, 5)
    assert (a["n_user"], a["n_assistant"], a["n_tool"], a["n_length"]) == (2, 3, 0, 0)
    assert a["share_user"] == pytest.approx(2 / 5) and a["share_agent"] == pytest.approx(3 / 5)
    # simB: identical 3, user 2, length 1 -> 3 diverging
    b = summ.loc[("all", "simB")]
    assert (b["n_diverging"], b["n_user"], b["n_length"]) == (3, 2, 1)
    assert b["share_user"] == pytest.approx(2 / 3) and b["share_length"] == pytest.approx(1 / 3)
    # pooled: user 4, assistant 3, length 1 of 8 diverging
    p = summ.loc[("all", "pooled")]
    assert (p["n_tasks"], p["n_pairs"], p["n_diverging"]) == (2, 12, 8)
    assert p["share_user"] == pytest.approx(0.5)
    assert p["share_agent"] == pytest.approx(3 / 8)
    assert p["share_tool"] == 0.0
    # Different verdicts: simA user 2 + assistant 2; simB none.
    d = summ.loc[("verdict_differs", "simA")]
    assert (d["n_pairs"], d["n_user"], d["n_assistant"], d["n_tasks"]) == (4, 2, 2, 2)
    assert d["share_user"] == pytest.approx(0.5)
    e = summ.loc[("verdict_differs", "simB")]
    assert e["n_pairs"] == 0 and np.isnan(e["share_user"]) and np.isnan(e["share_user_ci_low"])
    assert e["n_boot_undefined"] == 200
    assert set(summ["n_boot"]) == {200} and set(summ["seed"]) == {20261011}


def test_summary_bootstrap_matches_a_brute_force_loop(two_sim_runs: list[Path]) -> None:
    pairs = _drop_pairs(two_sim_runs)
    n_boot, seed = 300, 7
    summ = dv.summarize_divergence(pairs, n_boot=n_boot, seed=seed).set_index(["subset", "group"])
    tasks = ["1", "2"]  # natural order of the clusters
    idx = task_bootstrap_indices(len(tasks), n_boot, seed)
    draws = []
    for row in idx:
        sample = pd.concat([pairs[pairs["task"] == tasks[k]] for k in row])
        sample = sample[sample["simulator"] == "simA"]
        diverging = sample[sample["label"] != "identical"]
        draws.append((diverging["label"] == "user").mean() if len(diverging) else np.nan)
    draws = np.array(draws)
    low, high = np.quantile(draws[~np.isnan(draws)], [0.025, 0.975])
    assert summ.loc[("all", "simA"), "share_user_ci_low"] == pytest.approx(low)
    assert summ.loc[("all", "simA"), "share_user_ci_high"] == pytest.approx(high)


def _pairs_frame(rows: list[tuple[str, str, str, bool]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["task", "simulator", "label", "verdict_differs"])


def test_summary_degenerate_interval_when_every_task_is_alike() -> None:
    rows = [(str(t), "s", lab, False) for t in range(5) for lab in ("user", "assistant")]
    summ = dv.summarize_divergence(_pairs_frame(rows), n_boot=500)
    pooled = summ[(summ["subset"] == "all") & (summ["group"] == "pooled")].iloc[0]
    assert (pooled["share_user_ci_low"], pooled["share_user_ci_high"]) == (0.5, 0.5)
    assert pooled["share_user"] == 0.5


def test_summary_bootstrap_is_deterministic_and_seeded() -> None:
    labels = ["user", "assistant", "identical", "length", "user", "user", "assistant"]
    rows = [
        (str(t), sim, labels[(t + k) % len(labels)], (t + k) % 2 == 0)
        for t in range(8)
        for sim in ("s1", "s2")
        for k in range(3)
    ]
    pairs = _pairs_frame(rows)
    first = dv.summarize_divergence(pairs, n_boot=1000)
    again = dv.summarize_divergence(pairs, n_boot=1000)
    pd.testing.assert_frame_equal(first, again)
    other = dv.summarize_divergence(pairs, n_boot=1000, seed=1)
    ci_cols = [c for c in first.columns if c.endswith(("_ci_low", "_ci_high"))]
    assert not first[ci_cols].equals(other[ci_cols])
    # Point estimates do not depend on the seed.
    point = ["share_user", "share_agent", "share_tool", "share_length"]
    pd.testing.assert_frame_equal(first[point], other[point])
    # Intervals are ordered and stay inside [0, 1].
    ok = first.dropna(subset=["share_user"])
    assert ((ok["share_user_ci_low"] >= 0) & (ok["share_user_ci_high"] <= 1)).all()
    assert (ok["share_user_ci_low"] <= ok["share_user_ci_high"]).all()


def test_summary_counts_undefined_draws() -> None:
    # Group "s2" diverges only in task "0"; draws without task 0 define no share.
    rows = [(str(t), "s1", "user", False) for t in range(4)]
    rows += [("0", "s2", "assistant", False)] + [(str(t), "s2", "identical", False) for t in (1, 2)]
    pairs = _pairs_frame(rows)
    n_boot, seed = 400, 3
    summ = dv.summarize_divergence(pairs, n_boot=n_boot, seed=seed)
    s2 = summ[(summ["subset"] == "all") & (summ["group"] == "s2")].iloc[0]
    idx = task_bootstrap_indices(4, n_boot, seed)  # clusters "0".."3"
    assert s2["n_boot_undefined"] == int((idx != 0).all(axis=1).sum()) > 0
    assert (s2["share_agent_ci_low"], s2["share_agent_ci_high"]) == (1.0, 1.0)


def test_summary_rejects_bad_input() -> None:
    good = _pairs_frame([("1", "s", "user", False)])
    with pytest.raises(ValueError, match="no pairs"):
        dv.summarize_divergence(good.iloc[:0])
    with pytest.raises(ValueError, match="unknown labels"):
        dv.summarize_divergence(_pairs_frame([("1", "s", "agent", False)]))
    with pytest.raises(ValueError, match="pooled"):
        dv.summarize_divergence(_pairs_frame([("1", "pooled", "user", False)]))
    with pytest.raises(ValueError, match="n_boot"):
        dv.summarize_divergence(good, n_boot=0)
    with pytest.raises(ValueError, match="alpha"):
        dv.summarize_divergence(good, alpha=1.5)
    with pytest.raises(KeyError, match="missing columns"):
        dv.summarize_divergence(good, group_col="condition")
