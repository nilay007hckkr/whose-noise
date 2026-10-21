"""First-divergence attribution of re-run noise (H1c, exploratory).

Implements RESEARCH_NOTES section 5. Take two repeats of the same cell (task,
simulator and, optionally, condition) and walk their message lists in order. Before
the first index ``m`` where they differ the two conversations are identical, so both
generators at ``m`` saw the same conversation. The role of message ``m`` therefore
names the component that behaved differently first:

``assistant``
    The agent (llama-server) answered the same conversation differently.
``user``
    The user simulator (Groq) answered the same conversation differently.
``tool``
    The environment returned a different tool result for identical tool calls. This is
    not expected (the retail tools are deterministic): it is a bug to report, and
    :func:`divergence_pairs` emits :class:`ToolDivergenceWarning` for it.
``length``
    One conversation is a strict prefix of the other (one side stopped).
``identical``
    No difference at all (the index is None).
``other``
    The message at ``m`` has a role outside the three above (e.g. ``system``). Never
    expected in tau2 text runs; kept so that one odd message cannot crash an analysis.

Only the FIRST divergence can be attributed: after it the inputs differ, and later
differences are consequences.

What "the same conversation" does and does not cover
----------------------------------------------------
* :func:`normalize` compares the role, the content with surrounding whitespace
  stripped (``None`` counts as ``""``), and the tool calls as an ordered tuple of
  ``(name, canonical JSON of the arguments)``. Ids, timestamps, ``usage``,
  ``generation_time_seconds``, ``cost``, ``raw_data``, ``turn_idx`` and every other key
  are ignored.
* The system prompts are not in the message list; they are fixed per task (agent
  policy, user scenario), so they are the same for two repeats of a cell.
* tau2 gives every trial its own seed and forwards it as ``seed`` to BOTH the agent and
  the user LLM call (``LLMConfigMixin.set_seed``). Two repeats therefore send identical
  messages with different ``seed`` values. At temperature 0 llama.cpp is expected to
  sample greedily, ignoring the seed (not verified for the pinned build), and Groq
  documents ``seed`` as best-effort. A divergence is therefore attributed to a
  component, but this design cannot tell serving nondeterminism from a seed effect
  inside that component.

Missing data (PREREGISTRATION section 8)
----------------------------------------
Infrastructure errors are missing, never failures. By default a task is used only
when EVERY cell of that task (each simulator / condition present in the data) has the
target number of valid repeats; otherwise the whole task is dropped from every cell,
with an :class:`~whose_noise.variance.UnbalancedDesignWarning` that lists it. Cells
with more repeats than the target keep the first ones by trial index.
:func:`task_completeness` reports the per-cell counts behind that decision.

Uncertainty
-----------
:func:`summarize_divergence` reports the shares with percentile task-cluster bootstrap
intervals: whole tasks (all their pairs, in every simulator and condition) are
resampled together, with indices from :func:`whose_noise.variance.task_bootstrap_indices`
(seed 20261011 by default, B = 10,000). Shares are ratios of pair counts, so the pairs
of a cell (which share repeats) are never treated as independent. With fewer than ~20
tasks the percentile interval under-covers.
"""

from __future__ import annotations

import itertools
import json
import math
import warnings
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from whose_noise.tau2_results import DuplicateTrialError, read_results, resolve_results_path
from whose_noise.variance import (
    DEFAULT_BOOTSTRAP_SEED,
    UnbalancedDesignWarning,
    task_bootstrap_indices,
)

__all__ = [
    "ALL_LABELS",
    "COMPLETENESS_COLUMNS",
    "DIVERGENCE_LABELS",
    "IDENTICAL",
    "LENGTH",
    "OTHER",
    "PAIR_COLUMNS",
    "POOLED",
    "ROLE_LABELS",
    "SHARE_NAMES",
    "IdenticalVerdictMismatchWarning",
    "ToolDivergenceWarning",
    "canonical_arguments",
    "diff_fields",
    "divergence_pairs",
    "first_divergence",
    "load_messages",
    "normalize",
    "summarize_divergence",
    "task_completeness",
    "tool_divergences",
]

ROLE_LABELS: tuple[str, ...] = ("user", "assistant", "tool")
LENGTH = "length"
IDENTICAL = "identical"
OTHER = "other"
DIVERGENCE_LABELS: tuple[str, ...] = (*ROLE_LABELS, LENGTH, OTHER)
"""Labels of pairs that diverge (the denominator of every share)."""
ALL_LABELS: tuple[str, ...] = (*DIVERGENCE_LABELS, IDENTICAL)

SHARE_NAMES: dict[str, str] = {
    "user": "share_user",
    "assistant": "share_agent",
    "tool": "share_tool",
    "length": "share_length",
}
"""Label -> name of its share column in :func:`summarize_divergence`."""

POOLED = "pooled"
"""Group value of the pooled row in :func:`summarize_divergence`."""

PAIR_COLUMNS: tuple[str, ...] = (
    "task",
    "task_id",
    "simulator",
    "condition",
    "run_name",
    "trial_i",
    "trial_j",
    "success_i",
    "success_j",
    "verdict_differs",
    "index",
    "label",
    "diff_fields",
    "n_messages_i",
    "n_messages_j",
)

COMPLETENESS_COLUMNS: tuple[str, ...] = (
    "task",
    "simulator",
    "condition",
    "n_runs",
    "n_infra",
    "n_valid",
    "target_reps",
    "cell_complete",
    "task_complete",
)

MessageKey = tuple[str, str, int]
"""``(run_name, task_id, trial)``; ``run_name`` is the results file's directory name."""
NormalizedCall = tuple[Any, str]
NormalizedMessage = tuple[Any, str, tuple[NormalizedCall, ...]]
Messages = Mapping[MessageKey, Sequence[Mapping[str, Any]]]
OnIncomplete = Literal["drop", "keep"]
PathLike = str | Path
_Cell = tuple[Hashable, Hashable, Hashable]  # (task, simulator, condition)


class ToolDivergenceWarning(UserWarning):
    """A pair first diverges at a TOOL message: environment nondeterminism (a bug)."""


class IdenticalVerdictMismatchWarning(UserWarning):
    """Two identical conversations got different verdicts: grader nondeterminism (a bug)."""


# ---------------------------------------------------------------------------
# Message normalisation and the first divergence
# ---------------------------------------------------------------------------


def canonical_arguments(arguments: Any) -> str:
    """Canonical text of tool-call arguments, independent of key order and encoding.

    A string that parses as JSON is parsed first, so ``'{"b": 1, "a": 2}'`` and
    ``{"a": 2, "b": 1}`` give the same text. The value is then dumped with
    ``json.dumps(sort_keys=True, ensure_ascii=False)`` (keys sorted at every level,
    list order kept). A string that is not valid JSON is returned unchanged; it can
    never equal a JSON dump, because every dump is valid JSON.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def _call_name_and_arguments(call: Mapping[str, Any]) -> tuple[Any, Any]:
    """``(name, arguments)`` of a tau2 tool call, or of an OpenAI-style ``function`` call."""
    if "name" in call or "arguments" in call:
        return call.get("name"), call.get("arguments")
    function = call.get("function") or {}
    return function.get("name"), function.get("arguments")


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)


def normalize(message: Mapping[str, Any]) -> NormalizedMessage:
    """The parts of a message that the first-divergence comparison looks at.

    Returns ``(role, content, tool_calls)``: the content with leading and trailing
    whitespace stripped (``None`` -> ``""``; non-string content as canonical JSON) and
    the tool calls as an ordered tuple of ``(name, canonical_arguments(arguments))``
    (``None`` and ``[]`` are both ``()``). Every other key (ids, timestamps, ``usage``,
    ``generation_time_seconds``, ``cost``, ``raw_data``, ``turn_idx``, ...) is ignored.
    Inner whitespace is kept: a different line break inside a reply is a real
    difference in generated text.
    """
    calls = tuple(
        (name, canonical_arguments(args))
        for name, args in (_call_name_and_arguments(c) for c in message.get("tool_calls") or [])
    )
    return message.get("role"), _content_text(message.get("content")), calls


def diff_fields(a: Mapping[str, Any], b: Mapping[str, Any]) -> str | None:
    """Which normalised fields differ between two messages, e.g. ``"content+tool_calls"``.

    ``None`` when the normalised messages are equal.
    """
    names = ("role", "content", "tool_calls")
    differ = [f for f, x, y in zip(names, normalize(a), normalize(b), strict=True) if x != y]
    return "+".join(differ) or None


def first_divergence(
    msgs_a: Sequence[Mapping[str, Any]], msgs_b: Sequence[Mapping[str, Any]]
) -> tuple[int | None, str]:
    """Index and label of the first message where two conversations differ.

    Returns:
        ``(index, label)``. ``label`` is the role of ``msgs_a[index]`` (``"user"``,
        ``"assistant"`` or ``"tool"``; ``"other"`` for any other role) at the first index
        whose :func:`normalize` differs; ``(len(shorter), "length")`` when one list is a
        strict prefix of the other; ``(None, "identical")`` when they are equal.
    """
    for i, (a, b) in enumerate(zip(msgs_a, msgs_b, strict=False)):
        if normalize(a) != normalize(b):
            role = a.get("role")
            return i, role if role in ROLE_LABELS else OTHER
    if len(msgs_a) != len(msgs_b):
        return min(len(msgs_a), len(msgs_b)), LENGTH
    return None, IDENTICAL


# ---------------------------------------------------------------------------
# Loading messages
# ---------------------------------------------------------------------------


def load_messages(
    paths: PathLike | Iterable[PathLike],
) -> dict[MessageKey, list[dict[str, Any]]]:
    """Message lists of every simulation, keyed by ``(run_name, task_id, trial)``.

    ``run_name`` is the results file's directory name, as in
    :func:`whose_noise.tau2_results.load_runs`, so the keys match its rows. Infra-error
    simulations are included (often with an empty list).

    Raises:
        DuplicateTrialError: A key occurs twice (a repeated ``(task_id, trial)`` in one
            file, or two results files in directories with the same name).
    """
    if isinstance(paths, str | Path):
        paths = [paths]
    out: dict[MessageKey, list[dict[str, Any]]] = {}
    for path in paths:
        results_path = resolve_results_path(path)
        run_name = results_path.parent.name
        for sim in read_results(results_path)["simulations"]:
            key = (run_name, str(sim.get("task_id")), int(sim.get("trial") or 0))
            if key in out:
                raise DuplicateTrialError(
                    f"{results_path}: messages for {key} occur more than once (a repeated "
                    "(task_id, trial), or two results files in directories with the same name)"
                )
            out[key] = list(sim.get("messages") or [])
    return out


# ---------------------------------------------------------------------------
# Cells, completeness and pairs
# ---------------------------------------------------------------------------


def _natural_key(value: Any) -> tuple[int, float, str]:
    """Sort numeric-looking labels numerically ('2' < '10'), everything else after, as text."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (1, math.inf, str(value))
    if math.isnan(number):
        return (1, math.inf, str(value))
    return (0, number, str(value))


def _natural_sorted(values: Iterable[Hashable]) -> list[Hashable]:
    return sorted(dict.fromkeys(values), key=_natural_key)


def _cell_order(cell: _Cell) -> tuple[tuple[int, float, str], ...]:
    """Sort cells by simulator, then condition, then task."""
    task, simulator, condition = cell
    return (_natural_key(simulator), _natural_key(condition), _natural_key(task))


def _check_columns(df: pd.DataFrame, cols: Iterable[str | None]) -> None:
    missing = [c for c in dict.fromkeys(cols) if c is not None and c not in df.columns]
    if missing:
        raise KeyError(f"missing columns {missing}; available: {list(df.columns)}")


def _records(
    runs: pd.DataFrame,
    task_col: str,
    sim_col: str,
    condition_col: str | None,
    extra: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Row dicts of ``runs`` with their cell under the key ``"_cell"``."""
    _check_columns(
        runs,
        ["run_name", "task_id", "trial", "is_infra_error", task_col, sim_col, condition_col]
        + list(extra),
    )
    records = runs.to_dict("records")
    for rec in records:
        cell = (rec[task_col], rec[sim_col], rec[condition_col] if condition_col else None)
        for name, value in zip(("task_col", "sim_col", "condition_col"), cell, strict=True):
            if (name != "condition_col" or condition_col) and bool(pd.isna(value)):
                raise ValueError(f"{name} has a missing value in a row of task {rec['task_id']}")
        rec["_cell"] = cell
    return records


def _completeness_rows(
    records: Sequence[Mapping[str, Any]], n_reps: int | None
) -> list[dict[str, Any]]:
    """Per-cell counts over the grid tasks x observed (simulator, condition) combinations."""
    if not records:
        return []
    tasks = _natural_sorted(rec["_cell"][0] for rec in records)
    combos = sorted(
        dict.fromkeys(rec["_cell"][1:] for rec in records),
        key=lambda sc: (_natural_key(sc[0]), _natural_key(sc[1])),
    )
    n_runs = Counter(rec["_cell"] for rec in records)
    n_infra = Counter(rec["_cell"] for rec in records if bool(rec["is_infra_error"]))
    rows = [
        {
            "task": task,
            "simulator": sim,
            "condition": cond,
            "n_runs": n_runs[(task, sim, cond)],
            "n_infra": n_infra[(task, sim, cond)],
            "n_valid": n_runs[(task, sim, cond)] - n_infra[(task, sim, cond)],
        }
        for sim, cond in combos
        for task in tasks
    ]
    target = int(n_reps) if n_reps is not None else max(r["n_valid"] for r in rows)
    incomplete_tasks = {r["task"] for r in rows if r["n_valid"] < target}
    for r in rows:
        r["target_reps"] = target
        r["cell_complete"] = r["n_valid"] >= target
        r["task_complete"] = r["task"] not in incomplete_tasks
    return rows


def task_completeness(
    runs: pd.DataFrame,
    *,
    task_col: str = "task_id",
    sim_col: str = "user_llm",
    condition_col: str | None = None,
    n_reps: int | None = None,
) -> pd.DataFrame:
    """Valid repeats per cell and whether each task is complete (PREREGISTRATION section 8).

    The cells of a task are the task crossed with every ``(simulator, condition)``
    combination that occurs anywhere in ``runs`` (infra-error rows included), so a task
    that one simulator has not run yet shows that cell with ``n_valid == 0``.

    Args:
        runs: Frame from :func:`whose_noise.tau2_results.load_runs` (one or more runs).
        task_col: Column identifying the task across cells (use a stripped id when the
            conditions use different task ids, e.g. ``5_en`` / ``5_hi``).
        sim_col: Column identifying the user simulator.
        condition_col: Optional column for a further condition (e.g. the RQ2 arm, or
            ``run_name``).
        n_reps: Target valid repeats per cell; None uses the largest valid count of any
            cell (the design's repeat count once at least one cell is finished).

    Returns:
        One row per cell with :data:`COMPLETENESS_COLUMNS`: ``n_runs`` simulations,
        ``n_infra`` infra errors, ``n_valid`` valid ones, ``target_reps``,
        ``cell_complete`` (``n_valid >= target_reps``) and ``task_complete`` (every cell
        of the task complete). Sorted by simulator, condition and task.
    """
    if n_reps is not None and n_reps < 1:
        raise ValueError(f"n_reps must be >= 1, got {n_reps}")
    rows = _completeness_rows(_records(runs, task_col, sim_col, condition_col), n_reps)
    return pd.DataFrame(rows, columns=list(COMPLETENESS_COLUMNS))


def _incomplete_message(rows: Sequence[Mapping[str, Any]], dropped: Sequence[Hashable]) -> str:
    parts = []
    for task in dropped:
        counts = ", ".join(
            f"{r['simulator']}{'' if r['condition'] is None else '/' + str(r['condition'])}"
            f"={r['n_valid']}"
            for r in rows
            if r["task"] == task
        )
        parts.append(f"{task} ({counts})")
    n_tasks = len({r["task"] for r in rows})
    return (
        f"{len(dropped)} of {n_tasks} task(s) have a cell with fewer than "
        f"{rows[0]['target_reps']} valid repeats and are dropped whole from every cell "
        "(PREREGISTRATION section 8): " + "; ".join(parts)
    )


def divergence_pairs(
    runs: pd.DataFrame,
    messages: Messages | None = None,
    *,
    task_col: str = "task_id",
    sim_col: str = "user_llm",
    condition_col: str | None = None,
    n_reps: int | None = None,
    on_incomplete: OnIncomplete = "drop",
    carry: Sequence[str] = (),
) -> pd.DataFrame:
    """First divergence of every unordered pair of valid repeats in every cell.

    A cell is ``(task, simulator[, condition])``. Infrastructure errors are never
    paired. With ``on_incomplete="drop"`` (the pre-registered rule) only tasks whose
    every cell has ``target`` valid repeats are used (see :func:`task_completeness`),
    each cell keeps its first ``target`` repeats by trial index, and dropped tasks are
    listed in an :class:`~whose_noise.variance.UnbalancedDesignWarning`. With
    ``"keep"`` (exploratory) every cell with at least 2 valid repeats contributes all
    its pairs (only the first ``n_reps`` when ``n_reps`` is given).

    Emits :class:`ToolDivergenceWarning` when a pair is labelled ``tool`` and
    :class:`IdenticalVerdictMismatchWarning` when identical conversations got different
    verdicts; both mean a nondeterministic environment or grader.

    Args:
        runs: Frame from :func:`whose_noise.tau2_results.load_runs`.
        messages: From :func:`load_messages`; None loads them from the frame's
            ``results_path`` column.
        task_col, sim_col, condition_col, n_reps: As in :func:`task_completeness`.
        on_incomplete: ``"drop"`` (default) or ``"keep"``.
        carry: Further columns of ``runs`` to copy per repeat as ``<col>_i``/``<col>_j``.

    Returns:
        One row per pair, columns :data:`PAIR_COLUMNS` (then the carried ones):
        ``task`` (value of ``task_col``, the bootstrap cluster), ``task_id`` (tau2's
        id), ``simulator``, ``condition`` (None without ``condition_col``),
        ``run_name``, ``trial_i < trial_j``, ``success_i``, ``success_j``,
        ``verdict_differs``, ``index`` (nullable Int64; missing when identical),
        ``label`` (one of :data:`ALL_LABELS`), ``diff_fields`` (see :func:`diff_fields`;
        missing for ``identical`` and ``length``) and the two message counts. Sorted by
        simulator, condition, task and trials.

    Raises:
        ValueError: A cell holds repeats from more than one run (pass
            ``condition_col="run_name"`` or filter the frame), or a bad argument.
        KeyError: A valid simulation has no entry in ``messages``.
    """
    if on_incomplete not in ("drop", "keep"):
        raise ValueError(f"on_incomplete must be 'drop' or 'keep', got {on_incomplete!r}")
    if n_reps is not None and n_reps < 2:
        raise ValueError(f"n_reps must be >= 2 to form pairs, got {n_reps}")
    records = _records(runs, task_col, sim_col, condition_col, ["success", *carry])
    if messages is None:
        _check_columns(runs, ["results_path"])
        messages = load_messages(list(dict.fromkeys(runs["results_path"])))

    cells: dict[_Cell, list[dict[str, Any]]] = {}
    for rec in records:
        if not bool(rec["is_infra_error"]):
            cells.setdefault(rec["_cell"], []).append(rec)
    mixed = [cell for cell, reps in cells.items() if len({r["run_name"] for r in reps}) > 1]
    if mixed:
        raise ValueError(
            f"cells {mixed[:5]} hold repeats from more than one run; pass "
            "condition_col='run_name' or filter the frame to one run per simulator/condition"
        )

    target = n_reps
    if on_incomplete == "drop" and records:
        rows = _completeness_rows(records, n_reps)
        target = int(rows[0]["target_reps"])
        dropped = _natural_sorted(r["task"] for r in rows if not r["task_complete"])
        if dropped:
            warnings.warn(_incomplete_message(rows, dropped), UnbalancedDesignWarning, stacklevel=2)
            cells = {cell: reps for cell, reps in cells.items() if cell[0] not in set(dropped)}

    pair_rows: list[dict[str, Any]] = []
    for cell in sorted(cells, key=_cell_order):
        reps = sorted(cells[cell], key=lambda r: int(r["trial"]))
        if target is not None:
            reps = reps[:target]
        for a, b in itertools.combinations(reps, 2):
            pair_rows.append(_pair_row(a, b, messages, carry))

    carried = [f"{c}_{side}" for c in carry for side in ("i", "j")]
    pairs = pd.DataFrame(pair_rows, columns=[*PAIR_COLUMNS, *carried])
    pairs["index"] = pairs["index"].astype("Int64")
    for col in ("success_i", "success_j", "verdict_differs"):
        pairs[col] = pairs[col].astype(bool)
    for col in ("trial_i", "trial_j", "n_messages_i", "n_messages_j"):
        pairs[col] = pairs[col].astype(int)
    _warn_anomalies(pairs)
    return pairs


def _messages_for(messages: Messages, rec: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    key = (str(rec["run_name"]), str(rec["task_id"]), int(rec["trial"]))
    try:
        return messages[key]
    except KeyError:
        raise KeyError(f"no messages for simulation {key}") from None


def _pair_row(
    a: Mapping[str, Any], b: Mapping[str, Any], messages: Messages, carry: Sequence[str]
) -> dict[str, Any]:
    msgs_a, msgs_b = _messages_for(messages, a), _messages_for(messages, b)
    index, label = first_divergence(msgs_a, msgs_b)
    at_message = index is not None and label != LENGTH
    success_a, success_b = bool(a["success"]), bool(b["success"])
    task, simulator, condition = a["_cell"]
    row = {
        "task": task,
        "task_id": a["task_id"],
        "simulator": simulator,
        "condition": condition,
        "run_name": a["run_name"],
        "trial_i": int(a["trial"]),
        "trial_j": int(b["trial"]),
        "success_i": success_a,
        "success_j": success_b,
        "verdict_differs": success_a != success_b,
        "index": index,
        "label": label,
        "diff_fields": diff_fields(msgs_a[index], msgs_b[index]) if at_message else None,
        "n_messages_i": len(msgs_a),
        "n_messages_j": len(msgs_b),
    }
    for col in carry:
        row[f"{col}_i"] = a[col]
        row[f"{col}_j"] = b[col]
    return row


def _describe(rows: pd.DataFrame) -> str:
    items = [
        f"{r['run_name']} task {r['task_id']} trials {r['trial_i']}/{r['trial_j']}"
        + ("" if pd.isna(r["index"]) else f" at message {r['index']}")
        for r in rows.head(10).to_dict("records")
    ]
    return "; ".join(items) + (" ..." if len(rows) > 10 else "")


def _warn_anomalies(pairs: pd.DataFrame) -> None:
    tool = tool_divergences(pairs)
    if len(tool):
        warnings.warn(
            f"{len(tool)} pair(s) first diverge at a TOOL message although every earlier "
            "message (including the tool calls) is identical: the environment is "
            f"nondeterministic, which is a bug to report: {_describe(tool)}",
            ToolDivergenceWarning,
            stacklevel=3,
        )
    same = pairs[(pairs["label"] == IDENTICAL) & pairs["verdict_differs"]]
    if len(same):
        warnings.warn(
            f"{len(same)} pair(s) have identical conversations but different verdicts: "
            f"the grader is nondeterministic, which is a bug to report: {_describe(same)}",
            IdenticalVerdictMismatchWarning,
            stacklevel=3,
        )


def tool_divergences(pairs: pd.DataFrame) -> pd.DataFrame:
    """Pairs whose first divergence is a tool result (environment nondeterminism)."""
    return pairs[pairs["label"] == "tool"]


# ---------------------------------------------------------------------------
# Summary with task-cluster bootstrap intervals
# ---------------------------------------------------------------------------

_SUBSETS = ("all", "verdict_differs")


def _bootstrap_weights(n_tasks: int, n_boot: int, seed: int) -> np.ndarray:
    """``(n_boot, n_tasks)`` multiplicity of each task in each bootstrap draw."""
    idx = task_bootstrap_indices(n_tasks, n_boot, seed)
    flat = (idx + n_tasks * np.arange(n_boot)[:, None]).ravel()
    return np.bincount(flat, minlength=n_boot * n_tasks).reshape(n_boot, n_tasks)


def _quantiles(draws: np.ndarray, alpha: float) -> tuple[float, float]:
    finite = draws[~np.isnan(draws)]
    if not finite.size:
        return math.nan, math.nan
    low, high = np.quantile(finite, [alpha / 2, 1 - alpha / 2])
    return float(low), float(high)


def summarize_divergence(
    pairs: pd.DataFrame,
    *,
    group_col: str = "simulator",
    cluster_col: str = "task",
    n_boot: int = 10_000,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> pd.DataFrame:
    """Label counts and shares per group and pooled, with task-cluster bootstrap CIs.

    ``share_user = #user / #diverging`` (likewise ``share_agent`` for ``assistant``,
    ``share_tool`` and ``share_length``), where diverging pairs are all pairs not
    labelled ``identical``. Computed for every pair (``subset == "all"``) and for the
    pairs whose verdicts differ (``subset == "verdict_differs"``), per value of
    ``group_col`` and for all groups together (``group == "pooled"``, pair-weighted).

    Each bootstrap draw resamples the tasks (``cluster_col``) with replacement, the
    same draw for every row, and recomputes every share as a ratio of summed counts.
    Draws with no diverging pair in a row define no share and are left out of that
    row's intervals (counted in ``n_boot_undefined``).

    Returns:
        One row per (subset, group): ``subset``, ``group``, ``n_tasks`` (tasks with at
        least one pair in the row), ``n_pairs``, ``n_identical``, ``n_diverging``,
        ``n_user``, ``n_assistant``, ``n_tool``, ``n_length``, ``n_other``, then for
        each share ``share_x``, ``share_x_ci_low``, ``share_x_ci_high``; finally
        ``n_boot_undefined``, ``n_boot``, ``seed`` and ``alpha``.
    """
    if pairs.empty:
        raise ValueError("no pairs to summarise")
    if n_boot < 1:
        raise ValueError(f"n_boot must be >= 1, got {n_boot}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    _check_columns(pairs, [group_col, cluster_col, "label", "verdict_differs"])
    unknown = set(pairs["label"]) - set(ALL_LABELS)
    if unknown:
        raise ValueError(f"unknown labels {sorted(unknown)}; expected {ALL_LABELS}")
    if pairs[group_col].isna().any():
        raise ValueError(f"{group_col!r} has missing values")
    groups = _natural_sorted(pairs[group_col])
    if POOLED in groups:
        raise ValueError(f"{group_col!r} may not contain the value {POOLED!r}")
    clusters = _natural_sorted(pairs[cluster_col])

    n_t, n_g, n_l = len(clusters), len(groups), len(ALL_LABELS)
    t_idx = pd.Categorical(pairs[cluster_col], categories=clusters).codes
    g_idx = pd.Categorical(pairs[group_col], categories=groups).codes
    l_idx = pd.Categorical(pairs["label"], categories=list(ALL_LABELS)).codes
    differs = pairs["verdict_differs"].astype(bool).to_numpy()
    counts = np.zeros((n_t, len(_SUBSETS), n_g + 1, n_l))
    for s, mask in enumerate((np.ones(len(pairs), dtype=bool), differs)):
        np.add.at(counts, (t_idx[mask], s, g_idx[mask], l_idx[mask]), 1.0)
    counts[:, :, n_g, :] = counts[:, :, :n_g, :].sum(axis=2)

    weights = _bootstrap_weights(n_t, n_boot, seed)
    boot = np.tensordot(weights, counts, axes=(1, 0))  # (n_boot, subsets, groups + 1, labels)
    div_pos = [ALL_LABELS.index(lab) for lab in DIVERGENCE_LABELS]
    lab_pos = {lab: ALL_LABELS.index(lab) for lab in ALL_LABELS}

    rows: list[dict[str, Any]] = []
    for s, subset in enumerate(_SUBSETS):
        for g, group in enumerate([*groups, POOLED]):
            total = counts[:, s, g, :].sum(axis=0)
            n_div = int(total[div_pos].sum())
            row: dict[str, Any] = {
                "subset": subset,
                "group": group,
                "n_tasks": int((counts[:, s, g, :].sum(axis=1) > 0).sum()),
                "n_pairs": int(total.sum()),
                "n_identical": int(total[lab_pos[IDENTICAL]]),
                "n_diverging": n_div,
                **{f"n_{lab}": int(total[lab_pos[lab]]) for lab in DIVERGENCE_LABELS},
            }
            den = boot[:, s, g, div_pos].sum(axis=-1)
            defined = den > 0
            for lab, name in SHARE_NAMES.items():
                draws = np.full(n_boot, np.nan)
                draws[defined] = boot[defined, s, g, lab_pos[lab]] / den[defined]
                row[name] = float(total[lab_pos[lab]] / n_div) if n_div else math.nan
                row[f"{name}_ci_low"], row[f"{name}_ci_high"] = _quantiles(draws, alpha)
            row["n_boot_undefined"] = int((~defined).sum())
            row.update({"n_boot": n_boot, "seed": seed, "alpha": alpha})
            rows.append(row)
    return pd.DataFrame(rows)
