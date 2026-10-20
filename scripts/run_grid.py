"""Run a grid of tau2-bench simulations: (user simulators) x (tasks) x (trials) for one agent.

Usage (from the whose-noise repo root; ``--config PATH`` is accepted as well)::

    python scripts/run_grid.py configs/pilot.json --dry-run
    python scripts/run_grid.py configs/rq1_simulator_swap.json --dry-run
    python scripts/run_grid.py configs/rq1_simulator_swap.json --only gptoss120b --task-chunk 1/6
    python scripts/run_grid.py configs/rq2_option_a.json --dry-run --task-chunk 1/12
    python scripts/run_grid.py configs/rq2_option_a.json --condition hi_romanized

The script is standard-library only, so any Python >= 3.11 works (``uv run python ...``
also works once the package builds).

For every enabled simulator in the config this script builds one ``tau2 run`` command::

    uv run tau2 run --domain retail --agent-llm <agent> --agent-llm-args '<json>'
        --user-llm <sim> --user-llm-args '<json>' --num-trials N --seed S
        --task-ids ... --max-concurrency 1 --save-to <run_name> --auto-resume

and runs it inside the tau2-bench checkout with ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8``.

RQ2 conditions (``tau-multilingual`` branch)
--------------------------------------------
A config may add a top-level ``"conditions"`` list. The grid is then simulators x
conditions, one ``tau2 run`` (one results.json) per cell. Each condition names a task set
(``retail_hi``), the suffix its task ids carry (``_hi``), a ``user_persona_id`` and an
optional ``text_input_style``, plus its own ``tokens_per_task_estimate`` (Hindi costs about
3.5x English) and optional ``extra_tau2_args``. Base task ids come from the usual
``"tasks"`` block (split / ``exclude_nl_assertions`` / ``limit``, read from ``tau2_dir``),
and the suffix is appended per condition (``"limit": 12`` keeps the first 12 ids after the
NL filter, in split-file order, so a pilot is a prefix of the full grid). Optional
``"enabled": false`` keeps a condition in the config without running it (select it with
``--condition NAME``). A condition cell adds, right after ``--domain``::

    --task-set-name retail_hi --user-persona-id hi --text-input-style romanized
    --no-llm-communicate-judge --target-language-directive-version v3
    --agent-caller-locale-context

and ``--workers 0`` after ``--max-concurrency`` (see below). ``--no-llm-communicate-judge``
is always passed: the branch's default ("auto") scores ``communicate_info`` with an LLM
judge for non-English runs only, which would make the English and Hindi arms differ in
their scorer. The directive version and the caller-locale context are pinned at the
branch's @8ff48b8 defaults, so a branch update cannot change the simulator's language
directive or the agent prompt between chunks. In a condition grid, ``extra_tau2_args``
(top level or per condition) may not set any of these flags, nor the ones run_grid sets
itself (``--seed``, ``--num-trials``, ``--domain``, ``--agent-llm*``, ``--user-llm*``,
``--max-concurrency``, ``--task-ids``, ``--task-subset``, ``--num-tasks``,
``--task-split-name``, ``--save-to``), and may not abbreviate an option: tau2's argparse
expands unique prefixes (``--text-input`` is ``--text-input-style``) and the last value
wins, so an abbreviation would silently override the condition. Configs without
``"conditions"`` produce exactly the commands and run names they produced before.

Design decisions
----------------
* **One results.json per (agent, simulator, task set, trials, seed).** The run name is a
  deterministic function of the config, so re-running the same command resumes the same
  run (``--auto-resume``): finished simulations are skipped and ``infrastructure_error``
  simulations are retried by tau2. The default template includes ``{cfg}``, a short hash
  of the agent and simulator LLM strings and llm_args and the domain, so changing a
  model or a temperature gives a new run. tau2's ``--auto-resume`` does not stop at a
  config change: on ``main`` it silently keeps the OLD ``info`` block; on
  ``tau-multilingual`` it logs a warning, REWRITES ``info`` to the new config, keeps the
  old simulations as done and moves the old header to ``info_history``. Either way one
  results.json would hold two regimes under one header, so an existing results.json is
  checked before every run: a different agent/simulator llm or llm_args, num_trials,
  seed or domain, a file that already mixes seeds, or a non-empty ``info_history`` is
  refused. Condition cells are also checked for the branch settings (task set, persona,
  style, judge mode, directive version, caller-locale context, ``max_steps``,
  ``max_errors``, ``timeout``, ``scores``, text noise), the user-simulator guidelines
  text and ``git_commit`` against the checkout (:func:`branch_setting_mismatches`).
* **Resume bookkeeping mirrors tau2.** tau2 skips ``(trial, task_id, seed)`` triples whose
  simulation finished, with per-trial seeds drawn as ``random.seed(seed)`` then
  ``randint(0, 1000000)`` per trial (``tau2/runner/batch.py``). :func:`trial_seeds`
  reproduces that, so "already done" counts match what tau2 will actually skip.
* **NL-assertion judge preflight.** tau2 scores a task's ``nl_assertions`` with a
  hard-coded LLM judge (``DEFAULT_LLM_NL_ASSERTIONS`` in ``tau2/config.py``). If its API
  key is missing, every such task ends in ``infrastructure_error`` AFTER the whole
  conversation ran, so simulator tokens are spent for nothing, again on every resume.
  The script refuses to run such tasks when the judge's key variable is absent from
  both the environment and ``tau2-bench/.env`` (only the variable NAME is checked,
  values are never read into output), unless ``--allow-missing-judge-key``. A config
  can drop those tasks instead with ``"tasks": {..., "exclude_nl_assertions": true}``.
* **Chunks are cumulative prefixes.** tau2's resume logic accepts *added* tasks but
  refuses *removed* ones, so ``--task-chunk K/N`` passes the first K of N contiguous chunks
  of the task list. Already finished simulations are skipped by tau2, so day K only costs
  the new chunk (plus any infra-error retries).
* **Budget guard.** The planned simulator cost is
  ``remaining_simulations * tokens_per_task_estimate`` (remaining = not yet finished in the
  existing results.json). A simulator whose plan exceeds its ``daily_token_budget`` (or
  optional ``daily_request_budget``) is refused unless ``--force``; the refusal suggests the
  smallest N for ``--task-chunk K/N`` that fits. The guard is an estimate: it does not
  know what else consumed the same provider quota today.
* **No secrets in configs.** API keys come from tau2-bench's ``.env``. The config is
  scanned for secret-looking values and rejected if one is found; the only allowed
  "key" values are dummies such as ``"local"`` for the llama.cpp server.
* **Conditions share a simulator's quota.** Cells that use the same simulator model draw
  on one provider daily quota, so with conditions the guard sums the planned cost of all
  of that simulator's cells (each with its condition's per-simulation estimate) and
  refuses them together; the hint gives ``--task-chunk`` for all conditions at once and
  for one condition per run (``--condition NAME``).
* **Condition run names and pairing.** The default template for condition grids adds
  ``{cond}``, and ``{cfg}`` then also hashes the condition's definition (task set,
  suffix, persona, style, its extra args and the config's top-level extra args), so
  every cell has its own run name and editing any extra arg starts a new run; duplicate
  run names are refused. Before planning, each condition's localized task file is
  located in ``tau2_dir`` (``data/tau2/multilingual/*/<domain>_tasks_<suffix>.json``),
  and every ``<id><suffix>`` must exist there with ``evaluation_criteria``,
  ``user_scenario`` and ``initial_state`` identical to base task ``<id>``. That keeps the
  ``exclude_nl_assertions`` filter valid and the arms paired, and it refuses the
  ``*_identity`` packs (other user identities and starting DB, same criteria on some
  tasks).
  A non-default ``text_input_style`` must also be declared by the persona's language
  pack (``pack.yaml``, ``text_input.styles``): tau2 checks that only when it builds each
  simulation, so without this check a dry-run would print a command that cannot work.
* **Same base tasks as ``main``.** With ``"reference_tau2_dir"`` set (the ``main``
  checkout), the domain's ``split_tasks.json`` and ``tasks.json`` in ``tau2_dir`` must
  equal the reference's as parsed JSON, so a base id such as ``"5"`` names the same task
  in RQ1 (``main``) and RQ2 (``tau-multilingual``).
* **``--workers 0`` on condition cells.** On the ``tau-multilingual`` branch ``tau2 run``
  defaults to 8 worker processes for text runs (``DEFAULT_TEXT_WORKERS``), each holding
  ``--max-concurrency`` simulations; on ``main`` the default is 0 (one process). Without
  the pin, ``--max-concurrency 1`` would still put 8 simulations in flight against the
  4-slot llama-server and Groq's per-minute limits. A ``--workers`` value in
  ``extra_tau2_args`` (top level or condition) replaces the pin.

This file is standard-library only so it can run before the package dependencies exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TAU2_DIR = Path("D:/Programming/Reaserch/tau2-bench")
DEFAULT_LOG_DIR = REPO_ROOT / "results" / "logs"
DEFAULT_RUN_NAME_TEMPLATE = "{experiment}_{agent}_{user}_{tasks}_t{trials}_s{seed}_c{cfg}"
DEFAULT_CONDITION_RUN_NAME_TEMPLATE = (
    "{experiment}_{agent}_{user}_{cond}_{tasks}_t{trials}_s{seed}_c{cfg}"
)
DEFAULT_SEED = 300
# tau-multilingual text-input styles (tau2.multilingual.text_input_catalog); None means the
# branch default, native_script.
TEXT_INPUT_STYLES = ("native_script", "romanized", "diacritic_free", "code_mixed")
DEFAULT_TEXT_INPUT_STYLE = "native_script"
NO_COMMUNICATE_JUDGE_FLAG = "--no-llm-communicate-judge"
# info.communicate_judge_mode recorded by the branch when the flag above is passed.
EXACT_COMMUNICATE_JUDGE_MODE = "exact"
CONDITION_WORKERS = "0"
# Pinned on every condition cell, at the tau-multilingual @8ff48b8 defaults, so that a
# branch update cannot change the user-simulator language directive or the agent prompt
# under a resumed run (checked against the stored header by branch_setting_mismatches).
TARGET_LANGUAGE_DIRECTIVE_FLAG = "--target-language-directive-version"
TARGET_LANGUAGE_DIRECTIVE_VERSION = "v3"
CALLER_LOCALE_CONTEXT_FLAG = "--agent-caller-locale-context"
PINNED_CONDITION_FLAGS: tuple[str, ...] = (
    TARGET_LANGUAGE_DIRECTIVE_FLAG,
    TARGET_LANGUAGE_DIRECTIVE_VERSION,
    CALLER_LOCALE_CONTEXT_FLAG,
)
# Set by the condition fields, by the config or by the script itself; not allowed in the
# extra_tau2_args of a condition grid, spelled out or abbreviated (abbreviated_flags).
CONDITION_RESERVED_FLAGS = frozenset(
    {
        "--task-set-name",
        "--user-persona-id",
        "--text-input-style",
        "--llm-communicate-judge",
        NO_COMMUNICATE_JUDGE_FLAG,
        TARGET_LANGUAGE_DIRECTIVE_FLAG,
        CALLER_LOCALE_CONTEXT_FLAG,
        "--no-agent-caller-locale-context",
        "--task-ids",
        "--save-to",
        "--domain",
        "--agent-llm",
        "--agent-llm-args",
        "--user-llm",
        "--user-llm-args",
        "--num-trials",
        "--seed",
        "--max-concurrency",
        "--task-subset",
        "--num-tasks",
        "--task-split-name",
    }
)
# Options a condition grid's extra_tau2_args may set: they replace run_grid's pin
# (--workers) or tau2's default, and the run-setting ones are compared with the stored
# header. Abbreviations of them are refused like abbreviations of reserved flags.
CONDITION_OVERRIDABLE_FLAGS = frozenset(
    {
        "--workers",
        "--max-steps",
        "--max-errors",
        "--timeout",
        "--scores",
        "--text-noise",
        "--text-noise-seed",
    }
)
# tau2 options whose exact names are prefixes of a reserved flag; argparse prefers an
# exact match, so they are not abbreviations (checked with `tau2 run --help` @8ff48b8).
TAU2_PREFIX_OPTIONS = frozenset({"--agent", "--user", "--user-persona"})
# tau-multilingual run defaults (src/tau2/config.py @8ff48b8), used when the checkout's
# config.py cannot be read. The text-mode scores default is decided in tau2/cli.py.
BRANCH_RUN_DEFAULTS: dict[str, Any] = {"max_steps": 200, "max_errors": 10, "timeout": 2400.0}
BRANCH_DEFAULT_CONSTANTS = {
    "max_steps": ("DEFAULT_MAX_STEPS", int),
    "max_errors": ("DEFAULT_MAX_ERRORS", int),
    "timeout": ("DEFAULT_TIMEOUT_SECONDS", float),
}
TEXT_DEFAULT_SCORES = ("reward",)
TEXT_ALL_SCORES = ("nativeness", "quality", "reward")
TEXT_NOISE_DEFAULT_SEED = 42
# User-simulator guidelines files of the checkout; tau2 records the text of the one it
# used in info.user_info.global_simulation_guidelines.
GUIDELINE_FILES = ("simulation_guidelines.md", "simulation_guidelines_tools.md")
DEFAULT_MAX_CONCURRENCY = 1
INFRA_ERROR = "infrastructure_error"
RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SUBPROCESS_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
# Per-HTTP-request timeout read by litellm (constants.request_timeout, default 6000 s).
# A request hung by laptop sleep or a dead connection then fails after this many seconds and is
# retried by litellm, instead of blocking a simulation for hours (seen 2026-10-19: one
# simulation stalled ~24 h). Deliberately NOT tau2's --timeout: that ends the simulation as
# termination "timeout", which tau2 grades as a failure (biasing pass rates), and it is only
# checked between steps, so it cannot interrupt a hung request. Not part of the run-name hash
# because it does not change any outcome; a value already set in the environment wins.
DEFAULT_REQUEST_TIMEOUT_S = "300"
# tau2's judge for NL assertions (tau2/config.py); read from the checkout when possible.
FALLBACK_NL_JUDGE_MODEL = "gpt-4.1-2025-04-14"
NL_JUDGE_RE = re.compile(r"""^DEFAULT_LLM_NL_ASSERTIONS\s*=\s*["']([^"']+)["']""", re.MULTILINE)
# litellm provider prefix -> API key variable. No prefix means OpenAI.
PROVIDER_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}
TAU2_TRIAL_SEED_MAX = 1_000_000

# Values that may legitimately sit under a "*key*" field: dummies for local servers.
DUMMY_SECRET_VALUES = frozenset({"local", "none", "dummy", "empty", "sk-no-key-required", ""})
SECRET_FIELD_RE = re.compile(r"(api[_-]?key|token|secret|password)", re.IGNORECASE)
# Prefixes of real provider keys (Groq, OpenAI, Google, Anthropic, Hugging Face, ...).
SECRET_VALUE_RE = re.compile(r"^(gsk_|sk-(?!no-key)|sk_|AIza|hf_|xai-|ghp_|github_pat_)")


# --------------------------------------------------------------------------------------
# Config model
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSpec:
    """The agent under test (one per grid)."""

    name: str
    llm: str
    llm_args: dict[str, Any]


@dataclass(frozen=True)
class SimulatorSpec:
    """One user simulator and its daily provider budget."""

    name: str
    llm: str
    llm_args: dict[str, Any]
    daily_token_budget: int | None
    tokens_per_task_estimate: int
    daily_request_budget: int | None = None
    requests_per_task_estimate: int | None = None
    enabled: bool = True
    notes: str = ""


@dataclass(frozen=True)
class TaskSource:
    """Either an explicit ordered list of task ids or a named split of split_tasks.json.

    ``exclude_nl_assertions`` drops tasks whose ``evaluation_criteria.nl_assertions`` is
    non-empty (they need tau2's LLM judge), BEFORE ``limit`` is applied.
    """

    ids: tuple[str, ...] | None = None
    split: str | None = None
    limit: int | None = None
    exclude_nl_assertions: bool = False


@dataclass(frozen=True)
class ConditionSpec:
    """One user-language condition of an RQ2 grid (``tau-multilingual`` branch).

    ``tokens_per_task_estimate`` replaces the simulator's estimate for this condition's
    cells (Hindi users cost about 3.5x English ones); ``requests_per_task_estimate`` does
    the same for the request budget when set, else the simulator's value is used.
    """

    name: str
    task_set_name: str
    task_id_suffix: str
    tokens_per_task_estimate: int
    user_persona_id: str | None = None
    text_input_style: str | None = None
    requests_per_task_estimate: int | None = None
    extra_tau2_args: tuple[str, ...] = ()
    enabled: bool = True
    notes: str = ""


@dataclass(frozen=True)
class GridConfig:
    """Validated grid configuration (see configs/*.json)."""

    experiment: str
    domain: str
    agent: AgentSpec
    simulators: tuple[SimulatorSpec, ...]
    tasks: TaskSource
    num_trials: int
    seed: int = DEFAULT_SEED
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    run_name_template: str = DEFAULT_RUN_NAME_TEMPLATE
    tau2_dir: str | None = None
    extra_tau2_args: tuple[str, ...] = ()
    description: str = ""
    conditions: tuple[ConditionSpec, ...] = ()
    reference_tau2_dir: str | None = None


class ConfigError(ValueError):
    """Raised for an invalid or unsafe grid configuration."""


def find_secret_like_values(obj: Any, path: str = "$") -> list[str]:
    """Return JSON paths of values that look like real credentials.

    A value is flagged when it sits under a key whose name looks like a credential field
    (``api_key``, ``token``, ...) and is not a known dummy such as ``"local"``, or when
    any string value starts with a known provider-key prefix (``gsk_``, ``sk-``, ...).
    """
    hits: list[str] = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            child = f"{path}.{key}"
            if (
                SECRET_FIELD_RE.search(str(key))
                and isinstance(value, str)
                and value.strip().lower() not in DUMMY_SECRET_VALUES
            ):
                hits.append(child)
            else:
                hits.extend(find_secret_like_values(value, child))
    elif isinstance(obj, list | tuple):
        for i, value in enumerate(obj):
            hits.extend(find_secret_like_values(value, f"{path}[{i}]"))
    elif isinstance(obj, str) and SECRET_VALUE_RE.match(obj.strip()):
        hits.append(path)
    return hits


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"{where}: missing required field '{key}'")
    return mapping[key]


def _optional_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{where}: expected a non-negative integer or null, got {value!r}")
    return value


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: expected a positive integer, got {value!r}")
    return value


def _parse_task_source(raw: Mapping[str, Any]) -> TaskSource:
    has_ids, has_split = "ids" in raw, "split" in raw
    if has_ids == has_split:
        raise ConfigError("tasks: give exactly one of 'ids' (list) or 'split' (name)")
    limit = raw.get("limit")
    if limit is not None:
        limit = _positive_int(limit, "tasks.limit")
    exclude_nl = raw.get("exclude_nl_assertions", False)
    if not isinstance(exclude_nl, bool):
        raise ConfigError("tasks.exclude_nl_assertions: expected true or false")
    if has_ids:
        ids = raw["ids"]
        if not isinstance(ids, list) or not ids:
            raise ConfigError("tasks.ids: expected a non-empty list")
        return TaskSource(
            ids=tuple(str(i) for i in ids), limit=limit, exclude_nl_assertions=exclude_nl
        )
    split = raw["split"]
    if not isinstance(split, str) or not split:
        raise ConfigError("tasks.split: expected a split name such as 'train' or 'test'")
    return TaskSource(split=split, limit=limit, exclude_nl_assertions=exclude_nl)


def _string_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise ConfigError(f"{where}: expected a list of strings")
    return tuple(value)


def flag_names(args: Iterable[str]) -> set[str]:
    """Option names in a CLI argument list (``--flag=value`` counts as ``--flag``)."""
    return {a.split("=", 1)[0] for a in args if a.startswith("--")}


def abbreviated_flags(args: Iterable[str]) -> list[str]:
    """Options in ``args`` that tau2 would expand to a flag run_grid sets or checks.

    tau2 parses with argparse's default ``allow_abbrev=True``: ``--text-input code_mixed``
    means ``--text-input-style code_mixed`` and, coming last, overrides the condition's
    style. Reported: every option that is a proper prefix of a reserved or overridable
    flag (except tau2 options whose exact names are such prefixes, e.g. ``--agent``), and
    every single-dash short option (``-d`` is ``--domain``).
    """
    managed = CONDITION_RESERVED_FLAGS | CONDITION_OVERRIDABLE_FLAGS
    found: set[str] = set()
    for arg in args:
        if re.match(r"-[A-Za-z]", arg):
            found.add(arg[:2])
            continue
        if not arg.startswith("--"):
            continue
        name = arg.split("=", 1)[0]
        if name in managed or name in TAU2_PREFIX_OPTIONS:
            continue
        if any(flag.startswith(name) for flag in managed):
            found.add(name)
    return sorted(found)


def _check_condition_extra_args(extra: Sequence[str], where: str, reserved_note: str) -> None:
    """Refuse reserved and abbreviated options in a condition grid's extra_tau2_args."""
    reserved = sorted(flag_names(extra) & CONDITION_RESERVED_FLAGS)
    if reserved:
        raise ConfigError(f"{where}: {reserved} {reserved_note}")
    abbreviated = abbreviated_flags(extra)
    if abbreviated:
        raise ConfigError(
            f"{where}: {abbreviated} abbreviate an option that run_grid sets or checks "
            "(tau2's argparse expands unique prefixes and the last value wins); write the "
            "full long option name"
        )


def _optional_str(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: expected a non-empty string or null")
    return value


def _parse_condition(raw: Any, where: str) -> ConditionSpec:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where}: expected an object")
    name = str(_require(raw, "name", where))
    if not RUN_NAME_RE.fullmatch(name):
        raise ConfigError(f"{where}.name: {name!r} must match {RUN_NAME_RE.pattern}")
    task_set = _require(raw, "task_set_name", where)
    if not isinstance(task_set, str) or not task_set:
        raise ConfigError(f"{where}.task_set_name: expected a task set name such as 'retail_hi'")
    suffix = raw.get("task_id_suffix", "")
    if not isinstance(suffix, str) or not re.fullmatch(r"[A-Za-z0-9_.-]*", suffix):
        raise ConfigError(f"{where}.task_id_suffix: expected a string such as '_hi'")
    persona = _optional_str(raw.get("user_persona_id"), f"{where}.user_persona_id")
    style = _optional_str(raw.get("text_input_style"), f"{where}.text_input_style")
    if style is not None and style not in TEXT_INPUT_STYLES:
        raise ConfigError(f"{where}.text_input_style: {style!r} is not one of {TEXT_INPUT_STYLES}")
    if style not in (None, DEFAULT_TEXT_INPUT_STYLE) and persona is None:
        raise ConfigError(
            f"{where}: text_input_style {style!r} needs a language-pack user_persona_id "
            "(tau2 only accepts non-default styles for a pack that declares them)"
        )
    tpt = _require(raw, "tokens_per_task_estimate", where)
    extra = _string_list(raw.get("extra_tau2_args", []), f"{where}.extra_tau2_args")
    _check_condition_extra_args(
        extra,
        f"{where}.extra_tau2_args",
        "are set by the condition fields or by run_grid itself",
    )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}.enabled: expected true or false")
    return ConditionSpec(
        name=name,
        task_set_name=task_set,
        task_id_suffix=suffix,
        tokens_per_task_estimate=_positive_int(tpt, f"{where}.tokens_per_task_estimate"),
        user_persona_id=persona,
        text_input_style=style,
        requests_per_task_estimate=_optional_int(
            raw.get("requests_per_task_estimate"), f"{where}.requests_per_task_estimate"
        ),
        extra_tau2_args=extra,
        enabled=enabled,
        notes=str(raw.get("notes", "")),
    )


def _parse_conditions(raw: Any, extra: tuple[str, ...]) -> tuple[ConditionSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ConfigError("conditions: expected a non-empty list (or leave the field out)")
    conditions = tuple(_parse_condition(c, f"conditions[{i}]") for i, c in enumerate(raw))
    names = [c.name for c in conditions]
    if len(set(names)) != len(names):
        raise ConfigError(f"conditions: names must be unique, got {names}")
    _check_condition_extra_args(
        extra,
        "extra_tau2_args",
        "are set per condition or by run_grid itself in a condition grid",
    )
    return conditions


def parse_config(raw: Mapping[str, Any]) -> GridConfig:
    """Validate a raw config mapping and convert it to a :class:`GridConfig`."""
    secrets = find_secret_like_values(raw)
    if secrets:
        raise ConfigError(
            "config contains secret-looking values at "
            + ", ".join(secrets)
            + ". Put API keys in tau2-bench/.env, never in configs."
        )

    agent_raw = _require(raw, "agent", "config")
    agent = AgentSpec(
        name=str(_require(agent_raw, "name", "agent")),
        llm=str(_require(agent_raw, "llm", "agent")),
        llm_args=dict(agent_raw.get("llm_args", {"temperature": 0.0})),
    )

    default_tpt = raw.get("tokens_per_task_estimate")
    simulators: list[SimulatorSpec] = []
    for i, sim_raw in enumerate(_require(raw, "simulators", "config")):
        where = f"simulators[{i}]"
        tpt = sim_raw.get("tokens_per_task_estimate", default_tpt)
        if tpt is None:
            raise ConfigError(f"{where}: missing 'tokens_per_task_estimate' (no global default)")
        simulators.append(
            SimulatorSpec(
                name=str(_require(sim_raw, "name", where)),
                llm=str(_require(sim_raw, "llm", where)),
                llm_args=dict(sim_raw.get("llm_args", {"temperature": 0.0})),
                daily_token_budget=_optional_int(
                    sim_raw.get("daily_token_budget"), f"{where}.daily_token_budget"
                ),
                tokens_per_task_estimate=_positive_int(tpt, f"{where}.tokens_per_task_estimate"),
                daily_request_budget=_optional_int(
                    sim_raw.get("daily_request_budget"), f"{where}.daily_request_budget"
                ),
                requests_per_task_estimate=_optional_int(
                    sim_raw.get("requests_per_task_estimate"),
                    f"{where}.requests_per_task_estimate",
                ),
                enabled=bool(sim_raw.get("enabled", True)),
                notes=str(sim_raw.get("notes", "")),
            )
        )
    if not simulators:
        raise ConfigError("simulators: expected at least one simulator")
    names = [s.name for s in simulators]
    if len(set(names)) != len(names):
        raise ConfigError(f"simulators: names must be unique, got {names}")

    extra = raw.get("extra_tau2_args", [])
    if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
        raise ConfigError("extra_tau2_args: expected a list of strings")
    conditions = _parse_conditions(raw.get("conditions"), tuple(extra))
    default_template = (
        DEFAULT_CONDITION_RUN_NAME_TEMPLATE if conditions else DEFAULT_RUN_NAME_TEMPLATE
    )

    return GridConfig(
        experiment=str(_require(raw, "experiment", "config")),
        domain=str(raw.get("domain", "retail")),
        agent=agent,
        simulators=tuple(simulators),
        tasks=_parse_task_source(_require(raw, "tasks", "config")),
        num_trials=_positive_int(_require(raw, "num_trials", "config"), "num_trials"),
        seed=int(raw.get("seed", DEFAULT_SEED)),
        max_concurrency=_positive_int(
            raw.get("max_concurrency", DEFAULT_MAX_CONCURRENCY), "max_concurrency"
        ),
        run_name_template=str(raw.get("run_name_template", default_template)),
        tau2_dir=raw.get("tau2_dir"),
        extra_tau2_args=tuple(extra),
        description=str(raw.get("description", "")),
        conditions=conditions,
        reference_tau2_dir=_optional_str(raw.get("reference_tau2_dir"), "reference_tau2_dir"),
    )


def load_config(path: Path) -> GridConfig:
    """Read and validate a grid config JSON file."""
    with open(path, encoding="utf-8") as fh:
        return parse_config(json.load(fh))


# --------------------------------------------------------------------------------------
# Tasks and chunks
# --------------------------------------------------------------------------------------


def split_file_path(tau2_dir: Path, domain: str) -> Path:
    """Location of tau2's task split file for ``domain``."""
    return tau2_dir / "data" / "tau2" / "domains" / domain / "split_tasks.json"


def _dedupe(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for task_id in ids:
        if task_id not in seen:
            seen.add(task_id)
            out.append(task_id)
    return out


def tasks_file_path(tau2_dir: Path, domain: str) -> Path:
    """Location of tau2's task definitions for ``domain``."""
    return tau2_dir / "data" / "tau2" / "domains" / domain / "tasks.json"


def load_tasks(path: Path) -> list[dict[str, Any]]:
    """Parse a tau2 tasks.json file (a list of task objects)."""
    with open(path, encoding="utf-8") as fh:
        tasks = json.load(fh)
    if not isinstance(tasks, list):
        raise ValueError(f"{path}: expected a JSON list of tasks")
    return tasks


def nl_assertion_task_ids(tasks: Iterable[Mapping[str, Any]]) -> set[str]:
    """Ids of tasks with a non-empty ``evaluation_criteria.nl_assertions`` list.

    tau2's default evaluation (``EvaluationType.ALL``) calls the LLM judge for exactly
    these tasks (the NL evaluator returns early when the list is empty).
    """
    out: set[str] = set()
    for task in tasks:
        criteria = task.get("evaluation_criteria") or {}
        if criteria.get("nl_assertions"):
            out.add(str(task.get("id")))
    return out


def resolve_task_ids(
    source: TaskSource,
    splits: Mapping[str, Sequence[Any]],
    nl_task_ids: set[str] | None = None,
) -> list[str]:
    """Return the ordered, de-duplicated task ids described by ``source``.

    ``splits`` is the parsed split_tasks.json (``{"train": [...], "test": [...],
    "base": [...]}``). Explicit ids are validated against the ``base`` split when present.
    With ``source.exclude_nl_assertions``, ids in ``nl_task_ids`` (see
    :func:`nl_assertion_task_ids`) are removed first. ``limit`` then keeps the first N
    ids, in file/config order.
    """
    if source.ids is not None:
        ids = _dedupe(source.ids)
        if "base" in splits:
            known = {str(t) for t in splits["base"]}
            unknown = [t for t in ids if t not in known]
            if unknown:
                raise ConfigError(f"tasks.ids: unknown task ids {unknown}")
    else:
        assert source.split is not None
        if source.split not in splits:
            raise ConfigError(
                f"tasks.split: '{source.split}' not in split file (have {sorted(splits)})"
            )
        ids = _dedupe(str(t) for t in splits[source.split])
    if source.exclude_nl_assertions:
        if nl_task_ids is None:
            raise ConfigError("tasks.exclude_nl_assertions needs the domain's tasks.json")
        ids = [t for t in ids if t not in nl_task_ids]
    if source.limit is not None:
        ids = ids[: source.limit]
    if not ids:
        raise ConfigError("tasks: resolved to an empty task list")
    return ids


def load_splits(path: Path) -> dict[str, list[Any]]:
    """Parse a tau2 split_tasks.json file."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def condition_task_ids(base_ids: Sequence[str], condition: ConditionSpec | None) -> list[str]:
    """Task ids as the condition's task set names them: ``<base id><suffix>``.

    Without a condition the base ids are returned unchanged (``"5"`` -> ``"5"``); for
    ``task_id_suffix="_hi"``, ``"5"`` -> ``"5_hi"``. Order is kept, so chunks line up
    across conditions.
    """
    if condition is None:
        return list(base_ids)
    return [f"{t}{condition.task_id_suffix}" for t in base_ids]


def task_set_file_path(tau2_dir: Path, domain: str, task_set_name: str) -> Path:
    """The tasks file behind ``--task-set-name`` on the ``tau-multilingual`` branch.

    The domain's own set (``retail``) is ``data/tau2/domains/retail/tasks.json``. A
    localized set ``<domain>_<suffix>`` is registered by file name convention from
    ``data/tau2/multilingual/<lang>/<domain>_tasks_<suffix>.json``
    (``tau2.multilingual.task_sets``), e.g. ``retail_hi`` -> ``hi/retail_tasks_hi.json``.
    Raises :class:`ConfigError` unless exactly one such file exists, which is also how a
    ``tau2_dir`` that is not the multilingual checkout is caught.
    """
    if task_set_name == domain:
        return tasks_file_path(tau2_dir, domain)
    prefix = f"{domain}_"
    if not task_set_name.startswith(prefix) or task_set_name == prefix:
        raise ConfigError(f"task set {task_set_name!r} is not a '{domain}' task set")
    pattern = f"*/{domain}_tasks_{task_set_name[len(prefix) :]}.json"
    matches = sorted((tau2_dir / "data" / "tau2" / "multilingual").glob(pattern))
    if len(matches) != 1:
        raise ConfigError(
            f"task set {task_set_name!r}: expected one file data/tau2/multilingual/{pattern} "
            f"under {tau2_dir}, found {len(matches)}. Is tau2_dir the tau-multilingual checkout?"
        )
    return matches[0]


# Task fields that must be identical for a condition task to pair with its base task:
# what the simulated user is told, the starting database and how the run is scored.
PAIRED_TASK_FIELDS = ("evaluation_criteria", "user_scenario", "initial_state")


def pairing_problems(
    base_tasks: Iterable[Mapping[str, Any]],
    condition_tasks: Iterable[Mapping[str, Any]],
    base_ids: Sequence[str],
    suffix: str,
) -> list[str]:
    """Reasons why condition task ``<id><suffix>`` cannot be paired with base task ``<id>``.

    A paired comparison needs every planned id to exist in the condition's task set with
    the same :data:`PAIRED_TASK_FIELDS` as the base task: the same ``user_scenario`` and
    ``initial_state`` (the ``*_identity`` packs change the user's name, address and the
    starting DB, PREREGISTRATION section 4) and the same ``evaluation_criteria`` (which
    also keeps the ``exclude_nl_assertions`` filter, computed on the base tasks, valid).
    Metadata such as ``description`` and ``issues`` is not compared. Empty list means
    every id pairs.
    """
    base = {str(t.get("id")): t for t in base_tasks}
    localized = {str(t.get("id")): t for t in condition_tasks}
    missing: list[str] = []
    differ: dict[str, list[str]] = {}
    for task_id in base_ids:
        cond_id = f"{task_id}{suffix}"
        if cond_id not in localized:
            missing.append(cond_id)
            continue
        base_task = base.get(task_id) or {}
        fields = [
            name
            for name in PAIRED_TASK_FIELDS
            if localized[cond_id].get(name) != base_task.get(name)
        ]
        if fields:
            differ[cond_id] = fields
    problems: list[str] = []
    if missing:
        problems.append(f"{len(missing)} id(s) missing from the task set: {missing[:12]}")
    if differ:
        fields_seen = [f for f in PAIRED_TASK_FIELDS if any(f in v for v in differ.values())]
        problems.append(
            f"{len(differ)} id(s) have {', '.join(fields_seen)} unlike the base task: "
            f"{list(differ)[:12]}"
        )
    return problems


def base_task_file_differences(tau2_dir: Path, reference_dir: Path, domain: str) -> list[str]:
    """Base task files of ``domain`` whose parsed JSON differs between two tau2 checkouts.

    Compares ``split_tasks.json`` and ``tasks.json``. An RQ2 grid runs on the
    ``tau-multilingual`` checkout but selects its tasks the way RQ1 does on ``main``, so a
    base id must name the same task in both. Parsed JSON is compared, so line endings
    and formatting do not matter. A missing file raises :class:`OSError`.
    """
    differ: list[str] = []
    for path_of in (split_file_path, tasks_file_path):
        here, there = path_of(tau2_dir, domain), path_of(reference_dir, domain)
        with open(here, encoding="utf-8") as fh_here, open(there, encoding="utf-8") as fh_there:
            if json.load(fh_here) != json.load(fh_there):
                differ.append(here.name)
    return differ


def multilingual_dir(tau2_dir: Path) -> Path:
    """Root of the language packs on the ``tau-multilingual`` branch."""
    return tau2_dir / "data" / "tau2" / "multilingual"


TEXT_STYLE_LINE_RE = re.compile(r"^\s*-\s*style:\s*['\"]?([A-Za-z_]+)['\"]?\s*(#.*)?$")


def declared_text_input_styles(pack_yaml: str) -> set[str]:
    """Styles listed under the top-level ``text_input:`` block of a language ``pack.yaml``.

    A line scanner rather than a YAML parser (this script is standard-library only): the
    block runs from ``text_input:`` to the next top-level key, and each ``- style: NAME``
    line in it declares one style (``hi`` -> ``{"romanized", "code_mixed"}``).
    """
    styles: set[str] = set()
    inside = False
    for line in pack_yaml.splitlines():
        if line and not line[0].isspace() and not line.startswith(("#", "-")):
            inside = line.split(":", 1)[0].strip() == "text_input"
            continue
        match = TEXT_STYLE_LINE_RE.match(line) if inside else None
        if match:
            styles.add(match.group(1))
    return styles


def persona_pack_file(tau2_dir: Path, persona_id: str) -> Path | None:
    """``pack.yaml`` of the language pack that a ``--user-persona-id`` value belongs to.

    A bare language code (``hi``) is the pack's directory name; a pack persona id
    (``rishika_hindi_v1``) is found by its ``persona_id:`` line. None when neither
    matches (for example a stock English voice persona, which has no pack).
    """
    root = multilingual_dir(tau2_dir)
    direct = root / persona_id / "pack.yaml"
    if direct.is_file():
        return direct
    pattern = re.compile(
        rf"^\s*-?\s*persona_id:\s*['\"]?{re.escape(persona_id)}['\"]?\s*$", re.MULTILINE
    )
    for pack in sorted(root.glob("*/pack.yaml")):
        if pattern.search(pack.read_text(encoding="utf-8")):
            return pack
    return None


def text_style_problem(tau2_dir: Path, condition: ConditionSpec) -> str | None:
    """Why tau2 would reject the condition's ``text_input_style`` (None if it is fine).

    tau2 checks the style only when it builds each simulation
    (``tau2.multilingual.registry.resolve_text_input_style``), so a dry-run would print
    a command that cannot work. A non-default style needs a persona whose language pack
    declares it under ``text_input.styles`` (``hi``: romanized, code_mixed).
    """
    style = condition.text_input_style
    if style in (None, DEFAULT_TEXT_INPUT_STYLE):
        return None
    persona = condition.user_persona_id or ""
    pack = persona_pack_file(tau2_dir, persona) if persona else None
    if pack is None:
        return (
            f"text_input_style {style!r} needs a language-pack persona, but user_persona_id "
            f"{persona!r} names no pack under {multilingual_dir(tau2_dir)}"
        )
    declared = declared_text_input_styles(pack.read_text(encoding="utf-8"))
    if style not in declared:
        return (
            f"language pack {pack.parent.name!r} declares text_input styles "
            f"{sorted(declared) or 'none'}, not {style!r}"
        )
    return None


def parse_chunk(spec: str) -> tuple[int, int]:
    """Parse ``"K/N"`` (1-based chunk K of N) into ``(K, N)``."""
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", spec)
    if not match:
        raise ValueError(f"--task-chunk expects K/N such as 2/5, got {spec!r}")
    k, n = int(match.group(1)), int(match.group(2))
    if n < 1 or not 1 <= k <= n:
        raise ValueError(f"--task-chunk needs 1 <= K <= N, got {k}/{n}")
    return k, n


def chunk_bounds(n_items: int, n_chunks: int) -> list[tuple[int, int]]:
    """Split ``range(n_items)`` into ``n_chunks`` contiguous near-equal ``[start, end)`` parts.

    The first ``n_items % n_chunks`` chunks get one extra item, e.g. 10 into 3 -> 4, 3, 3.
    """
    if n_chunks < 1:
        raise ValueError("n_chunks must be >= 1")
    base, extra = divmod(n_items, n_chunks)
    bounds, start = [], 0
    for i in range(n_chunks):
        end = start + base + (1 if i < extra else 0)
        bounds.append((start, end))
        start = end
    return bounds


def select_chunk(task_ids: Sequence[str], k: int, n: int) -> tuple[list[str], list[str]]:
    """Return ``(cumulative_ids, new_ids)`` for chunk K of N.

    ``cumulative_ids`` (chunks 1..K) is what is passed to tau2, because tau2 refuses to
    resume a run whose task set shrank; ``new_ids`` is chunk K alone.
    """
    start, end = chunk_bounds(len(task_ids), n)[k - 1]
    return list(task_ids[:end]), list(task_ids[start:end])


# --------------------------------------------------------------------------------------
# Run names and commands
# --------------------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Lower-case ``text`` and collapse every run of non ``[a-z0-9.]`` characters to ``-``."""
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-.")


def tasks_tag(source: TaskSource) -> str:
    """Short deterministic label for a task source (used in run names).

    ``split=train`` -> ``train``; with ``limit=10`` -> ``train-first10``; explicit ids ->
    ``ids-<first 8 hex chars of sha1 of the comma-joined ids in config order>``.
    Chunking does not change the tag: all chunks of a grid share one run.
    """
    if source.ids is not None:
        digest = hashlib.sha1(",".join(source.ids).encode("utf-8")).hexdigest()[:8]
        tag = f"ids-{digest}"
    else:
        assert source.split is not None
        tag = slugify(source.split)
    if source.exclude_nl_assertions:
        tag += "-nonl"
    if source.limit is not None:
        tag += f"-first{source.limit}"
    return tag


def condition_definition(
    condition: ConditionSpec, top_extra_tau2_args: Sequence[str] = ()
) -> dict[str, Any]:
    """The settings that define what a condition runs (hashed into its run name).

    Budget estimates, notes and ``enabled`` are left out: changing them does not change
    the simulations. The communicate-judge mode is included because it is always pinned.
    The config's top-level ``extra_tau2_args`` reach every condition cell's command, so
    they are included as ``top_extra_tau2_args`` when non-empty (an empty list adds no
    key, which keeps the names of configs without top-level extra args unchanged). The
    later pins (:data:`PINNED_CONDITION_FLAGS`) are not hashed, for the same reason; the
    resume guard compares them with the stored header instead.
    """
    definition: dict[str, Any] = {
        "communicate_judge": EXACT_COMMUNICATE_JUDGE_MODE,
        "extra_tau2_args": list(condition.extra_tau2_args),
        "task_id_suffix": condition.task_id_suffix,
        "task_set_name": condition.task_set_name,
        "text_input_style": condition.text_input_style,
        "user_persona_id": condition.user_persona_id,
    }
    if top_extra_tau2_args:
        definition["top_extra_tau2_args"] = list(top_extra_tau2_args)
    return definition


def config_hash(
    config: GridConfig, simulator: SimulatorSpec, condition: ConditionSpec | None = None
) -> str:
    """First 6 hex chars of sha1 over the model-defining settings of one grid cell.

    Covers the agent and simulator ``llm`` strings and ``llm_args`` and the domain, so a
    different model, temperature or api_base gives a different run name. A condition
    cell also hashes :func:`condition_definition` (with the config's top-level
    ``extra_tau2_args``); without a condition the payload (and so every existing run
    name) is exactly what it was before conditions existed.
    """
    fields: dict[str, Any] = {
        "agent_llm": config.agent.llm,
        "agent_llm_args": config.agent.llm_args,
        "domain": config.domain,
        "user_llm": simulator.llm,
        "user_llm_args": simulator.llm_args,
    }
    if condition is not None:
        fields["condition"] = condition_definition(condition, config.extra_tau2_args)
    return hashlib.sha1(compact_json(fields).encode("utf-8")).hexdigest()[:6]


def make_run_name(
    config: GridConfig, simulator: SimulatorSpec, condition: ConditionSpec | None = None
) -> str:
    """Deterministic tau2 ``--save-to`` name for one (agent, simulator[, condition]) cell.

    Template fields: ``{experiment} {agent} {user} {cond} {tasks} {trials} {seed}
    {domain} {cfg}`` (``cfg`` = :func:`config_hash`; ``cond`` is the slugified condition
    name, empty without a condition). A template without ``{cfg}`` (such as the legacy
    pilot one) is protected only by the existing-run check in :func:`main`.
    """
    try:
        name = config.run_name_template.format(
            experiment=slugify(config.experiment),
            agent=slugify(config.agent.name),
            user=slugify(simulator.name),
            cond="" if condition is None else slugify(condition.name),
            tasks=tasks_tag(config.tasks),
            trials=config.num_trials,
            seed=config.seed,
            domain=slugify(config.domain),
            cfg=config_hash(config, simulator, condition),
        )
    except KeyError as exc:
        raise ConfigError(f"run_name_template: unknown field {exc}") from exc
    if not RUN_NAME_RE.fullmatch(name):
        raise ConfigError(f"run name {name!r} has characters unsafe for a directory name")
    return name


def selected_cells(
    config: GridConfig,
    simulators: Sequence[SimulatorSpec],
    conditions: Sequence[ConditionSpec | None],
) -> list[tuple[SimulatorSpec, ConditionSpec | None, str]]:
    """``(simulator, condition, run_name)`` for every planned cell, simulator-major.

    Raises :class:`ConfigError` when two cells would share a run name (for example a
    custom template without ``{cond}``): tau2 would resume one into the other.
    """
    cells = [(s, c, make_run_name(config, s, c)) for s in simulators for c in conditions]
    seen: dict[str, str] = {}
    for sim, cond, name in cells:
        label = sim.name if cond is None else f"{sim.name}/{cond.name}"
        if name in seen:
            raise ConfigError(
                f"run name {name!r} is shared by cells {seen[name]} and {label}; "
                "add {cond} (and {cfg}) to run_name_template"
            )
        seen[name] = label
    return cells


def compact_json(obj: Mapping[str, Any]) -> str:
    """JSON with stable key order and no spaces, for CLI arguments."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def condition_flags(condition: ConditionSpec) -> list[str]:
    """The tau-multilingual flags that select one condition (inserted after ``--domain``).

    Task set, persona and style come from the condition; the communicate judge, the
    language-directive version and the caller-locale context are pinned for every cell
    (:data:`PINNED_CONDITION_FLAGS`).
    """
    flags = ["--task-set-name", condition.task_set_name]
    if condition.user_persona_id is not None:
        flags += ["--user-persona-id", condition.user_persona_id]
    if condition.text_input_style is not None:
        flags += ["--text-input-style", condition.text_input_style]
    flags.append(NO_COMMUNICATE_JUDGE_FLAG)
    flags += PINNED_CONDITION_FLAGS
    return flags


def build_tau2_command(
    config: GridConfig,
    simulator: SimulatorSpec,
    task_ids: Sequence[str],
    run_name: str,
    condition: ConditionSpec | None = None,
) -> list[str]:
    """Exact argv list for one ``tau2 run`` invocation (run with cwd = tau2-bench).

    ``task_ids`` are passed as given; for a condition cell they must already carry the
    suffix (:func:`condition_task_ids`). A condition cell adds :func:`condition_flags`
    after ``--domain``, ``--workers 0`` after ``--max-concurrency`` (unless an extra arg
    sets ``--workers``), and its own ``extra_tau2_args`` after the config's.
    """
    extra = list(config.extra_tau2_args)
    if condition is not None:
        extra += condition.extra_tau2_args
    workers: list[str] = []
    if condition is not None and "--workers" not in flag_names(extra):
        workers = ["--workers", CONDITION_WORKERS]
    return [
        "uv",
        "run",
        "tau2",
        "run",
        "--domain",
        config.domain,
        *([] if condition is None else condition_flags(condition)),
        "--agent-llm",
        config.agent.llm,
        "--agent-llm-args",
        compact_json(config.agent.llm_args),
        "--user-llm",
        simulator.llm,
        "--user-llm-args",
        compact_json(simulator.llm_args),
        "--num-trials",
        str(config.num_trials),
        "--seed",
        str(config.seed),
        "--task-ids",
        *task_ids,
        "--max-concurrency",
        str(config.max_concurrency),
        *workers,
        "--save-to",
        run_name,
        "--auto-resume",
        *extra,
    ]


def subprocess_env(base: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``base`` with UTF-8 forced and a litellm request timeout for the child process."""
    env = dict(base)
    env.update(SUBPROCESS_ENV)
    env.setdefault("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT_S)
    return env


# --------------------------------------------------------------------------------------
# Existing results and budget
# --------------------------------------------------------------------------------------


def results_path(tau2_dir: Path, run_name: str) -> Path:
    """Where tau2 writes the results of ``--save-to run_name``."""
    return tau2_dir / "data" / "simulations" / run_name / "results.json"


def load_simulations(path: Path) -> list[dict[str, Any]]:
    """Simulations stored for a run (empty if the run does not exist yet).

    Supports both the single-file format and tau2's directory format, in which
    results.json holds metadata and each simulation lives in ``simulations/<id>.json``.
    """
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    sims = data.get("simulations") or []
    sims_dir = path.parent / "simulations"
    if not sims and sims_dir.is_dir():
        for sim_file in sorted(sims_dir.glob("*.json")):
            with open(sim_file, encoding="utf-8") as fh:
                sims.append(json.load(fh))
    return sims


def load_run_info(path: Path) -> dict[str, Any] | None:
    """The ``info`` block of an existing results.json (None if the run does not exist)."""
    return load_run_header(path)[0]


def load_run_header(path: Path) -> tuple[dict[str, Any] | None, list[Any]]:
    """``(info, info_history)`` of an existing results.json (``(None, [])`` if absent).

    ``info_history`` is written by the ``tau-multilingual`` branch only: when
    ``--auto-resume`` proceeds under a changed config it rewrites ``info`` to the new
    config and appends the displaced header there (``tau2/runner/checkpoint.py``).
    """
    if not path.exists():
        return None, []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("info") or {}, list(data.get("info_history") or [])


@dataclass(frozen=True)
class CheckoutFacts:
    """What the tau2 checkout itself says a condition cell will run with.

    ``defaults`` are the run-setting defaults (``max_steps``, ``max_errors``, ``timeout``)
    read from ``src/tau2/config.py``; ``guidelines`` maps a user-simulator guidelines
    file name to its text; ``git_commit`` is the checkout's HEAD (None when unknown).
    """

    defaults: Mapping[str, Any] = field(default_factory=lambda: dict(BRANCH_RUN_DEFAULTS))
    guidelines: Mapping[str, str] = field(default_factory=dict)
    git_commit: str | None = None


def checkout_run_defaults(tau2_dir: Path) -> dict[str, Any]:
    """``max_steps`` / ``max_errors`` / ``timeout`` defaults of a tau2 checkout.

    Read from the ``DEFAULT_*`` constants in ``src/tau2/config.py``; each one that cannot
    be read keeps its tau-multilingual @8ff48b8 value (:data:`BRANCH_RUN_DEFAULTS`).
    """
    defaults = dict(BRANCH_RUN_DEFAULTS)
    try:
        text = (tau2_dir / "src" / "tau2" / "config.py").read_text(encoding="utf-8")
    except OSError:
        return defaults
    for key, (name, kind) in BRANCH_DEFAULT_CONSTANTS.items():
        match = re.search(rf"^{name}\s*=\s*([0-9][0-9_.]*)\s*(?:#.*)?$", text, re.MULTILINE)
        if match:
            try:
                defaults[key] = kind(match.group(1))
            except ValueError:
                pass
    return defaults


def checkout_commit(tau2_dir: Path) -> str | None:
    """HEAD commit of the git checkout ``tau2_dir``, read from ``.git`` (None if unknown).

    tau2 records ``git rev-parse HEAD`` of its working directory as ``info.git_commit``.
    Handles a branch ref (loose or in ``packed-refs``) and a detached HEAD; anything else
    (e.g. a worktree whose ``.git`` is a file) gives None, and the check is skipped.
    """
    git = tau2_dir / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    sha_re = re.compile(r"[0-9a-f]{40}")
    if not head.startswith("ref:"):
        return head if sha_re.fullmatch(head) else None
    ref = head[len("ref:") :].strip()
    try:
        sha = (git / ref).read_text(encoding="utf-8").strip()
        return sha if sha_re.fullmatch(sha) else None
    except OSError:
        pass
    try:
        for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref and sha_re.fullmatch(parts[0]):
                return parts[0]
    except OSError:
        pass
    return None


def checkout_guidelines(tau2_dir: Path) -> dict[str, str]:
    """Text of the checkout's user-simulator guidelines files that exist (name -> text).

    Read in text mode, as tau2 reads them, so line endings match the stored copy.
    """
    root = tau2_dir / "data" / "tau2" / "user_simulator"
    texts: dict[str, str] = {}
    for name in GUIDELINE_FILES:
        try:
            texts[name] = (root / name).read_text(encoding="utf-8")
        except OSError:
            continue
    return texts


def read_checkout_facts(tau2_dir: Path) -> CheckoutFacts:
    """:class:`CheckoutFacts` of ``tau2_dir`` (files only; nothing is executed)."""
    return CheckoutFacts(
        defaults=checkout_run_defaults(tau2_dir),
        guidelines=checkout_guidelines(tau2_dir),
        git_commit=checkout_commit(tau2_dir),
    )


def last_option_value(args: Sequence[str], flag: str) -> str | None:
    """Value of the last ``flag VALUE`` or ``flag=VALUE`` in ``args`` (argparse: last wins)."""
    value: str | None = None
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(flag + "="):
            value = arg[len(flag) + 1 :]
    return value


def planned_branch_settings(
    config: GridConfig, condition: ConditionSpec, defaults: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The tau-multilingual header values a condition cell's command will produce.

    Pinned flags give ``target_language_directive_version`` and
    ``agent_caller_locale_context``; ``max_steps``, ``max_errors``, ``timeout``,
    ``scores`` and ``text_noise`` come from the last occurrence in the config's and the
    condition's ``extra_tau2_args`` (exact names are guaranteed by
    :func:`abbreviated_flags`), else from ``defaults`` (the checkout's) and tau2's text-mode
    rules (``--timeout 0`` means no timeout, i.e. None; ``scores`` ``all`` expands to
    reward, quality and nativeness; text noise seeds default to 42). Raises
    :class:`ConfigError` for a value tau2 could not parse either.
    """
    run_defaults = {**BRANCH_RUN_DEFAULTS, **(defaults or {})}
    extra = [*config.extra_tau2_args, *condition.extra_tau2_args]

    def number(flag: str, key: str, kind: type) -> Any:
        raw = last_option_value(extra, flag)
        if raw is None:
            return run_defaults[key]
        try:
            return kind(raw)
        except ValueError as exc:
            raise ConfigError(f"extra_tau2_args: {flag} {raw!r} is not a number") from exc

    timeout = number("--timeout", "timeout", float)
    raw_scores = last_option_value(extra, "--scores")
    scores: set[str] = set()
    for token in (raw_scores or ",".join(TEXT_DEFAULT_SCORES)).split(","):
        token = token.strip().lower()
        if token == "all":
            scores.update(TEXT_ALL_SCORES)
        elif token:
            scores.add(token)
    noise_seed = last_option_value(extra, "--text-noise-seed")
    try:
        seed = TEXT_NOISE_DEFAULT_SEED if noise_seed is None else int(noise_seed)
    except ValueError as exc:
        raise ConfigError(f"extra_tau2_args: --text-noise-seed {noise_seed!r}") from exc
    return {
        "target_language_directive_version": TARGET_LANGUAGE_DIRECTIVE_VERSION,
        "agent_caller_locale_context": True,
        "max_steps": number("--max-steps", "max_steps", int),
        "max_errors": number("--max-errors", "max_errors", int),
        "timeout": timeout or None,
        "scores": sorted(scores),
        "text_noise": {"seed": seed} if "--text-noise" in flag_names(extra) else None,
    }


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def branch_setting_mismatches(
    info: Mapping[str, Any],
    config: GridConfig,
    condition: ConditionSpec,
    checkout: CheckoutFacts | None = None,
) -> list[str]:
    """Differences between a stored tau-multilingual header and what a resume would run.

    Compares :func:`planned_branch_settings` (language directive, caller locale context,
    ``max_steps``, ``max_errors``, ``timeout``, ``scores``, text noise) with the header,
    and, when ``checkout`` knows them, the user-simulator guidelines text (tau2 reads it
    from the checkout on every invocation) and ``git_commit``. tau2 treats any of these
    as config drift: under ``--auto-resume`` it would rewrite the header and keep the
    old one in ``info_history``, mixing two regimes in one results.json.
    """
    facts = checkout or CheckoutFacts()
    planned = planned_branch_settings(config, condition, facts.defaults)
    problems: list[str] = []
    for key, want in planned.items():
        stored = info.get(key)
        if key == "text_noise":
            armed = stored is not None
            if armed != (want is not None) or (
                want is not None
                and (not isinstance(stored, Mapping) or stored.get("seed") != want["seed"])
            ):
                problems.append(f"text_noise {stored!r} != planned {want!r}")
            continue
        if key == "scores" and isinstance(stored, list):
            stored = sorted(str(s) for s in stored)
        if key == "timeout" and stored is not None:
            try:
                stored = float(stored)
            except (TypeError, ValueError):
                pass
        if stored != want:
            problems.append(f"{key} {stored!r} != planned {want!r}")
    guidelines = (info.get("user_info") or {}).get("global_simulation_guidelines")
    if facts.guidelines and guidelines is not None and guidelines not in facts.guidelines.values():
        files = ", ".join(f"{name} sha1 {_sha(t)}" for name, t in facts.guidelines.items())
        problems.append(
            f"user-simulator guidelines (stored sha1 {_sha(str(guidelines))}) match no "
            f"guidelines file of the checkout ({files})"
        )
    stored_commit = str(info.get("git_commit") or "")
    if facts.git_commit and stored_commit and stored_commit != "unknown":
        if stored_commit != facts.git_commit:
            problems.append(
                f"git_commit {stored_commit[:12]} != tau2_dir HEAD {facts.git_commit[:12]} "
                "(check that commit out again, or start a new run)"
            )
    return problems


def trial_seeds(seed: int, num_trials: int) -> list[int]:
    """Per-trial seeds exactly as tau2 draws them (``tau2/runner/batch.py``).

    tau2 calls ``random.seed(seed)`` and then ``random.randint(0, 1000000)`` once per
    trial; a private :class:`random.Random` gives the same sequence. E.g. seed 300 ->
    ``[626729, 373753, 361454]`` (the seeds stored in the pilot run).
    """
    rng = random.Random(seed)
    return [rng.randint(0, TAU2_TRIAL_SEED_MAX) for _ in range(num_trials)]


def completed_runs(simulations: Iterable[Mapping[str, Any]]) -> set[tuple[str, int, int | None]]:
    """``(task_id, trial, seed)`` triples tau2 will not re-run (anything but infra errors).

    tau2 keys finished work on ``(trial, task_id, seed)``, so a simulation stored under a
    different seed does NOT count as done for the current plan.
    """
    return {
        (str(sim["task_id"]), int(sim["trial"]), sim.get("seed"))
        for sim in simulations
        if sim.get("termination_reason") != INFRA_ERROR
    }


def remaining_simulations(
    task_ids: Sequence[str],
    num_trials: int,
    done: set[tuple[str, int, int | None]],
    seeds: Sequence[int],
) -> int:
    """Planned (task, trial) simulations whose ``(task, trial, seeds[trial])`` is not done."""
    if len(seeds) < num_trials:
        raise ValueError(f"need {num_trials} trial seeds, got {len(seeds)}")
    return sum(
        1 for t in task_ids for trial in range(num_trials) if (t, trial, seeds[trial]) not in done
    )


def pending_task_ids(
    task_ids: Sequence[str],
    num_trials: int,
    done: set[tuple[str, int, int | None]],
    seeds: Sequence[int],
) -> list[str]:
    """Tasks (in plan order) with at least one trial that tau2 will still run."""
    return [t for t in task_ids if any((t, r, seeds[r]) not in done for r in range(num_trials))]


def _same_args(stored: Any, planned: Mapping[str, Any]) -> bool:
    """Compare llm_args as canonical JSON (key order and int/float spelling ignored)."""
    if not isinstance(stored, Mapping):
        return False
    return compact_json(dict(stored)) == compact_json(dict(planned))


def condition_mismatches(info: Mapping[str, Any], condition: ConditionSpec) -> list[str]:
    """Differences between a stored tau-multilingual ``info`` block and a planned condition.

    Compares ``task_set_name``, ``user_persona_id``, ``text_input_style`` (null and
    ``native_script`` are the same arm) and ``communicate_judge_mode`` (always
    ``exact``). A run made on ``main`` has none of these keys and so never matches.
    """
    problems: list[str] = []
    planned: dict[str, Any] = {
        "task_set_name": condition.task_set_name,
        "user_persona_id": condition.user_persona_id,
        "text_input_style": condition.text_input_style or DEFAULT_TEXT_INPUT_STYLE,
        "communicate_judge_mode": EXACT_COMMUNICATE_JUDGE_MODE,
    }
    stored = {key: info.get(key) for key in planned}
    stored["text_input_style"] = stored["text_input_style"] or DEFAULT_TEXT_INPUT_STYLE
    for key, want in planned.items():
        if stored[key] != want:
            problems.append(f"{key} {stored[key]!r} != planned {want!r}")
    return problems


def run_config_mismatches(
    info: Mapping[str, Any] | None,
    simulations: Sequence[Mapping[str, Any]],
    config: GridConfig,
    simulator: SimulatorSpec,
    condition: ConditionSpec | None = None,
    *,
    info_history: Sequence[Any] = (),
    checkout: CheckoutFacts | None = None,
) -> list[str]:
    """Reasons why an existing run must not be resumed with this config (empty if fine).

    Checks the stored ``info`` against the planned agent/simulator ``llm`` and
    ``llm_args``, ``num_trials``, ``seed`` and domain (plus, for a condition cell,
    :func:`condition_mismatches` and :func:`branch_setting_mismatches` with ``checkout``),
    and checks that every trial index of the stored simulations carries a single seed.
    A non-empty ``info_history`` is refused outright: the branch's ``--auto-resume``
    already rewrote the header once, so the file holds simulations from more than one
    config and a header that matches the plan proves nothing about the older ones.
    Messages name fields only; values of credential-like llm_args are never echoed.
    """
    if info is None:
        return []
    problems: list[str] = []
    if info_history:
        problems.append(
            f"results.json holds simulations from {len(info_history) + 1} run configs: "
            f"tau2 --auto-resume rewrote its header after a config change and kept "
            f"{len(info_history)} earlier header(s) in info_history"
        )
    agent_info = info.get("agent_info") or {}
    user_info = info.get("user_info") or {}
    if agent_info.get("llm") != config.agent.llm:
        problems.append(f"agent llm {agent_info.get('llm')!r} != planned {config.agent.llm!r}")
    if not _same_args(agent_info.get("llm_args"), config.agent.llm_args):
        problems.append("agent llm_args differ from the config")
    if user_info.get("llm") != simulator.llm:
        problems.append(f"user llm {user_info.get('llm')!r} != planned {simulator.llm!r}")
    if not _same_args(user_info.get("llm_args"), simulator.llm_args):
        problems.append("user llm_args differ from the config")
    if info.get("num_trials") != config.num_trials:
        problems.append(f"num_trials {info.get('num_trials')} != planned {config.num_trials}")
    if info.get("seed") != config.seed:
        problems.append(f"seed {info.get('seed')} != planned {config.seed}")
    domain = (info.get("environment_info") or {}).get("domain_name")
    if domain is not None and domain != config.domain:
        problems.append(f"domain {domain!r} != planned {config.domain!r}")
    if condition is not None:
        problems.extend(condition_mismatches(info, condition))
        problems.extend(branch_setting_mismatches(info, config, condition, checkout))
    seeds_by_trial: dict[int, set[Any]] = {}
    for sim in simulations:
        seeds_by_trial.setdefault(int(sim.get("trial") or 0), set()).add(sim.get("seed"))
    mixed = sorted(t for t, seeds in seeds_by_trial.items() if len(seeds) > 1)
    if mixed:
        problems.append(f"trial(s) {mixed} already hold simulations with different seeds")
    return problems


# --------------------------------------------------------------------------------------
# NL-assertion judge preflight
# --------------------------------------------------------------------------------------


def nl_judge_model(tau2_dir: Path) -> str:
    """tau2's NL-assertion judge model, read from ``src/tau2/config.py`` if possible."""
    try:
        text = (tau2_dir / "src" / "tau2" / "config.py").read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_NL_JUDGE_MODEL
    match = NL_JUDGE_RE.search(text)
    return match.group(1) if match else FALLBACK_NL_JUDGE_MODEL


def judge_key_var(model: str) -> str | None:
    """API key variable litellm needs for ``model`` (None for an unknown provider)."""
    if "/" not in model:
        return PROVIDER_KEY_VARS["openai"]
    return PROVIDER_KEY_VARS.get(model.split("/", 1)[0].lower())


def dotenv_var_names(path: Path) -> set[str]:
    """Names of variables assigned a non-empty value in a ``.env`` file (values discarded)."""
    names: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return names
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        if value.strip().strip("'\"").strip():
            names.add(name.strip())
    return names


def env_var_available(name: str, environ: Mapping[str, str], dotenv: Path) -> bool:
    """True iff ``name`` has a non-empty value in ``environ`` or in the ``.env`` file."""
    return bool(environ.get(name, "").strip()) or name in dotenv_var_names(dotenv)


def judge_preflight(
    task_ids: Sequence[str],
    nl_task_ids: set[str],
    judge_model: str,
    key_available: bool | None,
) -> str | None:
    """Refusal message when NL-assertion tasks would hit a judge without a key, else None.

    ``task_ids`` should be the tasks tau2 will actually (re-)run, so a run whose
    NL-assertion tasks are already finished is not refused.

    ``key_available`` is None when the judge's provider (hence key variable) is unknown;
    that case is not refused.
    """
    affected = [t for t in task_ids if t in nl_task_ids]
    if not affected or key_available is not False:
        return None
    var = judge_key_var(judge_model)
    return (
        f"{len(affected)} of {len(task_ids)} task(s) still to run have NL assertions "
        f"({', '.join(affected[:12])}{', ...' if len(affected) > 12 else ''}). tau2 scores them "
        f"with the judge {judge_model!r}, which needs {var}, and {var} is set neither in the "
        "environment nor in tau2-bench/.env. Each such simulation would run the whole "
        "conversation (spending simulator tokens) and then end in infrastructure_error, "
        "again on every resume. Options: (a) add the judge key to tau2-bench/.env; "
        "(b) point DEFAULT_LLM_NL_ASSERTIONS in tau2/config.py at an available model and "
        'log it as a deviation; (c) set "exclude_nl_assertions": true under "tasks" in '
        "the config (new run name; changes comparability with published numbers). "
        "--allow-missing-judge-key runs anyway."
    )


@dataclass(frozen=True)
class BudgetCheck:
    """Outcome of comparing a planned cost with a daily budget."""

    ok: bool
    planned: int
    budget: int | None
    unit: str
    message: str


def check_budget(
    n_simulations: int, per_simulation: int, daily_budget: int | None, unit: str = "tokens"
) -> BudgetCheck:
    """Planned cost = ``n_simulations * per_simulation``; ok iff it fits ``daily_budget``.

    An unknown budget (``None``) is not ok: the user must set it or pass ``--force``.
    """
    planned = n_simulations * per_simulation
    if daily_budget is None:
        return BudgetCheck(False, planned, None, unit, f"planned {planned} {unit}, budget unknown")
    ok = planned <= daily_budget
    verdict = "fits" if ok else "EXCEEDS"
    return BudgetCheck(
        ok, planned, daily_budget, unit, f"planned {planned} {unit} {verdict} budget {daily_budget}"
    )


def suggest_num_chunks(
    n_tasks: int, num_trials: int, per_simulation: int, daily_budget: int
) -> int | None:
    """Smallest N such that every chunk of ``--task-chunk K/N`` fits the daily budget.

    Returns ``None`` when even a single task (all its trials) exceeds the budget.
    """
    tasks_per_day = daily_budget // (num_trials * per_simulation)
    if tasks_per_day < 1:
        return None
    return math.ceil(n_tasks / tasks_per_day)


def chunk_hint(
    failed: Sequence[BudgetCheck], n_remaining: int, n_tasks: int, num_trials: int
) -> str:
    """One actionable hint for failed budget checks: the --task-chunk N that fits all of them.

    The per-simulation cost is recovered as ``planned / n_remaining``; the binding
    constraint is whichever check needs the most chunks.
    """
    if any(c.budget is None for c in failed):
        return "set daily budgets for this simulator in the config (or pass --force)"
    if n_remaining == 0:
        return "nothing left to run"
    needed: list[int] = []
    for c in failed:
        assert c.budget is not None
        n = suggest_num_chunks(n_tasks, num_trials, math.ceil(c.planned / n_remaining), c.budget)
        if n is None:
            return f"one task x {num_trials} trials already exceeds the daily {c.unit} budget"
        needed.append(n)
    n = max(needed)
    return f"try --task-chunk 1/{n} (then 2/{n}, ... on later days)"


def per_simulation_estimates(
    simulator: SimulatorSpec, condition: ConditionSpec | None = None
) -> dict[str, tuple[int, int | None]]:
    """``unit -> (estimate per simulation, daily budget)`` for one grid cell.

    Tokens always; requests only when the simulator or the condition estimates them. A
    condition's estimate replaces the simulator's (its budget stays the simulator's:
    the quota belongs to the provider model, not to the condition).
    """
    tokens = simulator.tokens_per_task_estimate
    requests = simulator.requests_per_task_estimate
    if condition is not None:
        tokens = condition.tokens_per_task_estimate
        if condition.requests_per_task_estimate is not None:
            requests = condition.requests_per_task_estimate
    out: dict[str, tuple[int, int | None]] = {"tokens": (tokens, simulator.daily_token_budget)}
    if requests is not None:
        out["requests"] = (requests, simulator.daily_request_budget)
    return out


def budget_checks(
    simulator: SimulatorSpec, n_remaining: int, condition: ConditionSpec | None = None
) -> list[BudgetCheck]:
    """Token check, plus a request check when the simulator (or condition) defines one."""
    return [
        check_budget(n_remaining, per_sim, budget, unit)
        for unit, (per_sim, budget) in per_simulation_estimates(simulator, condition).items()
    ]


def shared_budget_checks(
    simulator: SimulatorSpec, cells: Sequence[tuple[ConditionSpec | None, int]]
) -> list[BudgetCheck]:
    """Budget checks for all cells of one simulator together (they share its daily quota).

    ``cells`` holds ``(condition, remaining simulations)``; the planned cost per unit is
    ``sum(remaining * that cell's per-simulation estimate)``. With a single cell this
    equals :func:`budget_checks`.
    """
    planned: dict[str, int] = {}
    budgets: dict[str, int | None] = {}
    for condition, n_remaining in cells:
        for unit, (per_sim, budget) in per_simulation_estimates(simulator, condition).items():
            planned[unit] = planned.get(unit, 0) + n_remaining * per_sim
            budgets[unit] = budget
    return [check_budget(1, planned[unit], budgets[unit], unit) for unit in planned]


def shared_chunk_hint(
    simulator: SimulatorSpec,
    conditions: Sequence[ConditionSpec | None],
    n_tasks: int,
    num_trials: int,
) -> str:
    """``--task-chunk`` advice for one simulator's condition cells that exceed its budget.

    Gives N for running every condition together (per-simulation estimates summed, as
    :func:`shared_budget_checks` does) and, with several conditions, N for running one
    condition per invocation (``--condition NAME``; the costliest condition binds).
    """
    if not conditions:
        return "nothing left to run"
    budgets: dict[str, int | None] = {}
    for condition in conditions:
        for unit, (_, budget) in per_simulation_estimates(simulator, condition).items():
            budgets[unit] = budget
    if any(budget is None for budget in budgets.values()):
        return "set daily budgets for this simulator in the config (or pass --force)"

    def chunks_needed(conds: Sequence[ConditionSpec | None]) -> int | None:
        """Smallest N whose every chunk of ``conds`` together fits all daily budgets."""
        worst = 0
        for unit, budget in budgets.items():
            assert budget is not None
            per_sim = sum(
                per_simulation_estimates(simulator, c).get(unit, (0, None))[0] for c in conds
            )
            n = suggest_num_chunks(n_tasks, num_trials, per_sim, budget)
            if n is None:
                return None
            worst = max(worst, n)
        return worst

    together = chunks_needed(conditions)
    if len(conditions) == 1:
        if together is None:
            return f"one task x {num_trials} trials already exceeds the daily budget"
        return f"try --task-chunk 1/{together} (then 2/{together}, ... on later days)"
    singles = [chunks_needed([c]) for c in conditions]
    if any(n is None for n in singles):
        single_hint = "even one condition alone exceeds the daily budget for one task"
    else:
        one = max(n for n in singles if n is not None)
        single_hint = f"one condition per run: --condition NAME --task-chunk 1/{one}"
    if together is None:
        n = len(conditions)
        return f"{n} conditions together exceed the budget for one task; {single_hint}"
    return (
        f"all {len(conditions)} conditions together: try --task-chunk 1/{together} "
        f"(then 2/{together}, ... on later days); or {single_hint}"
    )


# --------------------------------------------------------------------------------------
# Agent server health
# --------------------------------------------------------------------------------------


def health_url(api_base: str) -> str:
    """llama.cpp health endpoint for an OpenAI-compatible ``api_base`` (``.../v1``)."""
    base = api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/health"


def is_healthy(status_code: int, body: str) -> bool:
    """True iff the server answered 200 with JSON ``{"status": "ok", ...}``."""
    if status_code != 200:
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def check_agent_health(api_base: str, timeout: float = 5.0) -> tuple[bool, str]:
    """GET the health endpoint; return ``(healthy, human readable detail)``."""
    url = health_url(api_base)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local URL
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:  # llama.cpp answers 503 while loading the model
        return False, f"{url} -> HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"{url} -> {exc}"
    return is_healthy(status, body), f"{url} -> HTTP {status} {body.strip()[:120]}"


# --------------------------------------------------------------------------------------
# Results summary
# --------------------------------------------------------------------------------------


@dataclass
class RunSummary:
    """What happened for one simulator after its tau2 process finished (or was skipped)."""

    simulator: str
    run_name: str
    status: str
    results: Path
    n_simulations: int = 0
    n_infra_errors: int = 0
    n_success: int = 0
    notes: list[str] = field(default_factory=list)
    condition: str | None = None


def count_outcomes(simulations: Iterable[Mapping[str, Any]]) -> tuple[int, int, int]:
    """``(total, infra_errors, successes)`` where success means reward >= 1 - 1e-6."""
    total = infra = success = 0
    for sim in simulations:
        total += 1
        if sim.get("termination_reason") == INFRA_ERROR:
            infra += 1
            continue
        reward = (sim.get("reward_info") or {}).get("reward")
        if reward is not None and reward >= 1 - 1e-6:
            success += 1
    return total, infra, success


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------


def console_write(text: str) -> None:
    """Write to stdout without crashing on consoles that cannot encode Hindi (e.g. cp1252)."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(encoding, errors="replace").decode(encoding))
    sys.stdout.flush()


def run_and_log(argv: Sequence[str], cwd: Path, log_path: Path) -> int:
    """Run ``argv`` in ``cwd``, streaming its merged output to the console and ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
        log.write(f"cwd: {cwd}\nargv: {shlex.join(argv)}\n\n")
        log.flush()
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=subprocess_env(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                console_write(line)
                log.write(line)
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            log.write("\n[run_grid] interrupted by user; rerun the same command to resume\n")
            raise
        finally:
            log.write(f"\n[run_grid] exit code: {proc.poll()}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_grid.py",
        description="Run tau2-bench over (user simulators) x (tasks) x (trials) for one agent.",
    )
    parser.add_argument("config", type=Path, nargs="?", help="grid config JSON (see configs/)")
    parser.add_argument(
        "--config", dest="config_opt", type=Path, metavar="CONFIG", help="same as CONFIG"
    )
    parser.add_argument("--tau2-dir", type=Path, help="tau2-bench checkout (overrides config)")
    parser.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="run only these simulators")
    parser.add_argument(
        "--condition",
        dest="conditions",
        nargs="+",
        metavar="NAME",
        help="run only these conditions of a condition grid (disabled ones included)",
    )
    parser.add_argument(
        "--task-chunk",
        metavar="K/N",
        help="run the first K of N contiguous task chunks (cumulative, resume-safe)",
    )
    parser.add_argument("--force", action="store_true", help="ignore the daily budget guard")
    parser.add_argument(
        "--allow-missing-judge-key",
        action="store_true",
        help="run NL-assertion tasks even though the judge's API key is missing",
    )
    parser.add_argument("--keep-going", action="store_true", help="continue after a failed run")
    parser.add_argument("--skip-health-check", action="store_true")
    parser.add_argument("--health-timeout", type=float, default=5.0)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    return parser


def _selected_simulators(config: GridConfig, only: Sequence[str] | None) -> list[SimulatorSpec]:
    if only:
        known = {s.name for s in config.simulators}
        unknown = sorted(set(only) - known)
        if unknown:
            raise ConfigError(f"--only: unknown simulators {unknown}; have {sorted(known)}")
        return [s for s in config.simulators if s.name in set(only)]
    return [s for s in config.simulators if s.enabled]


def _selected_conditions(
    config: GridConfig, names: Sequence[str] | None
) -> list[ConditionSpec | None]:
    """Conditions to run: ``[None]`` for a grid without conditions (one cell per simulator)."""
    if not config.conditions:
        if names:
            raise ConfigError("--condition: this config defines no conditions")
        return [None]
    if names:
        known = {c.name for c in config.conditions}
        unknown = sorted(set(names) - known)
        if unknown:
            raise ConfigError(f"--condition: unknown conditions {unknown}; have {sorted(known)}")
        return [c for c in config.conditions if c.name in set(names)]
    return [c for c in config.conditions if c.enabled]


def describe_condition(condition: ConditionSpec) -> str:
    """One-line human summary of a condition's tau2 settings."""
    return (
        f"task set {condition.task_set_name}, ids <id>{condition.task_id_suffix}, "
        f"persona {condition.user_persona_id or '(default user)'}, "
        f"style {condition.text_input_style or '(default)'}, "
        f"{condition.tokens_per_task_estimate} tokens/sim estimate"
    )


def _config_path(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    """The config path from the positional argument or ``--config`` (exactly one)."""
    if args.config is not None and args.config_opt is not None:
        if args.config != args.config_opt:
            parser.error("give the config either as CONFIG or with --config, not both")
    path = args.config or args.config_opt
    if path is None:
        parser.error("a config file is required (CONFIG or --config CONFIG)")
    return path


@dataclass
class CellPlan:
    """What is known about one grid cell (simulator x condition) before anything runs."""

    simulator: SimulatorSpec
    condition: ConditionSpec | None
    run_name: str
    results: Path
    task_ids: list[str]
    mismatches: list[str]
    n_done: int = 0
    n_left: int = 0
    judge_problem: str | None = None


def plan_cell(
    config: GridConfig,
    simulator: SimulatorSpec,
    condition: ConditionSpec | None,
    run_name: str,
    base_task_ids: Sequence[str],
    tau2_dir: Path,
    nl_ids: set[str],
    judge: str,
    key_ok: bool | None,
    checkout: CheckoutFacts | None = None,
) -> CellPlan:
    """Read the cell's existing results and work out what tau2 would still run.

    ``checkout`` (:func:`read_checkout_facts`) lets the resume guard of a condition cell
    compare the stored header with the checkout's defaults, guidelines and commit.
    """
    res_path = results_path(tau2_dir, run_name)
    existing = load_simulations(res_path)
    task_ids = condition_task_ids(base_task_ids, condition)
    info, history = load_run_header(res_path)
    mismatches = run_config_mismatches(
        info,
        existing,
        config,
        simulator,
        condition,
        info_history=history,
        checkout=checkout,
    )
    plan = CellPlan(simulator, condition, run_name, res_path, task_ids, mismatches)
    if mismatches:
        return plan
    done = completed_runs(existing)
    seeds = trial_seeds(config.seed, config.num_trials)
    plan.n_left = remaining_simulations(task_ids, config.num_trials, done, seeds)
    plan.n_done = len(task_ids) * config.num_trials - plan.n_left
    pending = pending_task_ids(task_ids, config.num_trials, done, seeds)
    cell_nl_ids = set(condition_task_ids(sorted(nl_ids), condition))
    plan.judge_problem = judge_preflight(pending, cell_nl_ids, judge, key_ok)
    return plan


def _run_cell(
    plan: CellPlan,
    config: GridConfig,
    args: argparse.Namespace,
    tau2_dir: Path,
    n_tasks: int,
    shared_over: Sequence[BudgetCheck],
    shared_hint: str,
) -> tuple[RunSummary, bool]:
    """Check, print and (unless dry-run) run one cell; returns ``(summary, stop_grid)``.

    Without a condition the output and decisions are those of the original
    one-cell-per-simulator loop. With a condition the budget decision is the simulator's
    shared check (``shared_over`` / ``shared_hint``), which is never below the cell's own.
    """
    sim, cond = plan.simulator, plan.condition
    if cond is None:
        print(f"\n[run_grid] === simulator {sim.name}: {sim.llm} ===")
    else:
        print(f"\n[run_grid] --- {sim.name} x condition {cond.name} ---")
    print(f"[run_grid] run name: {plan.run_name}")
    notes = sim.notes if cond is None else cond.notes
    if notes:
        print(f"[run_grid] notes: {notes}")
    summary = RunSummary(
        sim.name,
        plan.run_name,
        "pending",
        plan.results,
        condition=None if cond is None else cond.name,
    )

    if plan.mismatches:
        for problem in plan.mismatches:
            print(f"[run_grid] existing run differs: {problem}")
        print(
            "[run_grid] tau2 --auto-resume would mix both configs in one results.json "
            "(main keeps the old header; tau-multilingual rewrites it and moves the old one "
            "to info_history); use a new run name (or an experiment name / template with {cfg})"
        )
        summary.status = "refused: existing run has a different config"
        return summary, False

    print(f"[run_grid] simulations: {plan.n_done} already done, {plan.n_left} to run")
    checks = budget_checks(sim, plan.n_left, cond)
    scope = "" if cond is None else ", this cell alone"
    for check in checks:
        print(f"[run_grid] budget ({check.unit}{scope}): {check.message}")
    if cond is None:
        over: Sequence[BudgetCheck] = [c for c in checks if not c.ok]
        hint = chunk_hint(over, plan.n_left, n_tasks, config.num_trials) if over else ""
    else:
        over, hint = shared_over, shared_hint
    if over and not args.force:
        print(f"[run_grid] {hint}")
        summary.status = "refused: over budget (use --force to override)"
        print(f"[run_grid] {summary.status}")
        return summary, False

    if plan.judge_problem and not args.allow_missing_judge_key:
        print(f"[run_grid] {plan.judge_problem}")
        summary.status = "refused: NL-assertion judge key missing"
        print(f"[run_grid] {summary.status}")
        return summary, False

    argv_list = build_tau2_command(config, sim, plan.task_ids, plan.run_name, cond)
    print("[run_grid] command (POSIX quoting; cwd = tau2 dir):")
    print("  " + shlex.join(argv_list))
    if args.dry_run:
        summary.status = "dry-run"
        return summary, False
    if plan.n_left == 0:
        summary.status = "already complete"
        return summary, False

    if not args.skip_health_check and "api_base" in config.agent.llm_args:
        healthy, detail = check_agent_health(
            str(config.agent.llm_args["api_base"]), args.health_timeout
        )
        print(f"[run_grid] agent health: {detail}")
        if not healthy:
            summary.status = "aborted: agent server not healthy"
            print("[run_grid] start llama-server first, or pass --skip-health-check")
            return summary, True

    log_path = args.log_dir / f"{plan.run_name}.log"
    print(f"[run_grid] log: {log_path}")
    try:
        code = run_and_log(argv_list, tau2_dir, log_path)
    except KeyboardInterrupt:
        summary.status = "interrupted (rerun the same command to resume)"
        return summary, True
    summary.status = "ok" if code == 0 else f"failed (exit {code})"
    return summary, code != 0 and not args.keep_going


def _check_condition_pairing(
    config: GridConfig,
    conditions: Sequence[ConditionSpec | None],
    tau2_dir: Path,
    base_tasks: Sequence[Mapping[str, Any]],
    base_ids: Sequence[str],
) -> list[str]:
    """Locate each condition's task set and verify pairing; returns lines to print.

    Raises :class:`ConfigError` when a task set is missing, an id does not pair, or a
    non-default ``text_input_style`` is not declared by the persona's language pack
    (:func:`text_style_problem`).
    """
    lines: list[str] = []
    for cond in conditions:
        if cond is None:
            continue
        path = task_set_file_path(tau2_dir, config.domain, cond.task_set_name)
        problems = pairing_problems(base_tasks, load_tasks(path), base_ids, cond.task_id_suffix)
        if problems:
            raise ConfigError(
                f"condition {cond.name}: task set {cond.task_set_name} ({path}) does not "
                "pair with the base tasks: " + "; ".join(problems)
            )
        style_problem = text_style_problem(tau2_dir, cond)
        if style_problem:
            raise ConfigError(f"condition {cond.name}: {style_problem}")
        try:
            shown = path.relative_to(tau2_dir).as_posix()
        except ValueError:
            shown = str(path)
        style_note = (
            ""
            if cond.text_input_style in (None, DEFAULT_TEXT_INPUT_STYLE)
            else f"; style {cond.text_input_style} declared by the persona's pack"
        )
        lines.append(
            f"[run_grid]   {cond.name}: {describe_condition(cond)}; {len(base_ids)}/"
            f"{len(base_ids)} ids found in {shown} with identical "
            + ", ".join(PAIRED_TASK_FIELDS)
            + style_note
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = _config_path(parser, args)
    try:
        config = load_config(config_path)
        tau2_dir = args.tau2_dir or Path(config.tau2_dir or DEFAULT_TAU2_DIR)
        splits = load_splits(split_file_path(tau2_dir, config.domain))
        base_tasks = load_tasks(tasks_file_path(tau2_dir, config.domain))
        reference_line = ""
        if config.reference_tau2_dir:
            reference = Path(config.reference_tau2_dir)
            differ = base_task_file_differences(tau2_dir, reference, config.domain)
            if differ:
                raise ConfigError(
                    f"{', '.join(differ)} of domain {config.domain!r} differ between {tau2_dir} "
                    f"and reference_tau2_dir {reference}: base task ids would not name the "
                    "same tasks in both checkouts"
                )
            reference_line = (
                f"[run_grid] base tasks: split_tasks.json and tasks.json identical to {reference}"
            )
        nl_ids = nl_assertion_task_ids(base_tasks)
        all_ids = resolve_task_ids(config.tasks, splits, nl_ids)
        chunk = parse_chunk(args.task_chunk) if args.task_chunk else (1, 1)
        simulators = _selected_simulators(config, args.only)
        conditions = _selected_conditions(config, args.conditions)
        cells = selected_cells(config, simulators, conditions)
        pairing_lines = _check_condition_pairing(config, conditions, tau2_dir, base_tasks, all_ids)
        checkout = read_checkout_facts(tau2_dir) if config.conditions else None
        for cond in conditions:
            if cond is not None:  # extra-arg values tau2 could not parse fail here, early
                planned_branch_settings(config, cond, checkout.defaults if checkout else None)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"[run_grid] error: {exc}", file=sys.stderr)
        return 2

    task_ids, new_ids = select_chunk(all_ids, *chunk)
    print(f"[run_grid] experiment: {config.experiment}  ({config.description})")
    print(f"[run_grid] tau2 dir:   {tau2_dir}")
    if reference_line:
        print(reference_line)
    print(f"[run_grid] agent:      {config.agent.llm}  args={compact_json(config.agent.llm_args)}")
    print(
        f"[run_grid] tasks:      {len(all_ids)} total; chunk {chunk[0]}/{chunk[1]} adds "
        f"{len(new_ids)} -> passing {len(task_ids)} ids x {config.num_trials} trials"
    )
    if config.conditions:
        chosen = {c.name for c in conditions if c is not None}
        skipped = [c.name for c in config.conditions if c.name not in chosen]
        print(
            f"[run_grid] conditions: {len(chosen)} selected"
            + (f" (not selected: {', '.join(skipped)})" if skipped else "")
        )
        for line in pairing_lines:
            print(line)
    if not simulators:
        print("[run_grid] no enabled simulators selected (use --only NAME)", file=sys.stderr)
        return 2
    if not conditions:
        print("[run_grid] no enabled conditions selected (use --condition NAME)", file=sys.stderr)
        return 2

    judge = nl_judge_model(tau2_dir)
    key_var = judge_key_var(judge)
    key_ok = None if key_var is None else env_var_available(key_var, os.environ, tau2_dir / ".env")
    n_nl = sum(1 for t in task_ids if t in nl_ids)
    print(
        f"[run_grid] NL judge:   {judge} for {n_nl} selected task(s); key "
        + ("unknown provider, not checked" if key_var is None else f"{key_var} ")
        + ("" if key_var is None else ("present" if key_ok else "MISSING"))
    )

    if not args.dry_run and shutil.which("uv") is None:
        print("[run_grid] error: 'uv' not found on PATH", file=sys.stderr)
        return 2

    summaries: list[RunSummary] = []
    stop = False
    for sim in simulators:
        plans = [
            plan_cell(config, sim, cond, name, task_ids, tau2_dir, nl_ids, judge, key_ok, checkout)
            for cell_sim, cond, name in cells
            if cell_sim is sim
        ]
        shared_over: list[BudgetCheck] = []
        shared_hint = ""
        if config.conditions:
            print(f"\n[run_grid] === simulator {sim.name}: {sim.llm} ===")
            if sim.notes:
                print(f"[run_grid] notes: {sim.notes}")
            runnable = [
                p
                for p in plans
                if not p.mismatches and not (p.judge_problem and not args.allow_missing_judge_key)
            ]
            shared = shared_budget_checks(sim, [(p.condition, p.n_left) for p in runnable])
            if runnable:
                names = ", ".join(p.condition.name for p in runnable if p.condition is not None)
                print(f"[run_grid] daily budget shared by {len(runnable)} cell(s) ({names}):")
                for check in shared:
                    print(f"[run_grid]   {check.unit}: {check.message}")
            shared_over = [c for c in shared if not c.ok]
            if shared_over:
                shared_hint = shared_chunk_hint(
                    sim, [p.condition for p in runnable], len(all_ids), config.num_trials
                )
        for plan in plans:
            summary, stop = _run_cell(
                plan, config, args, tau2_dir, len(all_ids), shared_over, shared_hint
            )
            summaries.append(summary)
            if stop:
                break
        if stop:
            break

    print_summary(summaries)
    return 0 if all(s.status in {"ok", "dry-run", "already complete"} for s in summaries) else 1


def print_summary(summaries: Sequence[RunSummary]) -> None:
    """Print one line per run and the analyze.py command for the runs that have results."""
    print("\n[run_grid] ===== summary =====")
    existing: list[Path] = []
    labels = [
        s.simulator if s.condition is None else f"{s.simulator}/{s.condition}" for s in summaries
    ]
    label_width = max([14, *(len(label) for label in labels)])
    name_width = max([48, *(len(s.run_name) for s in summaries)])
    for s, label in zip(summaries, labels, strict=True):
        total, infra, success = count_outcomes(load_simulations(s.results))
        line = f"  {label:<{label_width}} {s.run_name:<{name_width}} {s.status}"
        if total:
            valid = total - infra
            line += f" | sims={total} infra_errors={infra} success={success}/{valid}"
            existing.append(s.results)
        print(line)
        if infra and not s.status.startswith("refused"):
            print("    infra errors are retried automatically: rerun the same command")
    if existing:
        print("\n[run_grid] analyze (run from the whose-noise repo root):")
        print("  uv run python -m whose_noise.analyze " + " ".join(p.as_posix() for p in existing))


if __name__ == "__main__":
    sys.exit(main())
