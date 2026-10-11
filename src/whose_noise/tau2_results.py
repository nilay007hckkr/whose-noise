"""Load tau2-bench ``results.json`` files into a tidy, one-row-per-simulation table.

A tau2 run directory (``tau2-bench/data/simulations/<run>/results.json``) holds the run
configuration under ``info`` and one entry per (task, trial) under ``simulations``.
This module flattens those entries into a :class:`pandas.DataFrame` so that downstream
code (``metrics.py``, ``analyze.py``) never has to touch the raw JSON.

Conventions
-----------
* An *infrastructure error* is a simulation whose ``termination_reason`` is
  ``"infrastructure_error"`` **or** whose ``reward_info`` is missing. Such rows say
  nothing about the agent or the user simulator, so their ``reward`` is NaN and their
  ``success`` is ``pd.NA``. They are kept in the frame (so they can be counted) and
  excluded by :func:`outcomes_by_task` by default.
* ``success`` is ``reward == 1.0`` (tau-bench's binary pass criterion).
* ``first_failed_action`` is the name of the first expected action (in the task's
  gold order) whose ``action_match`` is False, regardless of read/write type.
* ``failed_write_actions`` is a tuple of names of expected *write* actions that did
  not match, in gold order. Write actions are what change the DB, so they are the
  most direct explanation of a failed DB check.
* ``extra_tool_calls`` is a tuple of agent tool calls whose *names* are not covered
  by the gold actions (multiset difference, in call order). It explains failures
  where every gold action matched but the agent also did something unrequested
  (e.g. an extra ``modify_user_address`` that breaks the DB check).
* ``error_type`` / ``error_stage`` describe infrastructure errors. ``error_stage`` is
  ``"evaluation"`` when the traceback passes through ``tau2/evaluator`` (the
  conversation ran but scoring crashed, e.g. the NL-assertion judge had no API key)
  and ``"simulation"`` otherwise. Both are None for valid simulations.
* Within one results file, ``(task_id, trial)`` must be unique and each trial index
  must map to a single seed (tau2 derives one seed per trial from ``info.seed``).
  A tau2 ``--auto-resume`` after a config or seed change can append duplicates;
  :func:`load_runs` refuses such files by default (see :func:`trial_integrity_problems`).
"""

from __future__ import annotations

import json
import warnings
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

import pandas as pd

INFRA_ERROR = "infrastructure_error"

COLUMNS: tuple[str, ...] = (
    "run_name",
    "results_path",
    "domain",
    "task_id",
    "trial",
    "seed",
    "agent_llm",
    "user_llm",
    "termination_reason",
    "is_infra_error",
    "reward",
    "success",
    "db_match",
    "n_messages",
    "n_agent_turns",
    "n_user_turns",
    "n_tool_calls",
    "agent_prompt_tokens",
    "agent_completion_tokens",
    "user_prompt_tokens",
    "user_completion_tokens",
    "duration_s",
    "failed_write_actions",
    "first_failed_action",
    "extra_tool_calls",
    "error_type",
    "error_stage",
)

PathLike = str | Path
OnDuplicates = Literal["raise", "warn", "ignore"]


class DuplicateTrialError(ValueError):
    """A results file repeats a (task_id, trial) pair or gives one trial several seeds."""


class DuplicateTrialWarning(UserWarning):
    """Emitted by :func:`load_runs` with ``on_duplicates='warn'``."""


def resolve_results_path(path: PathLike) -> Path:
    """Return the ``results.json`` path for a file or a run directory.

    Accepting a directory lets callers pass ``data/simulations/<run>`` directly.
    """
    p = Path(path).resolve()
    if p.is_dir():
        p = p / "results.json"
    if not p.is_file():
        raise FileNotFoundError(f"tau2 results file not found: {p}")
    return p


def read_results(path: PathLike) -> dict[str, Any]:
    """Read and minimally validate one tau2 ``results.json`` file."""
    p = resolve_results_path(path)
    with p.open(encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:  # e.g. file still being written by tau2
            raise ValueError(f"{p} is not valid JSON (is the run still writing?): {exc}") from exc
    missing = {"info", "simulations"} - set(data)
    if missing:
        raise ValueError(f"{p} does not look like a tau2 results file; missing {sorted(missing)}")
    return data


def _token_sums(messages: Iterable[Mapping[str, Any]], role: str) -> tuple[int, int]:
    """Sum (prompt_tokens, completion_tokens) over messages of one role.

    Messages without a ``usage`` block (e.g. the scripted greeting, tool results)
    contribute zero.
    """
    prompt = completion = 0
    for msg in messages:
        if msg.get("role") != role:
            continue
        usage = msg.get("usage") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    return prompt, completion


def _count_role(messages: Iterable[Mapping[str, Any]], role: str) -> int:
    return sum(1 for msg in messages if msg.get("role") == role)


def _count_tool_calls(messages: Iterable[Mapping[str, Any]]) -> int:
    """Number of tool calls issued by the agent (assistant messages only)."""
    return sum(
        len(msg.get("tool_calls") or []) for msg in messages if msg.get("role") == "assistant"
    )


def _failed_actions(reward_info: Mapping[str, Any] | None) -> tuple[tuple[str, ...], str | None]:
    """Return (failed write action names, first failed action name) from ``action_checks``."""
    if not reward_info:
        return (), None
    checks = reward_info.get("action_checks") or []
    failed = [c for c in checks if c.get("action_match") is False]
    write_names = tuple(c["action"]["name"] for c in failed if c.get("tool_type") == "write")
    first = failed[0]["action"]["name"] if failed else None
    return write_names, first


def _extra_tool_calls(
    messages: Iterable[Mapping[str, Any]], reward_info: Mapping[str, Any] | None
) -> tuple[str, ...]:
    """Agent tool-call names left over after removing one per gold action name."""
    if not reward_info:
        return ()
    gold = Counter(
        c["action"]["name"]
        for c in reward_info.get("action_checks") or []
        if c.get("action", {}).get("requestor", "assistant") == "assistant"
    )
    extra: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            name = call.get("name")
            if gold[name] > 0:
                gold[name] -= 1
            else:
                extra.append(name)
    return tuple(extra)


def _error_info(sim: Mapping[str, Any], is_infra: bool) -> tuple[str | None, str | None]:
    """Return (error_type, error_stage) for an infrastructure error, else (None, None)."""
    if not is_infra:
        return None, None
    err = sim.get("info") or {}
    traceback = str(err.get("error_traceback") or "").replace("\\", "/")
    stage = "evaluation" if "tau2/evaluator" in traceback else "simulation"
    return err.get("error_type"), stage


def simulation_to_row(
    sim: Mapping[str, Any], info: Mapping[str, Any], results_path: Path
) -> dict[str, Any]:
    """Flatten one tau2 simulation dict into a row dict with keys :data:`COLUMNS`."""
    reward_info = sim.get("reward_info")
    termination = sim.get("termination_reason")
    is_infra = termination == INFRA_ERROR or reward_info is None

    if is_infra:
        reward: float = float("nan")
        success: Any = pd.NA
        db_match: Any = pd.NA
    else:
        raw_reward = reward_info.get("reward")
        reward = float(raw_reward) if raw_reward is not None else float("nan")
        success = bool(reward == 1.0)
        db_check = reward_info.get("db_check") or {}
        db_match = db_check.get("db_match", pd.NA)
        db_match = pd.NA if db_match is None else bool(db_match)

    messages = sim.get("messages") or []
    agent_p, agent_c = _token_sums(messages, "assistant")
    user_p, user_c = _token_sums(messages, "user")
    failed_writes, first_failed = _failed_actions(reward_info)
    error_type, error_stage = _error_info(sim, is_infra)
    duration = sim.get("duration")

    return {
        "run_name": results_path.parent.name,
        "results_path": str(results_path),
        "domain": (info.get("environment_info") or {}).get("domain_name"),
        "task_id": str(sim.get("task_id")),
        "trial": int(sim.get("trial") or 0),
        "seed": sim.get("seed"),
        "agent_llm": (info.get("agent_info") or {}).get("llm"),
        "user_llm": (info.get("user_info") or {}).get("llm"),
        "termination_reason": termination,
        "is_infra_error": bool(is_infra),
        "reward": reward,
        "success": success,
        "db_match": db_match,
        "n_messages": len(messages),
        "n_agent_turns": _count_role(messages, "assistant"),
        "n_user_turns": _count_role(messages, "user"),
        "n_tool_calls": _count_tool_calls(messages),
        "agent_prompt_tokens": agent_p,
        "agent_completion_tokens": agent_c,
        "user_prompt_tokens": user_p,
        "user_completion_tokens": user_c,
        "duration_s": float(duration) if duration is not None else float("nan"),
        "failed_write_actions": failed_writes,
        "first_failed_action": first_failed,
        "extra_tool_calls": _extra_tool_calls(messages, reward_info),
        "error_type": error_type,
        "error_stage": error_stage,
    }


def trial_integrity_problems(df: pd.DataFrame) -> list[str]:
    """Describe duplicated trials in a frame from :func:`load_runs` (empty list if clean).

    Two problems are checked per ``results_path``:

    * the same ``(task_id, trial)`` appears more than once (e.g. simulations appended
      by a tau2 resume with a different seed), which would silently count as extra
      trials and change pass^k and flip rates;
    * one trial index carries more than one seed (tau2 uses one seed per trial for all
      tasks), i.e. the file mixes runs made with different base seeds.

    Rows with a missing seed are ignored by the second check.
    """
    problems: list[str] = []
    for path, grp in df.groupby("results_path", sort=False):
        dup = grp[grp.duplicated(["task_id", "trial"], keep=False)]
        if len(dup):
            pairs = sorted(
                {(str(t), int(r)) for t, r in zip(dup["task_id"], dup["trial"], strict=True)}
            )
            problems.append(
                f"{path}: {len(pairs)} (task_id, trial) pair(s) occur more than once, "
                f"e.g. {pairs[:5]}"
            )
        seeds = grp.dropna(subset=["seed"]).groupby("trial")["seed"].nunique()
        multi = sorted(int(t) for t in seeds[seeds > 1].index)
        if multi:
            problems.append(
                f"{path}: trial index(es) {multi} have more than one seed "
                "(runs with different base seeds were mixed into one file)"
            )
    return problems


def load_runs(
    paths: PathLike | Iterable[PathLike], on_duplicates: OnDuplicates = "raise"
) -> pd.DataFrame:
    """Load one or more tau2 results files into a DataFrame (one row per simulation).

    Args:
        paths: A ``results.json`` path or run directory, or an iterable of them.
        on_duplicates: What to do when :func:`trial_integrity_problems` finds a
            repeated ``(task_id, trial)`` or a trial with several seeds in one file:
            ``"raise"`` (default) raises :class:`DuplicateTrialError`, ``"warn"``
            emits :class:`DuplicateTrialWarning` and keeps every row, ``"ignore"``
            keeps every row silently.

    Returns:
        DataFrame with columns :data:`COLUMNS`, sorted by (results_path, task_id, trial).
        ``success`` and ``db_match`` use pandas' nullable ``boolean`` dtype so that
        infrastructure errors are ``<NA>`` rather than False.
    """
    if on_duplicates not in ("raise", "warn", "ignore"):
        raise ValueError(f"on_duplicates must be raise/warn/ignore, got {on_duplicates!r}")
    if isinstance(paths, str | Path):
        paths = [paths]
    rows: list[dict[str, Any]] = []
    for path in paths:
        results_path = resolve_results_path(path)
        data = read_results(results_path)
        info = data.get("info") or {}
        rows.extend(simulation_to_row(sim, info, results_path) for sim in data["simulations"])

    df = pd.DataFrame(rows, columns=list(COLUMNS))
    df["success"] = df["success"].astype("boolean")
    df["db_match"] = df["db_match"].astype("boolean")
    df["reward"] = df["reward"].astype(float)
    df["trial"] = df["trial"].astype(int)
    df["is_infra_error"] = df["is_infra_error"].astype(bool)
    problems = trial_integrity_problems(df) if on_duplicates != "ignore" else []
    if problems:
        msg = "; ".join(problems)
        if on_duplicates == "raise":
            raise DuplicateTrialError(
                msg + ". Use a fresh run name per config, or load with on_duplicates='warn'."
            )
        warnings.warn(msg, DuplicateTrialWarning, stacklevel=2)
    return _sort_rows(df)


def _sort_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Sort by run, then task id (numeric ids numerically: '2' < '10'), then trial."""
    numeric_id = pd.to_numeric(df["task_id"], errors="coerce")
    keyed = df.assign(_non_numeric=numeric_id.isna(), _numeric_id=numeric_id)
    keyed = keyed.sort_values(
        ["results_path", "_non_numeric", "_numeric_id", "task_id", "trial"], kind="stable"
    )
    return keyed.drop(columns=["_non_numeric", "_numeric_id"]).reset_index(drop=True)


def outcomes_by_task(df: pd.DataFrame, exclude_infra: bool = True) -> dict[str, list[bool]]:
    """Group binary outcomes by task, ordered by trial.

    Args:
        df: Frame from :func:`load_runs` (typically filtered to a single run).
        exclude_infra: Drop infrastructure-error rows (their outcome is unknown).
            If False, infra errors are counted as failures, which is the pessimistic
            convention used by some leaderboards.

    Returns:
        ``{task_id: [success_trial0, success_trial1, ...]}``. Tasks with no usable
        trials are omitted. Insertion order follows the frame's task order.
    """
    sub = df[~df["is_infra_error"]] if exclude_infra else df
    out: dict[str, list[bool]] = {}
    for row in sub.sort_values("trial", kind="stable").itertuples(index=False):
        ok = row.success
        out.setdefault(str(row.task_id), []).append(bool(ok) if not pd.isna(ok) else False)
    # Re-order keys to match first appearance in the (task-sorted) input frame.
    order = list(dict.fromkeys(str(t) for t in sub["task_id"]))
    return {t: out[t] for t in order}
