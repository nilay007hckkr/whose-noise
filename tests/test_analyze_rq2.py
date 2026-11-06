"""Tests for scripts/analyze_rq2.py on small synthetic tau2 results files (no real RQ2 data).

Expected numbers are worked out by hand from the outcome tables, or by calling the library
function the script must delegate to with the same seed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from whose_noise import metrics, variance

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rq2 = _load("analyze_rq2")

N_BOOT, SEED = 200, 20261011
TASKS = ("5", "9", "12", "17", "18", "26")
ASK = 'The exchange is ready. Please confirm with a "yes" if you\'d like me to proceed.'
COMMIT = "8ff48b8e46575e1519e4d4b75a906770b0e4e268"
INFO = {
    "en_plain": {"task_set_name": "retail", "user_persona_id": None, "text_input_style": None},
    "hi_romanized": {"task_set_name": "retail_hi", "user_persona_id": "hi",
                     "text_input_style": "romanized"},
    "hi_code_mixed": {"task_set_name": "retail_hi", "user_persona_id": "hi",
                      "text_input_style": "code_mixed"},
}  # fmt: skip
# 1 pass, 0 fail, "S" fail with a strict premature stop, "M" fail with a merged turn,
# "D" pass whose customer typed Devanagari, None infra error.
TABLES: dict[str, dict[str, list[Any]]] = {
    "en_plain": {"5": [1, 1], "9": [1, 0], "12": [1, 1], "17": [0, 0], "18": [1, 1], "26": [1, 1]},
    "hi_romanized": {"5": [1, 0], "9": [0, 0], "12": [1, 1], "17": ["S", 0], "18": [1, "M"],
                     "26": [0, "D"]},
    "hi_code_mixed": {"5": [1, 1], "9": [1, 1], "12": [1, 1], "17": [0, 1], "18": [1, 1],
                      "26": [1, 1]},
}  # fmt: skip


def _msg(role: str, text: str | None, *calls: str) -> dict[str, Any]:
    tool_calls = [{"id": f"c{i}", "name": n, "arguments": {}} for i, n in enumerate(calls)]
    return {"role": role, "content": text, "tool_calls": tool_calls or None}


def _messages(arm: str, code: Any) -> list[dict[str, Any]]:
    opening_text = (
        "I want to exchange an item from order #W2378156."
        if arm == "en_plain"
        else "Mujhe order #W2378156 ka ek item exchange karna hai."
    )
    if code == "D":
        opening_text = "मुझे ऑर्डर #W2378156 का एक आइटम बदलना है।"
    opening = [
        _msg("assistant", "Hi! How can I help you today?"),
        _msg("user", opening_text),
        _msg("assistant", None, "get_order_details"),
        {"role": "tool", "content": '{"order_id": "#W2378156"}', "tool_calls": None},
    ]
    if code == "S":
        return [*opening, _msg("assistant", ASK), _msg("user", "Yes.###STOP###")]
    last = "Email handy nahi hai.Okay, thanks. ###STOP###" if code == "M" else "Thanks. ###STOP###"
    return [*opening, _msg("assistant", "Done. Anything else?"), _msg("user", last)]


def write_arm(root: Path, arm: str, table: dict[str, list[Any]], suffix: str | None = None) -> Path:
    suffix = suffix or rq2.ARM_HASH[arm]
    sims = []
    for task, codes in table.items():
        tid = task if arm == "en_plain" else f"{task}_hi"
        for trial, code in enumerate(codes):
            base = {"id": f"{tid}-{trial}", "task_id": tid, "trial": trial, "seed": 1000 + trial,
                    "start_time": "2026-11-22T02:10:00", "duration": 1.0}  # fmt: skip
            if code is None:
                sims.append({**base, "termination_reason": "infrastructure_error",
                             "reward_info": None, "messages": [],
                             "info": {"error_type": "RateLimitError"}})  # fmt: skip
                continue
            reward = 1.0 if code in (1, "D") else 0.0
            sims.append({**base, "termination_reason": "user_stop",
                         "messages": _messages(arm, code),
                         "reward_info": {"reward": reward, "db_check": {"db_match": bool(reward)},
                                         "action_checks": []}})  # fmt: skip
    data = {
        "info": {
            "git_commit": COMMIT, "num_trials": 2, "seed": 300, "timeout": 2400.0,
            "target_language_directive_version": "v3", "agent_caller_locale_context": True,
            **INFO[arm],
            "user_info": {"llm": "groq/openai/gpt-oss-120b", "llm_args": {"temperature": 0.0}},
            "agent_info": {"llm": "openai/qwen3-4b", "llm_args": {"temperature": 0.0}},
            "environment_info": {"domain_name": "retail"},
        },
        "tasks": [],
        "simulations": sims,
    }  # fmt: skip
    run_dir = root / f"rq2a_qwen4b_gptoss120b_{arm.replace('_', '-')}_t2_s300_{suffix}"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return run_dir / "results.json"


def write_all(root: Path, tables: dict[str, dict[str, list[Any]]] = TABLES) -> dict[str, Path]:
    return {arm: write_arm(root, arm, tables[arm]) for arm in rq2.ARMS}


def run_cli(paths: dict[str, Path], out_dir: Path, *extra: str) -> int:
    argv = [a for arm in rq2.ARMS for a in (rq2.flag(arm), str(paths[arm]))]
    return rq2.main([*argv, "--out-dir", str(out_dir), "--n-boot", str(N_BOOT),
                     "--analysis-timestamp", "2026-12-04T12:00:00+05:30", *extra])  # fmt: skip


def read_result(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / "results.json").read_text(encoding="utf-8"))


def succ(arm: str) -> dict[str, list[bool]]:
    return {t: [c in (1, "D") for c in v if c is not None] for t, v in TABLES[arm].items()}


@pytest.fixture(autouse=True)
def six_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rq2, "TASKS", TASKS)


def test_contrasts_and_holm_by_hand(tmp_path: Path) -> None:
    assert run_cli(write_all(tmp_path), tmp_path / "out") == 0
    res = read_result(tmp_path / "out")
    assert res["section8"]["mode"] == "final" and res["protocol_warnings"] == []
    assert res["label"] == "PROVISIONAL"  # B = 200, not 10,000
    hyp = res["hypotheses"]
    assert res["pass_k"]["en_plain"]["pass^1"]["estimate"] == pytest.approx(4.5 / 6)
    assert hyp["H2"]["estimate"] == pytest.approx(-2 / 6)
    assert hyp["H2b"]["estimate"] == pytest.approx(1 / 6)
    assert hyp["H2e"]["estimate"] == pytest.approx(3 / 6)
    # 95% CIs equal metrics.paired_bootstrap_diff with the same seed
    for h, (a, b) in rq2.CONTRASTS.items():
        _, lo, hi = metrics.paired_bootstrap_diff(succ(a), succ(b), k=1, n_boot=N_BOOT, seed=SEED)
        assert (hyp[h]["ci_low"], hyp[h]["ci_high"]) == pytest.approx((lo, hi))
    # Holm redone independently from the same draws
    draws, p = {}, {}
    for h, (a, b) in rq2.CONTRASTS.items():
        sa, sb = succ(a), succ(b)
        d = np.array([np.mean(sa[t]) - np.mean(sb[t]) for t in TASKS])
        idx = np.random.default_rng(SEED).integers(0, d.size, size=(N_BOOT, d.size))
        draws[h] = d[idx].mean(axis=1)
        p[h] = min(1.0, 2 * min((draws[h] <= 0).mean(), (draws[h] >= 0).mean()))
    order = sorted(p, key=lambda h: (p[h], h))
    assert hyp["order"] == order
    going = True
    for i, h in enumerate(order):
        level = 0.05 / (3 - i)
        lo, hi = np.percentile(draws[h], [100 * level / 2, 100 * (1 - level / 2)])
        going = going and (lo > 0 or hi < 0)
        assert hyp[h]["holm_alpha"] == pytest.approx(level)
        assert hyp[h]["differs"] == going
    # sensitivity: pass^2 contrast delegates to the library
    d2 = metrics.paired_bootstrap_diff(succ("hi_romanized"), succ("en_plain"), k=2, n_boot=N_BOOT,
                                       seed=SEED)  # fmt: skip
    assert hyp["H2"]["sensitivity"]["pass^2"]["estimate"] == pytest.approx(d2[0])


def test_noise_floor_decomposition_screens(tmp_path: Path) -> None:
    paths = write_all(tmp_path)
    assert run_cli(paths, tmp_path / "out") == 0
    res = read_result(tmp_path / "out")
    runs, _, _ = rq2.load({a: str(p) for a, p in paths.items()})
    valid = rq2.rq1.analysis_frame(runs, list(TASKS), 2)[0]
    en = valid[valid["role"] == "en_plain"]
    nf = variance.noise_floor(en, n_eval_tasks=6, trials_per_run=2, sim_col=None, n_reps=2,
                              on_unbalanced="raise", n_boot=N_BOOT, seed=SEED)  # fmt: skip
    assert res["noise_floor_en_plain"]["trials_per_run_2"]["band"]["estimate"] == pytest.approx(
        nf.band.estimate
    )
    diff = variance.compare_decompositions(
        en, valid[valid["role"] == "hi_romanized"], design="crossed", n_boot=N_BOOT, seed=SEED,
        sim_col=None, n_reps=2, on_unbalanced="raise",
    ).loc["residual"]  # fmt: skip
    assert res["H2d"]["hi_romanized"]["residual"]["difference"] == pytest.approx(diff["difference"])
    rom = res["H2f"]["hi_romanized"]
    assert rom["premature_stop"]["all"]["count"] == 1 and rom["merged_turns"]["all"]["count"] == 1
    assert rom["script_leakage"]["all"]["count"] == 1 and rom["script_leakage"]["in_passes"] == 1
    assert 0 < rom["devanagari_share_of_customer_letters"] < 0.5
    # 1 of 12 = 8 points from en_plain: under the 10-point rule, so no caveat
    assert res["caveats"] == []
    sens = res["sensitivity_without_flagged"]
    assert sens["excluded_per_arm"] == {"en_plain": 0, "hi_romanized": 2, "hi_code_mixed": 0}
    # romanized without (17, 0) and (18, 1): tasks 17 -> [0], 18 -> [1]; d = -.5,-.5,0,0,0,-.5
    assert sens["H2"]["estimate"] == pytest.approx(-1.5 / 6)


def test_caveats_and_infra_worst_case(tmp_path: Path) -> None:
    tables = {a: dict(t) for a, t in TABLES.items()}
    tables["hi_romanized"]["9"] = ["S", "S"]  # 3 premature stops of 12: 25 points over en_plain
    paths = write_all(tmp_path, tables)
    extra = ["--pre-rerun-infra", "en_plain=0", "hi_romanized=4", "hi_code_mixed=0",
             "--pre-rerun-source", "counted from the run logs"]  # fmt: skip
    assert run_cli(paths, tmp_path / "out", *extra) == 0
    res = read_result(tmp_path / "out")
    assert any("premature_stop" in c and "H2 " in c for c in res["caveats"])
    bound = res["section11_infra"]
    assert bound["rates_used"] == "infra_errors_before_reruns"
    assert bound["hi_romanized"]["triggered"] and not bound["hi_code_mixed"]["triggered"]
    # romanized passes: 5 -> 1, 12 -> 2, 18 -> 1, 26 -> 1 = 5; worst (5 - 4) / 12; en 9 / 12
    assert bound["hi_romanized"]["worst_case_delta"] == pytest.approx(1 / 12 - 9 / 12)
    assert run_cli(paths, tmp_path / "bad", "--pre-rerun-infra", "en_plain=0") == 2

    tables["hi_romanized"] = {t: ["D", "D"] for t in TASKS}  # all Devanagari
    paths = write_all(tmp_path / "leak", tables)
    assert run_cli(paths, tmp_path / "leak_out") == 0
    assert any("Devanagari" in c for c in read_result(tmp_path / "leak_out")["caveats"])


def test_partial_data_is_refused_and_protocol_checked(tmp_path: Path) -> None:
    tables = {a: dict(t) for a, t in TABLES.items()}
    tables["hi_code_mixed"]["26"] = [1, None]
    paths = write_all(tmp_path, tables)
    assert run_cli(paths, tmp_path / "refused") == 2
    assert run_cli(paths, tmp_path / "out", "--allow-partial") == 0
    s8 = read_result(tmp_path / "out")["section8"]
    assert s8["awaiting_rerun_tasks"] == ["26"] and s8["mode"] == "partial"
    assert run_cli(paths, tmp_path / "drop", "--retries-exhausted", "26") == 0
    assert read_result(tmp_path / "drop")["section8"]["dropped_tasks"] == ["26"]

    swapped = write_all(tmp_path / "swap")
    swapped["hi_romanized"], swapped["hi_code_mixed"] = (
        swapped["hi_code_mixed"],
        swapped["hi_romanized"],
    )
    assert run_cli(swapped, tmp_path / "swap_out") == 0
    warnings = read_result(tmp_path / "swap_out")["protocol_warnings"]
    assert any("text_input_style" in w for w in warnings)
    assert any("piloted hash" in w for w in warnings)


def test_outputs_are_deterministic(tmp_path: Path) -> None:
    paths = write_all(tmp_path)
    assert run_cli(paths, tmp_path / "a") == 0 and run_cli(paths, tmp_path / "b") == 0
    assert read_result(tmp_path / "a") == read_result(tmp_path / "b")
    report = (tmp_path / "a" / "report.md").read_text(encoding="utf-8")
    assert "## H2 (primary)" in report and "With 6 tasks (fewer than 20)" in report
