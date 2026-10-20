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
    assert env == {
        "PATH": "x",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "REQUEST_TIMEOUT": rg.DEFAULT_REQUEST_TIMEOUT_S,
    }


def test_subprocess_env_keeps_an_explicit_request_timeout():
    env = rg.subprocess_env({"REQUEST_TIMEOUT": "900"})
    assert env["REQUEST_TIMEOUT"] == "900"


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


# ---------------------------------------------------------------- RQ2 condition grids

SMOKE_HI = REPO_ROOT / "tests" / "fixtures" / "fidelity" / "fid_hi_smoke" / "results.json"
COND_EN = {
    "name": "en_pack",
    "task_set_name": "retail_en",
    "task_id_suffix": "_en",
    "user_persona_id": "en",
    "tokens_per_task_estimate": 13000,
}
COND_HI_ROM = {
    "name": "hi_romanized",
    "task_set_name": "retail_hi",
    "task_id_suffix": "_hi",
    "user_persona_id": "hi",
    "text_input_style": "romanized",
    "tokens_per_task_estimate": 35000,
}
COND_HI_CM = dict(COND_HI_ROM, name="hi_code_mixed", text_input_style="code_mixed")
PILOT_TASKS = {"split": "test", "exclude_nl_assertions": True, "limit": 12}
# config_hash payload of the hi_romanized cell (AGENT/SIM + condition), written out by hand.
HI_ROM_HASH = hashlib.sha1(
    b'{"agent_llm":"openai/qwen3-4b","agent_llm_args":{"api_base":"http://127.0.0.1:8080/v1",'
    b'"api_key":"local","temperature":0.0},"condition":{"communicate_judge":"exact",'
    b'"extra_tau2_args":[],"task_id_suffix":"_hi","task_set_name":"retail_hi",'
    b'"text_input_style":"romanized","user_persona_id":"hi"},"domain":"retail",'
    b'"user_llm":"groq/openai/gpt-oss-120b","user_llm_args":{"temperature":0.0}}'
).hexdigest()[:6]


def cond_config(conditions=None, **overrides):
    conds = [COND_EN, COND_HI_ROM, COND_HI_CM] if conditions is None else conditions
    return raw_config(conditions=[dict(c) for c in conds], **overrides)


def pilot_config(conditions=None):
    return cond_config(conditions, experiment="rq2pilot", num_trials=2, tasks=PILOT_TASKS)


def test_condition_command_exact_argv():
    config = rg.parse_config(cond_config(num_trials=2))
    hi = config.conditions[1]
    ids = rg.condition_task_ids(["5", "9"], hi)
    argv = rg.build_tau2_command(config, config.simulators[0], ids, "my_run", hi)
    assert argv == [
        "uv", "run", "tau2", "run",
        "--domain", "retail",
        # condition flags right after --domain; the communicate judge, the language
        # directive version and the caller-locale context are always pinned
        "--task-set-name", "retail_hi",
        "--user-persona-id", "hi",
        "--text-input-style", "romanized",
        "--no-llm-communicate-judge",
        "--target-language-directive-version", "v3",
        "--agent-caller-locale-context",
        "--agent-llm", "openai/qwen3-4b",
        "--agent-llm-args",
        '{"api_base":"http://127.0.0.1:8080/v1","api_key":"local","temperature":0.0}',
        "--user-llm", "groq/openai/gpt-oss-120b",
        "--user-llm-args", '{"temperature":0.0}',
        "--num-trials", "2",
        "--seed", "300",
        "--task-ids", "5_hi", "9_hi",
        "--max-concurrency", "1",
        "--workers", "0",  # the branch defaults to 8 worker processes for text runs
        "--save-to", "my_run",
        "--auto-resume",
    ]  # fmt: skip


def test_condition_command_without_style_and_with_extra_args():
    en = dict(COND_EN, extra_tau2_args=["--max-retries", "5"])
    config = rg.parse_config(cond_config([en], extra_tau2_args=["--workers", "2"]))
    argv = rg.build_tau2_command(config, config.simulators[0], ["5_en"], "r", config.conditions[0])
    assert argv[4:11] == [
        "--domain", "retail",
        "--task-set-name", "retail_en",
        "--user-persona-id", "en",
        "--no-llm-communicate-judge",
    ]  # fmt: skip
    assert "--text-input-style" not in argv
    # a --workers in extra args replaces the pin; config extras come before condition extras
    assert argv.count("--workers") == 1
    assert argv[-5:] == ["--auto-resume", "--workers", "2", "--max-retries", "5"]
    plain = rg.parse_config(raw_config())
    plain_argv = rg.build_tau2_command(plain, plain.simulators[0], ["5"], "r")
    assert "--no-llm-communicate-judge" not in plain_argv and "--workers" not in plain_argv


NO_TPT_EN = {k: v for k, v in COND_EN.items() if k != "tokens_per_task_estimate"}


@pytest.mark.parametrize(
    ("conditions", "extra", "match"),
    [
        ([dict(COND_HI_ROM, user_persona_id=None)], [], "needs a language-pack user_persona_id"),
        ([dict(COND_HI_ROM, text_input_style="hinglish")], [], "is not one of"),
        ([dict(COND_EN, extra_tau2_args=["--task-ids", "1"])], [], "set by the condition fields"),
        ([COND_EN], ["--text-input-style=romanized"], "set per condition"),
        # tau2's argparse expands unique prefixes, so these would override the condition
        ([dict(COND_HI_ROM, extra_tau2_args=["--text-input", "code_mixed"])], [], "abbreviate"),
        ([dict(COND_HI_ROM, extra_tau2_args=["--llm-comm"])], [], "abbreviate"),
        ([COND_EN], ["--save", "other_run"], "abbreviate"),
        ([COND_EN], ["--text-input=code_mixed"], "abbreviate"),
        ([dict(COND_EN, extra_tau2_args=["--max-st", "30"])], [], "abbreviate"),
        ([COND_EN], ["-d", "airline"], "abbreviate"),  # short option for --domain
        # set by run_grid itself or pinned on every condition cell
        ([COND_EN], ["--seed", "7"], "set per condition or by run_grid"),
        ([COND_EN], ["--num-tasks=5"], "set per condition or by run_grid"),
        ([COND_EN], ["--target-language-directive-version", "v1"], "set per condition"),
        (
            [dict(COND_EN, extra_tau2_args=["--no-agent-caller-locale-context"])],
            [],
            "set by the condition fields or by run_grid",
        ),
        ([COND_EN, COND_EN], [], "names must be unique"),
        ([NO_TPT_EN], [], "tokens_per_task_estimate"),
        ([], [], "non-empty list"),
        ([dict(COND_EN, enabled="no")], [], "true or false"),
        ([dict(COND_EN, name="en pack")], [], "must match"),
    ],
)
def test_condition_config_validation(conditions, extra, match):
    with pytest.raises(rg.ConfigError, match=match):
        rg.parse_config(raw_config(conditions=conditions, extra_tau2_args=extra))


def test_condition_extra_args_accept_exact_options():
    # Exact tau2 options that are prefixes of a reserved flag (--agent, --user-persona),
    # the overridable run settings and unrelated options are fine.
    extra = [
        "--agent", "llm_agent",
        "--user-persona", '{"verbosity": "minimal"}',
        "--max-steps", "30",
        "--workers=2",
        "--max-retries", "5",
        "--text-noise",
    ]  # fmt: skip
    rg.parse_config(cond_config([dict(COND_EN, extra_tau2_args=extra)]))
    flags = ["--text-input=code_mixed", "--llm-comm", "-dairline", "--max-ret", "5", "--agent"]
    assert rg.abbreviated_flags(flags) == ["--llm-comm", "--text-input", "-d"]
    # Grids without conditions keep their old, unchecked extra args (RQ1 behaviour).
    plain = rg.parse_config(raw_config(extra_tau2_args=["--seed", "1", "--save", "x"]))
    assert plain.extra_tau2_args == ("--seed", "1", "--save", "x")


def test_condition_suffix_is_applied_after_chunking_base_ids():
    hi = rg.parse_config(cond_config()).conditions[1]
    assert rg.condition_task_ids(["5", "9", "12"], hi) == ["5_hi", "9_hi", "12_hi"]
    assert rg.condition_task_ids(["5", "9"], None) == ["5", "9"]
    # chunks are cut on the base ids, so every condition gets the same tasks per chunk
    base = [str(i) for i in range(12)]
    cumulative, new = rg.select_chunk(base, 2, 6)
    assert rg.condition_task_ids(cumulative, hi) == ["0_hi", "1_hi", "2_hi", "3_hi"]
    assert rg.condition_task_ids(new, hi) == ["2_hi", "3_hi"]


def test_per_condition_budget_math():
    sim = dict(SIM, daily_request_budget=1000, requests_per_task_estimate=20)
    config = rg.parse_config(cond_config(simulators=[sim], num_trials=2))
    s = config.simulators[0]
    en, hi, cm = config.conditions
    assert rg.per_simulation_estimates(s) == {"tokens": (9000, 200_000), "requests": (20, 1000)}
    # the condition's token estimate replaces the simulator's; the budget stays the simulator's
    assert rg.per_simulation_estimates(s, hi) == {
        "tokens": (35000, 200_000),
        "requests": (20, 1000),
    }
    cheap = rg.parse_config(cond_config([dict(COND_EN, requests_per_task_estimate=12)]))
    assert rg.per_simulation_estimates(s, cheap.conditions[0])["requests"] == (12, 1000)
    # Option A: 12 tasks x 2 trials = 24 simulations per cell; two Hindi cells share the quota
    shared = rg.shared_budget_checks(s, [(hi, 24), (cm, 24)])
    assert [(c.unit, c.planned, c.ok) for c in shared] == [
        ("tokens", 1_680_000, False),
        ("requests", 960, True),
    ]
    mixed = rg.shared_budget_checks(s, [(en, 24), (hi, 24)])
    assert mixed[0].planned == 24 * 13000 + 24 * 35000
    assert rg.shared_budget_checks(s, [(hi, 24)]) == rg.budget_checks(s, 24, hi)
    # together: 200000 // (2 x 70000) = 1 task/day -> 12 chunks; one condition: 2/day -> 6
    assert rg.shared_chunk_hint(s, [hi, cm], 12, 2) == (
        "all 2 conditions together: try --task-chunk 1/12 (then 2/12, ... on later days); "
        "or one condition per run: --condition NAME --task-chunk 1/6"
    )
    # English pack: 200000 // (2 x 13000) = 7 tasks/day -> 2 chunks
    assert rg.shared_chunk_hint(s, [en], 12, 2) == (
        "try --task-chunk 1/2 (then 2/2, ... on later days)"
    )
    too_big = dict(COND_HI_ROM, tokens_per_task_estimate=150_000)
    big = rg.parse_config(cond_config([too_big], simulators=[sim], num_trials=2)).conditions[0]
    assert "already exceeds" in rg.shared_chunk_hint(s, [big], 12, 2)


def test_condition_run_names_are_deterministic_and_distinct():
    def names(raw):
        config = rg.parse_config(raw)
        return [rg.make_run_name(config, config.simulators[0], c) for c in config.conditions]

    first = names(pilot_config())
    assert first == names(pilot_config())
    assert len(set(first)) == 3
    assert first[1] == (
        f"rq2pilot_qwen4b_gptoss120b_hi-romanized_test-nonl-first12_t2_s300_c{HI_ROM_HASH}"
    )
    assert HI_ROM_HASH != CFG_HASH
    # budget estimates, notes and enabled do not change what runs, so not the name either
    tweaked = dict(COND_HI_ROM, tokens_per_task_estimate=42000, notes="x", enabled=False)
    assert names(pilot_config([COND_EN, tweaked, COND_HI_CM])) == first
    # what does run (persona, extra args) changes the hash under the same condition name
    pinned = dict(COND_HI_ROM, user_persona_id="rishika_hindi_v1")
    retried = dict(COND_HI_ROM, extra_tau2_args=["--max-retries", "5"])
    for variant in (pinned, retried):
        assert names(pilot_config([COND_EN, variant, COND_HI_CM]))[1] != first[1]


def test_top_level_extra_args_change_condition_run_names():
    def hi_name(extra):
        raw = cond_config(experiment="rq2pilot", num_trials=2, tasks=PILOT_TASKS)
        config = rg.parse_config(dict(raw, extra_tau2_args=extra))
        return rg.make_run_name(config, config.simulators[0], config.conditions[1])

    base = hi_name([])
    # an empty top-level list adds nothing to the hash: the pinned name is unchanged
    assert base.endswith(f"_c{HI_ROM_HASH}")
    edited = hi_name(["--max-steps", "30"])
    assert edited != base and edited != hi_name(["--max-steps", "31"])
    # grids without conditions do not hash extra args (RQ1 names are pinned elsewhere)
    plain = rg.parse_config(raw_config(extra_tau2_args=["--max-retries", "5"]))
    assert rg.config_hash(plain, plain.simulators[0]) == CFG_HASH


def test_condition_cells_refuse_shared_run_names():
    config = rg.parse_config(cond_config(run_name_template="{experiment}_{agent}_{user}"))
    with pytest.raises(rg.ConfigError, match="shared by cells"):
        rg.selected_cells(config, list(config.simulators), list(config.conditions))
    # without conditions the default template and names are the pre-RQ2 ones
    plain = rg.parse_config(raw_config())
    assert plain.run_name_template == rg.DEFAULT_RUN_NAME_TEMPLATE
    with_conditions = rg.parse_config(cond_config())
    assert with_conditions.run_name_template == rg.DEFAULT_CONDITION_RUN_NAME_TEMPLATE
    cells = rg.selected_cells(plain, list(plain.simulators), [None])
    assert [name for *_, name in cells] == [rg.make_run_name(plain, plain.simulators[0])]


def test_rq1_run_names_unchanged_through_the_cell_path():
    config = rg.load_config(REPO_ROOT / "configs" / "rq1_simulator_swap.json")
    assert config.conditions == () and config.reference_tau2_dir is None
    sims = rg._selected_simulators(config, None)
    conditions = rg._selected_conditions(config, None)
    assert conditions == [None]
    assert [name for *_, name in rg.selected_cells(config, sims, conditions)] == [
        "rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638",
        "rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908",
    ]
    argv = rg.build_tau2_command(config, sims[0], ["5"], "r")
    assert "--workers" not in argv and "--task-set-name" not in argv


def test_shipped_rq2_config_is_option_a_with_english_arm():
    config = rg.load_config(REPO_ROOT / "configs" / "rq2_option_a.json")
    sims = rg._selected_simulators(config, None)
    assert [(s.name, s.llm) for s in sims] == [("gptoss120b", "groq/openai/gpt-oss-120b")]
    assert sims[0].llm_args == {
        "temperature": 0.0,
        "num_retries": 8,
        "retry_strategy": "exponential_backoff_retry",
    }
    assert config.tasks == rg.TaskSource(split="test", exclude_nl_assertions=True, limit=12)
    assert (config.num_trials, config.seed, config.max_concurrency) == (2, 300, 1)
    assert {c.name: c.enabled for c in config.conditions} == {
        "en_pack": True,
        "hi_native": False,
        "hi_romanized": True,
        "hi_code_mixed": True,
    }
    by_name = {c.name: c for c in config.conditions}
    assert by_name["en_pack"].tokens_per_task_estimate == 13000
    assert by_name["hi_romanized"].tokens_per_task_estimate == 42000
    assert by_name["hi_code_mixed"].tokens_per_task_estimate == 42000
    assert config.tau2_dir is not None and "multilingual" in config.tau2_dir
    assert config.reference_tau2_dir is not None
    conditions = rg._selected_conditions(config, None)
    assert [name for *_, name in rg.selected_cells(config, sims, conditions)] == [
        "rq2a_qwen4b_gptoss120b_en-pack_test-nonl-first12_t2_s300_ca8f1b3",
        "rq2a_qwen4b_gptoss120b_hi-romanized_test-nonl-first12_t2_s300_cb50a00",
        "rq2a_qwen4b_gptoss120b_hi-code-mixed_test-nonl-first12_t2_s300_c7c5992",
    ]
    assert config.agent.llm_args["api_key"] == "local"


def test_condition_mismatches_against_the_real_hindi_smoke_run():
    data = json.loads(SMOKE_HI.read_text(encoding="utf-8"))
    info = data["info"]
    en, rom, cm = rg.parse_config(cond_config()).conditions
    assert rg.condition_mismatches(info, rom) == []
    assert rg.condition_mismatches(info, cm) == [
        "text_input_style 'romanized' != planned 'code_mixed'"
    ]
    assert rg.condition_mismatches(info, en) == [
        "task_set_name 'retail_hi' != planned 'retail_en'",
        "user_persona_id 'hi' != planned 'en'",
        "text_input_style 'romanized' != planned 'native_script'",
    ]
    # an English run made on main has none of the branch keys, so it never resumes as a cell
    assert len(rg.condition_mismatches(PILOT_INFO, rom)) == 4
    # null and native_script are the same arm
    native = rg.parse_config(cond_config([dict(COND_HI_ROM, text_input_style="native_script")]))
    assert rg.condition_mismatches(dict(info, text_input_style=None), native.conditions[0]) == []
    # the branch draws per-trial seeds exactly like main (seed 42 -> 670487 in the smoke run)
    assert rg.trial_seeds(info["seed"], 1) == [data["simulations"][0]["seed"]]


PACK_HI = """\
language: hi
personas:
- persona_id: rishika_hindi_v1
  tags:
    gender: female
text_input:
  styles:
  - style: romanized
    examples:
    - standard: x
      typed: y
  - style: code_mixed  # Hinglish
later_block:
  - style: diacritic_free
"""
PACK_EN = "language: en\npersonas:\n- persona_id: matt_en_v1\n  closing_style: brisk\n"


@pytest.fixture
def fake_ml_tau2(fake_tau2):
    """fake_tau2 plus tau-multilingual packs: hi/en task sets paired with FAKE_TASKS."""
    root = rg.multilingual_dir(fake_tau2)
    for lang, pack in (("hi", PACK_HI), ("en", PACK_EN)):
        (root / lang).mkdir(parents=True)
        localized = [dict(t, id=f"{t['id']}_{lang}") for t in FAKE_TASKS]
        (root / lang / f"retail_tasks_{lang}.json").write_text(json.dumps(localized))
        (root / lang / "pack.yaml").write_text(pack, encoding="utf-8")
    return fake_tau2


def test_task_set_files_and_pairing(fake_ml_tau2):
    hi_file = rg.multilingual_dir(fake_ml_tau2) / "hi" / "retail_tasks_hi.json"
    assert rg.task_set_file_path(fake_ml_tau2, "retail", "retail_hi") == hi_file
    base_file = rg.tasks_file_path(fake_ml_tau2, "retail")
    assert rg.task_set_file_path(fake_ml_tau2, "retail", "retail") == base_file
    with pytest.raises(rg.ConfigError, match="not a 'retail' task set"):
        rg.task_set_file_path(fake_ml_tau2, "retail", "airline_hi")
    with pytest.raises(rg.ConfigError, match="tau-multilingual checkout"):
        rg.task_set_file_path(fake_ml_tau2, "retail", "retail_ko")
    localized = rg.load_tasks(hi_file)
    assert rg.pairing_problems(FAKE_TASKS, localized, ["0", "1", "2"], "_hi") == []
    assert "missing" in rg.pairing_problems(FAKE_TASKS, localized, ["0", "7"], "_hi")[0]
    changed = [dict(t, evaluation_criteria={"nl_assertions": ["x"]}) for t in localized]
    assert "unlike the base task" in rg.pairing_problems(FAKE_TASKS, changed, ["0"], "_hi")[0]


def test_pairing_needs_same_user_scenario_and_initial_state():
    # Like retail_hi_identity: same evaluation_criteria, other user and starting DB.
    base = [
        dict(t, user_scenario={"instructions": "Yusuf"}, initial_state=None) for t in FAKE_TASKS
    ]
    paired = [dict(t, id=f"{t['id']}_hi") for t in base]
    assert rg.pairing_problems(base, paired, ["0", "1"], "_hi") == []
    # metadata (description, issues) may differ
    noted = [dict(t, description={"notes": "x"}, issues=[{"id": "i"}]) for t in paired]
    assert rg.pairing_problems(base, noted, ["0", "1"], "_hi") == []
    identity = [dict(t, user_scenario={"instructions": "Arjun"}) for t in paired]
    assert rg.pairing_problems(base, identity, ["0", "1"], "_hi") == [
        "2 id(s) have user_scenario unlike the base task: ['0_hi', '1_hi']"
    ]
    new_db = [dict(t, initial_state={"users": {"arjun_chopra_6575": {}}}) for t in paired]
    assert rg.pairing_problems(base, new_db, ["1"], "_hi") == [
        "1 id(s) have initial_state unlike the base task: ['1_hi']"
    ]
    both = [dict(t, user_scenario={}, initial_state={}) for t in paired]
    assert "user_scenario, initial_state unlike" in rg.pairing_problems(base, both, ["0"], "_hi")[0]


def test_main_refuses_an_identity_like_task_set(fake_ml_tau2, tmp_path, capsys):
    hi_dir = rg.multilingual_dir(fake_ml_tau2) / "hi"
    identity = [
        dict(t, id=f"{t['id']}_hi_identity", user_scenario={"name": "Arjun"}) for t in FAKE_TASKS
    ]
    (hi_dir / "retail_tasks_hi_identity.json").write_text(json.dumps(identity))
    cond = dict(
        COND_HI_ROM,
        name="hi_identity",
        task_set_name="retail_hi_identity",
        task_id_suffix="_hi_identity",
        text_input_style=None,
    )
    cfg = _write_config(tmp_path, tasks={"ids": ["0", "1"]}, conditions=[cond])
    assert rg.main([str(cfg), "--tau2-dir", str(fake_ml_tau2), "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert "have user_scenario unlike the base task" in captured.err
    assert "uv run tau2 run" not in captured.out


def test_text_style_must_be_declared_by_the_persona_pack(fake_ml_tau2):
    assert rg.declared_text_input_styles(PACK_HI) == {"romanized", "code_mixed"}
    assert rg.declared_text_input_styles(PACK_EN) == set()
    hi_pack = rg.multilingual_dir(fake_ml_tau2) / "hi" / "pack.yaml"
    assert rg.persona_pack_file(fake_ml_tau2, "hi") == hi_pack
    assert rg.persona_pack_file(fake_ml_tau2, "rishika_hindi_v1") == hi_pack
    assert rg.persona_pack_file(fake_ml_tau2, "matt_delaney") is None

    def problem(**fields):
        condition = rg.parse_config(cond_config([dict(COND_HI_ROM, **fields)])).conditions[0]
        return rg.text_style_problem(fake_ml_tau2, condition)

    assert problem() is None
    assert problem(user_persona_id="rishika_hindi_v1", text_input_style="code_mixed") is None
    assert problem(user_persona_id="en", text_input_style="native_script") is None
    assert "declares text_input styles none, not 'romanized'" in problem(user_persona_id="en")
    wrong = problem(text_input_style="diacritic_free")
    assert "['code_mixed', 'romanized'], not 'diacritic_free'" in wrong
    assert "names no pack" in problem(user_persona_id="matt_delaney")


def test_base_task_file_differences(fake_tau2, tmp_path):
    ref = tmp_path / "reference"
    for path_of in (rg.split_file_path, rg.tasks_file_path):
        target = path_of(ref, "retail")
        target.parent.mkdir(parents=True, exist_ok=True)
        # same content, different formatting: still identical
        data = json.loads(path_of(fake_tau2, "retail").read_text(encoding="utf-8"))
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert rg.base_task_file_differences(fake_tau2, ref, "retail") == []
    changed = [dict(t) for t in FAKE_TASKS]
    changed[0]["evaluation_criteria"] = {"nl_assertions": ["new"]}
    rg.tasks_file_path(ref, "retail").write_text(json.dumps(changed), encoding="utf-8")
    assert rg.base_task_file_differences(fake_tau2, ref, "retail") == ["tasks.json"]
    with pytest.raises(OSError):
        rg.base_task_file_differences(fake_tau2, tmp_path / "missing", "retail")


def test_main_condition_grid_dry_run(fake_ml_tau2, tmp_path, capsys):
    conds = [COND_EN, COND_HI_ROM, dict(COND_HI_CM, enabled=False)]
    cfg = _write_config(tmp_path, tasks={"ids": ["0", "1"]}, num_trials=2, conditions=conds)
    argv = [str(cfg), "--tau2-dir", str(fake_ml_tau2), "--dry-run"]
    assert rg.main(argv) == 0
    out = capsys.readouterr().out
    assert "conditions: 2 selected (not selected: hi_code_mixed)" in out
    assert "2/2 ids found in data/tau2/multilingual/hi/retail_tasks_hi.json" in out
    assert "style romanized declared by the persona's pack" in out
    # 2 tasks x 2 trials: en 4 x 13000 + hi 4 x 35000 = 192000 on one shared quota
    assert "daily budget shared by 2 cell(s) (en_pack, hi_romanized):" in out
    assert "tokens: planned 192000 tokens fits budget 200000" in out
    assert (
        "--task-set-name retail_hi --user-persona-id hi --text-input-style romanized "
        "--no-llm-communicate-judge --target-language-directive-version v3 "
        "--agent-caller-locale-context --agent-llm"
    ) in out
    assert "--task-ids 0_hi 1_hi --max-concurrency 1 --workers 0 --save-to" in out
    assert "--task-ids 0_en 1_en --max-concurrency 1 --workers 0 --save-to" in out
    # a disabled condition runs only when named
    assert rg.main([*argv, "--condition", "hi_code_mixed"]) == 0
    out = capsys.readouterr().out
    assert "--text-input-style code_mixed" in out and "retail_en" not in out


def test_main_condition_grid_budget_refusal_and_chunks(fake_ml_tau2, tmp_path, capsys):
    conds = [COND_EN, COND_HI_ROM]
    cfg = _write_config(tmp_path, tasks={"ids": ["0", "1"]}, num_trials=3, conditions=conds)
    argv = [str(cfg), "--tau2-dir", str(fake_ml_tau2), "--dry-run"]
    # 6 x 13000 + 6 x 35000 = 288000 > 200000: both cells are refused together
    assert rg.main(argv) == 1
    out = capsys.readouterr().out
    assert "tokens: planned 288000 tokens EXCEEDS budget 200000" in out
    assert out.count("refused: over budget") >= 2
    assert "all 2 conditions together: try --task-chunk 1/2" in out
    assert "uv run tau2 run" not in out
    assert rg.main([*argv, "--task-chunk", "1/2"]) == 0
    out = capsys.readouterr().out
    assert "tokens: planned 144000 tokens fits budget 200000" in out
    assert "--task-ids 0_hi --max-concurrency" in out


def test_main_condition_grid_refusals(fake_ml_tau2, tmp_path, capsys):
    tau2 = ["--tau2-dir", str(fake_ml_tau2), "--dry-run"]
    # a style the persona's pack does not declare
    bad = [dict(COND_EN, text_input_style="romanized")]
    bad_style = _write_config(tmp_path, tasks={"ids": ["0"]}, conditions=bad)
    assert rg.main([str(bad_style), *tau2]) == 2
    assert "declares text_input styles none" in capsys.readouterr().err
    # base task files that differ from the reference checkout
    ref = tmp_path / "ref"
    rg.split_file_path(ref, "retail").parent.mkdir(parents=True)
    splits = rg.split_file_path(fake_ml_tau2, "retail").read_text(encoding="utf-8")
    rg.split_file_path(ref, "retail").write_text(splits, encoding="utf-8")
    rg.tasks_file_path(ref, "retail").write_text(json.dumps(FAKE_TASKS[:2]), encoding="utf-8")
    cfg = _write_config(
        tmp_path, tasks={"ids": ["0"]}, conditions=[COND_HI_ROM], reference_tau2_dir=str(ref)
    )
    assert rg.main([str(cfg), *tau2]) == 2
    assert "tasks.json of domain 'retail' differ" in capsys.readouterr().err
    rg.tasks_file_path(ref, "retail").write_text(json.dumps(FAKE_TASKS), encoding="utf-8")
    assert rg.main([str(cfg), *tau2]) == 0
    assert "base tasks: split_tasks.json and tasks.json identical to" in capsys.readouterr().out
    # an existing run under the cell's name that was made with another style
    config = rg.parse_config(json.loads(cfg.read_text(encoding="utf-8")))
    name = rg.make_run_name(config, config.simulators[0], config.conditions[0])
    run = rg.results_path(fake_ml_tau2, name)
    run.parent.mkdir(parents=True)
    info = dict(
        PILOT_INFO,
        task_set_name="retail_hi",
        user_persona_id="hi",
        text_input_style="code_mixed",
        communicate_judge_mode="exact",
    )
    run.write_text(json.dumps({"info": info, "simulations": []}), encoding="utf-8")
    assert rg.main([str(cfg), *tau2]) == 1
    out = capsys.readouterr().out
    assert "existing run differs: text_input_style 'code_mixed' != planned 'romanized'" in out
    assert "refused: existing run has a different config" in out


def test_main_conditions_need_the_multilingual_checkout(fake_tau2, tmp_path, capsys):
    cfg = _write_config(tmp_path, tasks={"ids": ["0"]}, conditions=[COND_HI_ROM])
    assert rg.main([str(cfg), "--tau2-dir", str(fake_tau2), "--dry-run"]) == 2
    assert "Is tau2_dir the tau-multilingual checkout?" in capsys.readouterr().err
    plain = _write_config(tmp_path, tasks={"ids": ["0"]})
    argv = [str(plain), "--tau2-dir", str(fake_tau2), "--dry-run", "--condition", "x"]
    assert rg.main(argv) == 2
    assert "defines no conditions" in capsys.readouterr().err


# ---------------------------------------------------------------- tau-multilingual resume guard

# Branch header values as recorded by the real Hindi smoke run (smoke_hi_romanized_qwen38).
SMOKE_COMMIT = "8ff48b8e46575e1519e4d4b75a906770b0e4e268"
BRANCH_INFO = dict(
    PILOT_INFO,
    task_set_name="retail_hi",
    user_persona_id="hi",
    text_input_style="romanized",
    communicate_judge_mode="exact",
    target_language_directive_version="v3",
    agent_caller_locale_context=True,
    max_steps=200,
    max_errors=10,
    timeout=2400.0,
    scores=["reward"],
    text_noise=None,
    git_commit=SMOKE_COMMIT,
)


def _rom_cell(cond_overrides=None, **config_overrides):
    config = rg.parse_config(
        cond_config([dict(COND_HI_ROM, **(cond_overrides or {}))], **config_overrides)
    )
    return config, config.simulators[0], config.conditions[0]


def _with_guidelines(info, text):
    return dict(info, user_info=dict(info["user_info"], global_simulation_guidelines=text))


def test_branch_header_that_matches_the_plan_resumes():
    config, sim, rom = _rom_cell()
    assert rg.run_config_mismatches(BRANCH_INFO, [], config, sim, rom) == []
    facts = rg.CheckoutFacts(guidelines={"simulation_guidelines.md": "G"}, git_commit=SMOKE_COMMIT)
    info = _with_guidelines(BRANCH_INFO, "G")
    assert rg.run_config_mismatches(info, [], config, sim, rom, checkout=facts) == []


def test_branch_settings_changed_under_the_same_run_name_are_refused():
    # The issue's case: same run name, header written with other branch settings.
    config, sim, rom = _rom_cell()
    info = dict(
        BRANCH_INFO,
        target_language_directive_version="v1",
        agent_caller_locale_context=False,
        max_steps=30,
    )
    assert rg.run_config_mismatches(info, [], config, sim, rom) == [
        "target_language_directive_version 'v1' != planned 'v3'",
        "agent_caller_locale_context False != planned True",
        "max_steps 30 != planned 200",
    ]
    # A header from before these fields existed is not silently accepted either.
    old = {k: v for k, v in BRANCH_INFO.items() if k != "agent_caller_locale_context"}
    assert rg.run_config_mismatches(old, [], config, sim, rom) == [
        "agent_caller_locale_context None != planned True"
    ]
    # Plain (main) cells are not checked for branch fields.
    plain = rg.parse_config(raw_config())
    assert rg.run_config_mismatches(PILOT_INFO, [], plain, plain.simulators[0]) == []


def test_planned_branch_settings_follow_extra_args():
    config, _, rom = _rom_cell(
        {"extra_tau2_args": ["--max-steps", "30", "--scores", "all", "--text-noise"]},
        extra_tau2_args=["--timeout=0", "--max-errors", "5", "--text-noise-seed", "7"],
    )
    assert rg.planned_branch_settings(config, rom) == {
        "target_language_directive_version": "v3",
        "agent_caller_locale_context": True,
        "max_steps": 30,
        "max_errors": 5,
        "timeout": None,  # --timeout 0 means no timeout
        "scores": ["nativeness", "quality", "reward"],
        "text_noise": {"seed": 7},
    }
    # the condition's extra args come after the config's, so the last value wins
    twice, _, twice_rom = _rom_cell(
        {"extra_tau2_args": ["--max-steps", "40"]}, extra_tau2_args=["--max-steps", "30"]
    )
    assert rg.planned_branch_settings(twice, twice_rom)["max_steps"] == 40
    defaults = {"max_steps": 300, "max_errors": 10, "timeout": 1800.0}
    plain_config, _, plain_rom = _rom_cell()
    planned = rg.planned_branch_settings(plain_config, plain_rom, defaults)
    assert (planned["max_steps"], planned["timeout"]) == (300, 1800.0)
    assert planned["scores"] == ["reward"] and planned["text_noise"] is None
    bad_config, _, bad_rom = _rom_cell({"extra_tau2_args": ["--max-steps", "many"]})
    with pytest.raises(rg.ConfigError, match="--max-steps 'many' is not a number"):
        rg.planned_branch_settings(bad_config, bad_rom)


def test_text_noise_scores_and_timeout_are_compared():
    extra = ["--text-noise", "--scores", "reward,quality"]
    config, sim, rom = _rom_cell({"extra_tau2_args": extra})
    stored = dict(BRANCH_INFO, scores=["quality", "reward"], text_noise={"seed": 42, "x": 1})
    assert rg.run_config_mismatches(stored, [], config, sim, rom) == []
    unarmed = dict(stored, text_noise=None, scores=["reward"], timeout=None)
    assert rg.run_config_mismatches(unarmed, [], config, sim, rom) == [
        "timeout None != planned 2400.0",
        "scores ['reward'] != planned ['quality', 'reward']",
        "text_noise None != planned {'seed': 42}",
    ]


def test_guidelines_and_commit_of_the_checkout_are_compared():
    config, sim, rom = _rom_cell()
    facts = rg.CheckoutFacts(
        guidelines={"simulation_guidelines.md": "new text"}, git_commit="a" * 40
    )
    info = _with_guidelines(BRANCH_INFO, "old text")
    problems = rg.run_config_mismatches(info, [], config, sim, rom, checkout=facts)
    assert len(problems) == 2
    assert problems[0].startswith("user-simulator guidelines (stored sha1 ")
    assert "simulation_guidelines.md sha1" in problems[0]
    assert problems[1].startswith(f"git_commit {SMOKE_COMMIT[:12]} != tau2_dir HEAD aaaaaaaaaaaa")
    # an unknown commit on either side, or no guidelines files: those checks are skipped
    unknown = dict(_with_guidelines(BRANCH_INFO, "new text"), git_commit="unknown")
    for facts in (rg.CheckoutFacts(), rg.CheckoutFacts(git_commit="a" * 40)):
        assert rg.run_config_mismatches(unknown, [], config, sim, rom, checkout=facts) == []


def test_info_history_is_refused_even_when_the_header_matches():
    # The branch's --auto-resume rewrote the header to the plan and kept the code-mixed
    # regime in info_history: a matching header must not be trusted.
    config, sim, rom = _rom_cell()
    history = [{"superseded_at": "t", "info": dict(BRANCH_INFO, text_input_style="code_mixed")}]
    problems = rg.run_config_mismatches(BRANCH_INFO, [], config, sim, rom, info_history=history)
    assert problems == [
        "results.json holds simulations from 2 run configs: tau2 --auto-resume rewrote its "
        "header after a config change and kept 1 earlier header(s) in info_history"
    ]
    assert rg.run_config_mismatches(BRANCH_INFO, [], config, sim, rom, info_history=[]) == []


def test_main_refuses_a_run_with_info_history(fake_ml_tau2, tmp_path, capsys):
    cfg = _write_config(tmp_path, tasks={"ids": ["0"]}, conditions=[COND_HI_ROM])
    config = rg.parse_config(json.loads(cfg.read_text(encoding="utf-8")))
    name = rg.make_run_name(config, config.simulators[0], config.conditions[0])
    run = rg.results_path(fake_ml_tau2, name)
    run.parent.mkdir(parents=True)
    s0 = rg.trial_seeds(300, 3)[0]
    sims = [{"task_id": "0_hi", "trial": 0, "seed": s0, "termination_reason": "user_stop"}]
    history = [{"superseded_at": "t", "info": dict(BRANCH_INFO, text_input_style="code_mixed")}]
    data = {"info": BRANCH_INFO, "info_history": history, "simulations": sims}
    run.write_text(json.dumps(data), encoding="utf-8")
    assert rg.load_run_header(run) == (BRANCH_INFO, history)
    argv = [str(cfg), "--tau2-dir", str(fake_ml_tau2), "--dry-run"]
    assert rg.main(argv) == 1
    out = capsys.readouterr().out
    assert "existing run differs: results.json holds simulations from 2 run configs" in out
    assert "refused: existing run has a different config" in out
    # the same file without info_history resumes: 1 of 3 simulations is done
    run.write_text(json.dumps(dict(data, info_history=[])), encoding="utf-8")
    assert rg.main(argv) == 0
    assert "simulations: 1 already done, 2 to run" in capsys.readouterr().out


def test_read_checkout_facts(tmp_path):
    assert rg.read_checkout_facts(tmp_path) == rg.CheckoutFacts()
    config_py = tmp_path / "src" / "tau2" / "config.py"
    config_py.parent.mkdir(parents=True)
    config_py.write_text(
        "DEFAULT_MAX_STEPS = 300\nDEFAULT_MAX_ERRORS = 10  # per row\n"
        "DEFAULT_TIMEOUT_SECONDS = 1800.0\nDEFAULT_MAX_STEPS_SECONDS = 2400\n",
        encoding="utf-8",
    )
    guidelines = tmp_path / "data" / "tau2" / "user_simulator" / "simulation_guidelines.md"
    guidelines.parent.mkdir(parents=True)
    guidelines.write_bytes(b"# Guidelines\r\nBe a customer.\r\n")  # read like tau2: \r\n -> \n
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/tau-multilingual\n", encoding="utf-8")
    (git / "refs" / "heads" / "tau-multilingual").write_text(SMOKE_COMMIT + "\n")
    facts = rg.read_checkout_facts(tmp_path)
    assert facts.defaults == {"max_steps": 300, "max_errors": 10, "timeout": 1800.0}
    assert facts.guidelines == {"simulation_guidelines.md": "# Guidelines\nBe a customer.\n"}
    assert facts.git_commit == SMOKE_COMMIT
    # a ref that only exists in packed-refs, a detached HEAD, and a dangling ref
    (git / "refs" / "heads" / "tau-multilingual").unlink()
    packed = f"# pack-refs with: peeled\n{'b' * 40} refs/heads/tau-multilingual\n"
    (git / "packed-refs").write_text(packed, encoding="utf-8")
    assert rg.checkout_commit(tmp_path) == "b" * 40
    (git / "HEAD").write_text("c" * 40 + "\n", encoding="utf-8")
    assert rg.checkout_commit(tmp_path) == "c" * 40
    (git / "HEAD").write_text("ref: refs/heads/missing\n", encoding="utf-8")
    assert rg.checkout_commit(tmp_path) is None
