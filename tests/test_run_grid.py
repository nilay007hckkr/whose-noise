"""Tests for the pure functions of scripts/run_grid.py (no tau2 process is launched)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run_grid.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_grid", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_grid"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


rg = _load_module()

AGENT = {
    "name": "qwen4b",
    "llm": "openai/qwen3-4b",
    "llm_args": {"temperature": 0.0, "api_key": "local", "api_base": "http://127.0.0.1:8080/v1"},
}
SIM = {
    "name": "gptoss120b",
    "llm": "groq/openai/gpt-oss-120b",
    "llm_args": {"temperature": 0.0},
    "daily_token_budget": 200000,
    "tokens_per_task_estimate": 9000,
}


def raw_config(**overrides):
    raw = {
        "experiment": "pilot1",
        "agent": dict(AGENT),
        "simulators": [dict(SIM)],
        "tasks": {"ids": ["0", "1", "2"]},
        "num_trials": 3,
    }
    raw.update(overrides)
    return raw


# ---------------------------------------------------------------- command construction


def test_build_tau2_command_exact_argv():
    config = rg.parse_config(raw_config())
    argv = rg.build_tau2_command(config, config.simulators[0], ["0", "1", "2"], "my_run")
    assert argv == [
        "uv", "run", "tau2", "run",
        "--domain", "retail",
        "--agent-llm", "openai/qwen3-4b",
        # keys sorted, no spaces -> independent of config key order
        "--agent-llm-args",
        '{"api_base":"http://127.0.0.1:8080/v1","api_key":"local","temperature":0.0}',
        "--user-llm", "groq/openai/gpt-oss-120b",
        "--user-llm-args", '{"temperature":0.0}',
        "--num-trials", "3",
        "--seed", "300",
        "--task-ids", "0", "1", "2",
        "--max-concurrency", "1",
        "--save-to", "my_run",
        "--auto-resume",
    ]  # fmt: skip


def test_build_tau2_command_appends_extra_args_last():
    config = rg.parse_config(raw_config(extra_tau2_args=["--max-retries", "5"], seed=7))
    argv = rg.build_tau2_command(config, config.simulators[0], ["4"], "r")
    assert argv[-3:] == ["--auto-resume", "--max-retries", "5"]
    assert argv[argv.index("--seed") + 1] == "7"


def test_subprocess_env_forces_utf8_and_keeps_base():
    env = rg.subprocess_env({"PATH": "x", "PYTHONUTF8": "0"})
    assert env == {"PATH": "x", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


# ---------------------------------------------------------------- task-id resolution


@pytest.fixture
def fake_splits(tmp_path):
    splits = {
        "train": [0, 1, 2, 3, 4, 6],
        "test": ["5", "7", "9"],
        "base": [str(i) for i in range(10)],
    }
    path = tmp_path / "data" / "tau2" / "domains" / "retail" / "split_tasks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(splits), encoding="utf-8")
    return tmp_path


def test_resolve_split_from_fake_file(fake_splits):
    splits = rg.load_splits(rg.split_file_path(fake_splits, "retail"))
    assert rg.resolve_task_ids(rg.TaskSource(split="test"), splits) == ["5", "7", "9"]
    # ints in the file become strings; order of the file is kept
    train = rg.resolve_task_ids(rg.TaskSource(split="train"), splits)
    assert train == ["0", "1", "2", "3", "4", "6"]
    assert rg.resolve_task_ids(rg.TaskSource(split="train", limit=2), splits) == ["0", "1"]


def test_resolve_explicit_ids_dedupes_and_validates(fake_splits):
    splits = rg.load_splits(rg.split_file_path(fake_splits, "retail"))
    source = rg.TaskSource(ids=("3", "1", "3", "8"))
    assert rg.resolve_task_ids(source, splits) == ["3", "1", "8"]
    with pytest.raises(rg.ConfigError, match="unknown task ids"):
        rg.resolve_task_ids(rg.TaskSource(ids=("1", "99")), splits)
    with pytest.raises(rg.ConfigError, match="not in split file"):
        rg.resolve_task_ids(rg.TaskSource(split="dev"), splits)


# ---------------------------------------------------------------- chunks


def test_chunk_bounds_and_cumulative_selection():
    assert rg.chunk_bounds(10, 3) == [(0, 4), (4, 7), (7, 10)]
    assert rg.chunk_bounds(2, 3) == [(0, 1), (1, 2), (2, 2)]
    ids = [str(i) for i in range(10)]
    cumulative, new = rg.select_chunk(ids, 2, 3)
    assert cumulative == ["0", "1", "2", "3", "4", "5", "6"]
    assert new == ["4", "5", "6"]
    assert rg.select_chunk(ids, 1, 1) == (ids, ids)


@pytest.mark.parametrize("spec", ["0/3", "4/3", "1/0", "a/b", "2"])
def test_parse_chunk_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        rg.parse_chunk(spec)


def test_parse_chunk_accepts_k_of_n():
    assert rg.parse_chunk("2/5") == (2, 5)
    assert rg.parse_chunk(" 1 / 1 ") == (1, 1)


# ---------------------------------------------------------------- budget


def test_budget_check_pass_and_boundary():
    # 22 sims x 9000 = 198000 <= 200000
    check = rg.check_budget(22, 9000, 200_000)
    assert check.ok and check.planned == 198_000
    # exactly equal still fits
    assert rg.check_budget(20, 10_000, 200_000).ok


def test_budget_check_fail_and_unknown_budget():
    # 40 tasks x 3 trials x 9000 = 1,080,000 > 200,000
    check = rg.check_budget(120, 9000, 200_000)
    assert not check.ok and check.planned == 1_080_000
    assert "EXCEEDS" in check.message
    assert not rg.check_budget(1, 9000, None).ok


def test_suggest_num_chunks():
    # 200000 // (3 * 9000) = 7 tasks/day -> ceil(40 / 7) = 6 chunks
    assert rg.suggest_num_chunks(40, 3, 9000, 200_000) == 6
    # 200000 // (3 * 12000) = 5 tasks/day -> 8 chunks
    assert rg.suggest_num_chunks(40, 3, 12_000, 200_000) == 8
    # one task with 3 trials needs 300k > 200k
    assert rg.suggest_num_chunks(40, 3, 100_000, 200_000) is None


def test_budget_checks_include_requests_only_when_configured():
    config = rg.parse_config(raw_config())
    assert [c.unit for c in rg.budget_checks(config.simulators[0], 5)] == ["tokens"]
    sim = dict(SIM, daily_request_budget=1000, requests_per_task_estimate=20)
    config = rg.parse_config(raw_config(simulators=[sim]))
    checks = rg.budget_checks(config.simulators[0], 60)
    assert [(c.unit, c.planned, c.ok) for c in checks] == [
        ("tokens", 540_000, False),
        ("requests", 1200, False),
    ]


def test_trial_seeds_match_tau2():
    # Seeds stored by tau2 in the real pilot run (seed 300, 3 trials).
    assert rg.trial_seeds(300, 3) == [626729, 373753, 361454]
    # tau2 draws them in order, so more trials only append seeds.
    assert rg.trial_seeds(300, 5)[:3] == rg.trial_seeds(300, 3)


def test_remaining_simulations_skip_done_but_not_infra_errors(tmp_path):
    s0, s1 = rg.trial_seeds(300, 2)
    sims = [
        {"task_id": "0", "trial": 0, "seed": s0, "termination_reason": "user_stop"},
        {"task_id": "0", "trial": 1, "seed": s1, "termination_reason": "max_steps"},
        {"task_id": "1", "trial": 0, "seed": s0, "termination_reason": "infrastructure_error"},
    ]
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"simulations": sims}), encoding="utf-8")
    done = rg.completed_runs(rg.load_simulations(path))
    assert done == {("0", 0, s0), ("0", 1, s1)}
    # plan: tasks 0,1 x 2 trials = 4; 2 done -> 2 left (the infra error is retried)
    assert rg.remaining_simulations(["0", "1"], 2, done, [s0, s1]) == 2
    # tau2 keys on (trial, task, seed): with another base seed nothing counts as done.
    assert rg.remaining_simulations(["0", "1"], 2, done, rg.trial_seeds(999, 2)) == 4
    with pytest.raises(ValueError, match="trial seeds"):
        rg.remaining_simulations(["0"], 3, done, [s0])
    assert rg.load_simulations(tmp_path / "missing" / "results.json") == []


def test_load_simulations_directory_format(tmp_path):
    (tmp_path / "simulations").mkdir()
    (tmp_path / "results.json").write_text(json.dumps({"simulations": []}), encoding="utf-8")
    sim = {"task_id": "2", "trial": 0, "termination_reason": "agent_stop"}
    (tmp_path / "simulations" / "a.json").write_text(json.dumps(sim), encoding="utf-8")
    assert rg.load_simulations(tmp_path / "results.json") == [sim]


def test_count_outcomes():
    sims = [
        {"termination_reason": "user_stop", "reward_info": {"reward": 1.0}},
        {"termination_reason": "user_stop", "reward_info": {"reward": 0.0}},
        {"termination_reason": "infrastructure_error", "reward_info": None},
        {"termination_reason": "max_steps", "reward_info": None},
    ]
    assert rg.count_outcomes(sims) == (4, 1, 1)


# ---------------------------------------------------------------- run names


def test_run_name_reproduces_existing_pilot_run():
    config = rg.parse_config(raw_config(run_name_template="{experiment}_{agent}_{user}"))
    assert rg.make_run_name(config, config.simulators[0]) == "pilot1_qwen4b_gptoss120b"


def test_run_name_is_deterministic_and_task_sensitive():
    a = rg.parse_config(raw_config())
    b = rg.parse_config(raw_config())
    c = rg.parse_config(raw_config(tasks={"ids": ["0", "1", "3"]}))
    name_a = rg.make_run_name(a, a.simulators[0])
    assert name_a == rg.make_run_name(b, b.simulators[0])
    # sha1("0,1,2")[:8] and the config hash are pinned so a code change cannot silently
    # rename existing runs.
    assert name_a == f"pilot1_qwen4b_gptoss120b_ids-9805b47f_t3_s300_c{CFG_HASH}"
    assert name_a != rg.make_run_name(c, c.simulators[0])


def test_run_name_for_split_and_limit():
    config = rg.parse_config(
        raw_config(experiment="RQ1 swap", tasks={"split": "test", "limit": 10})
    )
    sim = rg.parse_config(raw_config(simulators=[dict(SIM, name="Qwen3.8 27B")])).simulators[0]
    assert (
        rg.make_run_name(config, sim)
        == f"rq1-swap_qwen4b_qwen3.8-27b_test-first10_t3_s300_c{CFG_HASH}"
    )


# sha1 of the canonical JSON of AGENT/SIM model settings + domain, written out by hand.
CFG_HASH = hashlib.sha1(
    b'{"agent_llm":"openai/qwen3-4b","agent_llm_args":{"api_base":"http://127.0.0.1:8080/v1",'
    b'"api_key":"local","temperature":0.0},"domain":"retail",'
    b'"user_llm":"groq/openai/gpt-oss-120b","user_llm_args":{"temperature":0.0}}'
).hexdigest()[:6]


def test_config_hash_changes_with_models_and_args():
    base = rg.parse_config(raw_config())
    assert rg.config_hash(base, base.simulators[0]) == CFG_HASH
    hot_agent = dict(AGENT, llm_args=dict(AGENT["llm_args"], temperature=0.7))
    variants = [
        raw_config(agent=hot_agent),
        raw_config(agent=dict(AGENT, llm="openai/qwen3-8b")),
        raw_config(simulators=[dict(SIM, llm="groq/openai/gpt-oss-20b")]),
        raw_config(simulators=[dict(SIM, llm_args={"temperature": 1.0})]),
        raw_config(domain="airline"),
    ]
    hashes = {rg.config_hash(c, c.simulators[0]) for c in map(rg.parse_config, variants)}
    assert len(hashes) == len(variants) and CFG_HASH not in hashes
    # Key order of llm_args does not matter.
    reordered = dict(AGENT, llm_args=dict(reversed(list(AGENT["llm_args"].items()))))
    c = rg.parse_config(raw_config(agent=reordered))
    assert rg.config_hash(c, c.simulators[0]) == CFG_HASH


def test_run_name_rejects_unknown_template_field():
    config = rg.parse_config(raw_config(run_name_template="{experiment}_{model}"))
    with pytest.raises(rg.ConfigError, match="unknown field"):
        rg.make_run_name(config, config.simulators[0])


# ---------------------------------------------------------------- config validation and health


def test_config_rejects_real_looking_keys():
    agent = dict(AGENT, llm_args={"api_key": "gsk_abcdef123456"})
    with pytest.raises(rg.ConfigError, match="secret-looking"):
        rg.parse_config(raw_config(agent=agent))
    sim = dict(SIM, llm_args={"temperature": 0.0, "extra": "sk-proj-abc"})
    assert rg.find_secret_like_values(raw_config(simulators=[sim])) == [
        "$.simulators[0].llm_args.extra"
    ]
    # the llama.cpp dummy key and integer *token* fields are fine
    assert rg.find_secret_like_values(raw_config()) == []


def test_config_requires_fields():
    raw = raw_config()
    del raw["num_trials"]
    with pytest.raises(rg.ConfigError, match="num_trials"):
        rg.parse_config(raw)
    with pytest.raises(rg.ConfigError, match="exactly one"):
        rg.parse_config(raw_config(tasks={"ids": ["0"], "split": "train"}))
    with pytest.raises(rg.ConfigError, match="unique"):
        rg.parse_config(raw_config(simulators=[dict(SIM), dict(SIM)]))


@pytest.mark.parametrize(
    ("config_file", "expected_names"),
    [
        ("pilot.json", ["pilot1_qwen4b_gptoss120b"]),
        (
            "rq1_simulator_swap.json",
            [
                # hashes include the Groq retry args (num_retries / exponential backoff)
                "rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638",
                "rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908",
                "rq1_qwen4b_gemini3.8flash_test-nonl_t3_s300_cf43780",
            ],
        ),
    ],
)
def test_shipped_configs_are_valid(config_file, expected_names):
    config = rg.load_config(REPO_ROOT / "configs" / config_file)
    assert [rg.make_run_name(config, s) for s in config.simulators] == expected_names
    assert config.agent.llm_args["api_key"] == "local"


def test_health_url_and_payload():
    assert rg.health_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/health"
    assert rg.health_url("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080/health"
    assert rg.health_url("http://localhost:9000") == "http://localhost:9000/health"
    assert rg.is_healthy(200, '{"status": "ok"}')
    assert not rg.is_healthy(503, '{"error": {"message": "Loading model"}}')
    assert not rg.is_healthy(200, '{"status": "loading"}')
    assert not rg.is_healthy(200, "not json")


def test_chunk_hint_uses_binding_constraint():
    # 120 sims: tokens need 6 chunks (9000/sim), requests need 3 chunks (20/sim)
    failed = [rg.check_budget(120, 9000, 200_000), rg.check_budget(120, 20, 1000, "requests")]
    assert rg.chunk_hint(failed, 120, 40, 3) == "try --task-chunk 1/6 (then 2/6, ... on later days)"
    assert "set daily budgets" in rg.chunk_hint([rg.check_budget(3, 9000, None)], 3, 1, 3)
    too_big = [rg.check_budget(3, 100_000, 200_000)]
    assert "already exceeds" in rg.chunk_hint(too_big, 3, 1, 3)


# ---------------------------------------------------------------- existing-run config guard

PILOT_INFO = {
    "num_trials": 3,
    "seed": 300,
    "agent_info": {"llm": AGENT["llm"], "llm_args": dict(AGENT["llm_args"])},
    "user_info": {"llm": SIM["llm"], "llm_args": {"temperature": 0.0}},
    "environment_info": {"domain_name": "retail"},
}


def _sims(seed_by_trial):
    return [
        {"task_id": "0", "trial": t, "seed": s, "termination_reason": "user_stop"}
        for t, s in seed_by_trial
    ]


def test_run_config_mismatches_accepts_identical_config():
    config = rg.parse_config(raw_config())
    sims = _sims(enumerate(rg.trial_seeds(300, 3)))
    assert rg.run_config_mismatches(PILOT_INFO, sims, config, config.simulators[0]) == []
    assert rg.run_config_mismatches(None, [], config, config.simulators[0]) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"seed": 999}, "seed 300 != planned 999"),
        ({"num_trials": 5}, "num_trials 3 != planned 5"),
        ({"simulators": [dict(SIM, llm="groq/openai/gpt-oss-20b")]}, "user llm"),
        ({"simulators": [dict(SIM, llm_args={"temperature": 1.0})]}, "user llm_args"),
        (
            {"agent": dict(AGENT, llm_args=dict(AGENT["llm_args"], temperature=0.7))},
            "agent llm_args",
        ),
        ({"agent": dict(AGENT, llm="openai/qwen3-8b")}, "agent llm"),
        ({"domain": "airline"}, "domain 'retail' != planned 'airline'"),
    ],
)
def test_run_config_mismatches_refuses_changed_settings(overrides, expected):
    # Every one of these left the legacy pilot run name unchanged.
    raw = raw_config(run_name_template="{experiment}_{agent}_{user}", **overrides)
    config = rg.parse_config(raw)
    assert rg.make_run_name(config, config.simulators[0]) == "pilot1_qwen4b_gptoss120b"
    problems = rg.run_config_mismatches(PILOT_INFO, [], config, config.simulators[0])
    assert len(problems) == 1 and expected in problems[0]


def test_run_config_mismatches_flags_files_that_already_mix_seeds():
    config = rg.parse_config(raw_config())
    sims = _sims([(0, 626729), (0, 12345), (1, 373753)])
    problems = rg.run_config_mismatches(PILOT_INFO, sims, config, config.simulators[0])
    assert problems == ["trial(s) [0] already hold simulations with different seeds"]


# ---------------------------------------------------------------- NL-assertion judge preflight

FAKE_TASKS = [
    {"id": "0", "evaluation_criteria": {"nl_assertions": None, "reward_basis": ["DB"]}},
    {"id": "1", "evaluation_criteria": {"nl_assertions": [], "reward_basis": ["DB"]}},
    {"id": "2", "evaluation_criteria": {"nl_assertions": ["Agent says 10 options."]}},
    {"id": "3", "evaluation_criteria": None},
]


def test_nl_assertion_task_ids_and_exclusion(fake_splits):
    nl = rg.nl_assertion_task_ids(FAKE_TASKS)
    assert nl == {"2"}
    splits = rg.load_splits(rg.split_file_path(fake_splits, "retail"))
    source = rg.TaskSource(split="train", limit=3, exclude_nl_assertions=True)
    # "2" is removed BEFORE the limit: first 3 of [0, 1, 3, 4, 6].
    assert rg.resolve_task_ids(source, splits, nl) == ["0", "1", "3"]
    assert rg.tasks_tag(source) == "train-nonl-first3"
    with pytest.raises(rg.ConfigError, match="tasks.json"):
        rg.resolve_task_ids(source, splits)
    with pytest.raises(rg.ConfigError, match="true or false"):
        rg.parse_config(raw_config(tasks={"split": "test", "exclude_nl_assertions": "yes"}))


def test_judge_key_lookup_never_needs_values(tmp_path):
    assert rg.judge_key_var("gpt-4.1-2025-04-14") == "OPENAI_API_KEY"
    assert rg.judge_key_var("groq/openai/gpt-oss-120b") == "GROQ_API_KEY"
    assert rg.judge_key_var("someprovider/x") is None
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nGROQ_API_KEY=abc\nexport GEMINI_API_KEY="x"\nOPENAI_API_KEY=\nEMPTY=""\n',
        encoding="utf-8",
    )
    assert rg.dotenv_var_names(env) == {"GROQ_API_KEY", "GEMINI_API_KEY"}
    assert not rg.env_var_available("OPENAI_API_KEY", {}, env)
    assert rg.env_var_available("OPENAI_API_KEY", {"OPENAI_API_KEY": "k"}, env)
    assert rg.env_var_available("GROQ_API_KEY", {}, env)
    assert rg.dotenv_var_names(tmp_path / "missing.env") == set()


def test_judge_preflight_message():
    nl = {"2", "3"}
    assert rg.judge_preflight(["0", "1"], nl, "gpt-4.1", False) is None  # no NL task
    assert rg.judge_preflight(["0", "2"], nl, "gpt-4.1", True) is None  # key present
    assert rg.judge_preflight(["2"], nl, "x/unknown", None) is None  # cannot check
    msg = rg.judge_preflight(["0", "2", "3"], nl, "gpt-4.1", False)
    assert msg is not None
    assert msg.startswith("2 of 3 task(s) still to run have NL assertions (2, 3)")
    assert "OPENAI_API_KEY" in msg and "exclude_nl_assertions" in msg


def test_nl_judge_model_reads_tau2_config(tmp_path):
    assert rg.nl_judge_model(tmp_path) == rg.FALLBACK_NL_JUDGE_MODEL
    cfg = tmp_path / "src" / "tau2" / "config.py"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('X = 1\nDEFAULT_LLM_NL_ASSERTIONS = "groq/judge-model"\n', encoding="utf-8")
    assert rg.nl_judge_model(tmp_path) == "groq/judge-model"


# ---------------------------------------------------------------- main() end to end (dry run)


@pytest.fixture
def fake_tau2(fake_splits):
    tasks = rg.tasks_file_path(fake_splits, "retail")
    tasks.write_text(json.dumps(FAKE_TASKS), encoding="utf-8")
    return fake_splits


def _write_config(tmp_path, **overrides):
    path = tmp_path / "grid.json"
    path.write_text(json.dumps(raw_config(**overrides)), encoding="utf-8")
    return path


def test_main_refuses_nl_tasks_without_judge_key(fake_tau2, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _write_config(tmp_path)  # tasks 0, 1, 2; task 2 has an NL assertion
    argv = ["--tau2-dir", str(fake_tau2), "--dry-run"]
    assert rg.main([str(cfg), *argv]) == 1
    out = capsys.readouterr().out
    assert "1 of 3 task(s) still to run have NL assertions (2)" in out
    assert "refused: NL-assertion judge key missing" in out
    assert "uv run tau2 run" not in out
    # Explicit override, --config spelling, and a key in tau2-bench/.env all proceed.
    assert rg.main(["--config", str(cfg), *argv, "--allow-missing-judge-key"]) == 0
    assert "uv run tau2 run" in capsys.readouterr().out
    (fake_tau2 / ".env").write_text("OPENAI_API_KEY=not-printed\n", encoding="utf-8")
    assert rg.main([str(cfg), *argv]) == 0
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY present" in out and "not-printed" not in out


def test_main_excluding_nl_tasks_needs_no_key(fake_tau2, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _write_config(tmp_path, tasks={"ids": ["0", "1", "2"], "exclude_nl_assertions": True})
    assert rg.main([str(cfg), "--tau2-dir", str(fake_tau2), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--task-ids 0 1 --max-concurrency" in out
    assert "simulations: 0 already done, 6 to run" in out


def test_main_refuses_resuming_a_run_with_another_config(fake_tau2, tmp_path, capsys):
    cfg = _write_config(
        tmp_path,
        tasks={"ids": ["0", "1"]},
        seed=999,
        run_name_template="{experiment}_{agent}_{user}",
    )
    run = rg.results_path(fake_tau2, "pilot1_qwen4b_gptoss120b")
    run.parent.mkdir(parents=True)
    run.write_text(
        json.dumps({"info": PILOT_INFO, "simulations": _sims([(0, 626729)])}), encoding="utf-8"
    )
    assert rg.main([str(cfg), "--tau2-dir", str(fake_tau2), "--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "existing run differs: seed 300 != planned 999" in out
    assert "refused: existing run has a different config" in out


def test_main_requires_a_config(capsys):
    with pytest.raises(SystemExit):
        rg.main(["--dry-run"])
    assert "config file is required" in capsys.readouterr().err


def test_pending_task_ids_and_finished_nl_tasks_are_not_refused(
    fake_tau2, tmp_path, monkeypatch, capsys
):
    s0, s1 = rg.trial_seeds(300, 2)
    done = {("2", 0, s0), ("2", 1, s1), ("0", 0, s0)}
    assert rg.pending_task_ids(["0", "1", "2"], 2, done, [s0, s1]) == ["0", "1"]
    # Task 2 (NL assertion) already finished both trials: only judge-free work remains.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _write_config(tmp_path, num_trials=2, run_name_template="{experiment}_{agent}_{user}")
    run = rg.results_path(fake_tau2, "pilot1_qwen4b_gptoss120b")
    run.parent.mkdir(parents=True)
    sims = [
        {"task_id": t, "trial": r, "seed": sd, "termination_reason": "user_stop"}
        for t, r, sd in sorted(done)
    ]
    run.write_text(json.dumps({"info": dict(PILOT_INFO, num_trials=2), "simulations": sims}))
    assert rg.main([str(cfg), "--tau2-dir", str(fake_tau2), "--dry-run"]) == 0
    assert "simulations: 3 already done, 3 to run" in capsys.readouterr().out
