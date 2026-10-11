"""Run a grid of tau2-bench simulations: (user simulators) x (tasks) x (trials) for one agent.

Usage (from the whose-noise repo root; ``--config PATH`` is accepted as well)::

    python scripts/run_grid.py configs/pilot.json --dry-run
    python scripts/run_grid.py configs/rq1_simulator_swap.json --dry-run
    python scripts/run_grid.py configs/rq1_simulator_swap.json --only gptoss120b --task-chunk 1/6

The script is standard-library only, so any Python >= 3.11 works (``uv run python ...``
also works once the package builds).

For every enabled simulator in the config this script builds one ``tau2 run`` command::

    uv run tau2 run --domain retail --agent-llm <agent> --agent-llm-args '<json>'
        --user-llm <sim> --user-llm-args '<json>' --num-trials N --seed S
        --task-ids ... --max-concurrency 1 --save-to <run_name> --auto-resume

and runs it inside the tau2-bench checkout with ``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8``.

Design decisions
----------------
* **One results.json per (agent, simulator, task set, trials, seed).** The run name is a
  deterministic function of the config, so re-running the same command resumes the same
  run (``--auto-resume``): finished simulations are skipped and ``infrastructure_error``
  simulations are retried by tau2. The default template includes ``{cfg}``, a short hash
  of the agent and simulator LLM strings and llm_args and the domain, so changing a
  model or a temperature gives a new run. Because tau2's ``--auto-resume`` silently keeps
  the OLD ``info`` block when the config changed, an existing results.json is also
  checked before every run: a different agent/simulator llm or llm_args, num_trials,
  seed or domain (or a file that already mixes seeds) is refused.
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
DEFAULT_SEED = 300
DEFAULT_MAX_CONCURRENCY = 1
INFRA_ERROR = "infrastructure_error"
RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SUBPROCESS_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
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
        run_name_template=str(raw.get("run_name_template", DEFAULT_RUN_NAME_TEMPLATE)),
        tau2_dir=raw.get("tau2_dir"),
        extra_tau2_args=tuple(extra),
        description=str(raw.get("description", "")),
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


def config_hash(config: GridConfig, simulator: SimulatorSpec) -> str:
    """First 6 hex chars of sha1 over the model-defining settings of one grid cell.

    Covers the agent and simulator ``llm`` strings and ``llm_args`` and the domain, so a
    different model, temperature or api_base gives a different run name.
    """
    payload = compact_json(
        {
            "agent_llm": config.agent.llm,
            "agent_llm_args": config.agent.llm_args,
            "domain": config.domain,
            "user_llm": simulator.llm,
            "user_llm_args": simulator.llm_args,
        }
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:6]


def make_run_name(config: GridConfig, simulator: SimulatorSpec) -> str:
    """Deterministic tau2 ``--save-to`` name for one (agent, simulator) cell of the grid.

    Template fields: ``{experiment} {agent} {user} {tasks} {trials} {seed} {domain}
    {cfg}`` (``cfg`` = :func:`config_hash`). A template without ``{cfg}`` (such as the
    legacy pilot one) is protected only by the existing-run check in :func:`main`.
    """
    try:
        name = config.run_name_template.format(
            experiment=slugify(config.experiment),
            agent=slugify(config.agent.name),
            user=slugify(simulator.name),
            tasks=tasks_tag(config.tasks),
            trials=config.num_trials,
            seed=config.seed,
            domain=slugify(config.domain),
            cfg=config_hash(config, simulator),
        )
    except KeyError as exc:
        raise ConfigError(f"run_name_template: unknown field {exc}") from exc
    if not RUN_NAME_RE.fullmatch(name):
        raise ConfigError(f"run name {name!r} has characters unsafe for a directory name")
    return name


def compact_json(obj: Mapping[str, Any]) -> str:
    """JSON with stable key order and no spaces, for CLI arguments."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def build_tau2_command(
    config: GridConfig, simulator: SimulatorSpec, task_ids: Sequence[str], run_name: str
) -> list[str]:
    """Exact argv list for one ``tau2 run`` invocation (run with cwd = tau2-bench)."""
    return [
        "uv",
        "run",
        "tau2",
        "run",
        "--domain",
        config.domain,
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
        "--save-to",
        run_name,
        "--auto-resume",
        *config.extra_tau2_args,
    ]


def subprocess_env(base: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``base`` with UTF-8 forced for the child Python process."""
    env = dict(base)
    env.update(SUBPROCESS_ENV)
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
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("info") or {}


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


def run_config_mismatches(
    info: Mapping[str, Any] | None,
    simulations: Sequence[Mapping[str, Any]],
    config: GridConfig,
    simulator: SimulatorSpec,
) -> list[str]:
    """Reasons why an existing run must not be resumed with this config (empty if fine).

    Checks the stored ``info`` against the planned agent/simulator ``llm`` and
    ``llm_args``, ``num_trials``, ``seed`` and domain, and checks that every trial index
    of the stored simulations carries a single seed. Messages name fields only; values
    of credential-like llm_args are never echoed.
    """
    if info is None:
        return []
    problems: list[str] = []
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


def budget_checks(simulator: SimulatorSpec, n_remaining: int) -> list[BudgetCheck]:
    """Token check, plus a request check when the simulator defines one."""
    checks = [
        check_budget(
            n_remaining, simulator.tokens_per_task_estimate, simulator.daily_token_budget, "tokens"
        )
    ]
    if simulator.requests_per_task_estimate is not None:
        checks.append(
            check_budget(
                n_remaining,
                simulator.requests_per_task_estimate,
                simulator.daily_request_budget,
                "requests",
            )
        )
    return checks


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


def _config_path(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    """The config path from the positional argument or ``--config`` (exactly one)."""
    if args.config is not None and args.config_opt is not None:
        if args.config != args.config_opt:
            parser.error("give the config either as CONFIG or with --config, not both")
    path = args.config or args.config_opt
    if path is None:
        parser.error("a config file is required (CONFIG or --config CONFIG)")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = _config_path(parser, args)
    try:
        config = load_config(config_path)
        tau2_dir = args.tau2_dir or Path(config.tau2_dir or DEFAULT_TAU2_DIR)
        splits = load_splits(split_file_path(tau2_dir, config.domain))
        nl_ids = nl_assertion_task_ids(load_tasks(tasks_file_path(tau2_dir, config.domain)))
        all_ids = resolve_task_ids(config.tasks, splits, nl_ids)
        chunk = parse_chunk(args.task_chunk) if args.task_chunk else (1, 1)
        simulators = _selected_simulators(config, args.only)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"[run_grid] error: {exc}", file=sys.stderr)
        return 2

    task_ids, new_ids = select_chunk(all_ids, *chunk)
    print(f"[run_grid] experiment: {config.experiment}  ({config.description})")
    print(f"[run_grid] tau2 dir:   {tau2_dir}")
    print(f"[run_grid] agent:      {config.agent.llm}  args={compact_json(config.agent.llm_args)}")
    print(
        f"[run_grid] tasks:      {len(all_ids)} total; chunk {chunk[0]}/{chunk[1]} adds "
        f"{len(new_ids)} -> passing {len(task_ids)} ids x {config.num_trials} trials"
    )
    if not simulators:
        print("[run_grid] no enabled simulators selected (use --only NAME)", file=sys.stderr)
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
    for sim in simulators:
        run_name = make_run_name(config, sim)
        res_path = results_path(tau2_dir, run_name)
        existing = load_simulations(res_path)
        argv_list = build_tau2_command(config, sim, task_ids, run_name)
        print(f"\n[run_grid] === simulator {sim.name}: {sim.llm} ===")
        print(f"[run_grid] run name: {run_name}")
        if sim.notes:
            print(f"[run_grid] notes: {sim.notes}")
        summary = RunSummary(sim.name, run_name, "pending", res_path)

        mismatches = run_config_mismatches(load_run_info(res_path), existing, config, sim)
        if mismatches:
            for problem in mismatches:
                print(f"[run_grid] existing run differs: {problem}")
            print(
                "[run_grid] tau2 --auto-resume would silently mix both configs in one "
                "results.json; use a new run name (or an experiment name / template with {cfg})"
            )
            summary.status = "refused: existing run has a different config"
            summaries.append(summary)
            continue

        done = completed_runs(existing)
        seeds = trial_seeds(config.seed, config.num_trials)
        n_left = remaining_simulations(task_ids, config.num_trials, done, seeds)
        n_done = len(task_ids) * config.num_trials - n_left
        print(f"[run_grid] simulations: {n_done} already done, {n_left} to run")
        pending = pending_task_ids(task_ids, config.num_trials, done, seeds)
        judge_problem = judge_preflight(pending, nl_ids, judge, key_ok)

        checks = budget_checks(sim, n_left)
        for check in checks:
            print(f"[run_grid] budget ({check.unit}): {check.message}")
        over = [c for c in checks if not c.ok]
        if over and not args.force:
            print(f"[run_grid] {chunk_hint(over, n_left, len(all_ids), config.num_trials)}")
            summary.status = "refused: over budget (use --force to override)"
            print(f"[run_grid] {summary.status}")
            summaries.append(summary)
            continue

        if judge_problem and not args.allow_missing_judge_key:
            print(f"[run_grid] {judge_problem}")
            summary.status = "refused: NL-assertion judge key missing"
            print(f"[run_grid] {summary.status}")
            summaries.append(summary)
            continue

        print("[run_grid] command (POSIX quoting; cwd = tau2 dir):")
        print("  " + shlex.join(argv_list))
        if args.dry_run:
            summary.status = "dry-run"
            summaries.append(summary)
            continue
        if n_left == 0:
            summary.status = "already complete"
            summaries.append(summary)
            continue

        if not args.skip_health_check and "api_base" in config.agent.llm_args:
            healthy, detail = check_agent_health(
                str(config.agent.llm_args["api_base"]), args.health_timeout
            )
            print(f"[run_grid] agent health: {detail}")
            if not healthy:
                summary.status = "aborted: agent server not healthy"
                summaries.append(summary)
                print("[run_grid] start llama-server first, or pass --skip-health-check")
                break

        log_path = args.log_dir / f"{run_name}.log"
        print(f"[run_grid] log: {log_path}")
        try:
            code = run_and_log(argv_list, tau2_dir, log_path)
        except KeyboardInterrupt:
            summary.status = "interrupted (rerun the same command to resume)"
            summaries.append(summary)
            break
        summary.status = "ok" if code == 0 else f"failed (exit {code})"
        summaries.append(summary)
        if code != 0 and not args.keep_going:
            break

    print_summary(summaries)
    return 0 if all(s.status in {"ok", "dry-run", "already complete"} for s in summaries) else 1


def print_summary(summaries: Sequence[RunSummary]) -> None:
    """Print one line per run and the analyze.py command for the runs that have results."""
    print("\n[run_grid] ===== summary =====")
    existing: list[Path] = []
    for s in summaries:
        total, infra, success = count_outcomes(load_simulations(s.results))
        line = f"  {s.simulator:<14} {s.run_name:<48} {s.status}"
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
