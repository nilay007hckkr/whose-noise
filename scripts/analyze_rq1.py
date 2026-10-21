"""Pre-registered RQ1 analysis in one command (PREREGISTRATION.md, git tag ``prereg-v1``).

Usage (from the whose-noise repo root)::

    uv run python scripts/analyze_rq1.py \
        --gptoss <results.json of the gpt-oss-120b run> \
        --qwen <results.json of the qwen3.8-27b run> \
        [--out-dir results/rq1_analysis] [--n-boot 10000] [--analysis-timestamp ISO] \
        [--retries-exhausted TASK ...] [--rerun-evidence evidence.json]

It writes ``report.md`` (human readable) and ``results.json`` (machine readable) to
``--out-dir``. It only READS its input files: no tau2 process, no LLM call, no request
to llama-server and no git command. Pass snapshot copies of files that tau2 is still
writing. Results are deterministic given the inputs, ``--n-boot`` and ``--seed``; the
analysis timestamp is metadata and can be pinned with ``--analysis-timestamp``.

What it computes, in pre-registration order
-------------------------------------------
* Section 8 (missing data). A cell is (task, simulator) and needs R = 3 valid repeats
  (infrastructure errors are missing, not failures). Infra errors are re-run by tau2
  ``--auto-resume``, up to 3 extra attempts per cell, and only a cell that is still
  incomplete after that gets its task dropped WHOLE from every condition. tau2 deletes
  an infra-error simulation from results.json before re-running it, so the file cannot
  show how many re-runs were made: a cell that still holds an infra error is "awaiting
  re-run" (the data stay PARTIAL and nothing is confirmatory) until the user confirms
  that its re-runs are exhausted, with ``--retries-exhausted TASK ...`` or with
  ``extra_attempts >= 3`` in a ``--rerun-evidence`` file built from the run logs. Only
  confirmed tasks are dropped (listed, with the evidence; more than 3 dropped is a
  deviation). The data are FINAL when every planned task is complete or dropped.
  With PARTIAL data only tasks complete for both simulators are analysed, and every
  decision is labelled provisional.
* Step 1: simulations, infra errors and termination reasons per simulator. Infra-error
  counts in results.json are AFTER re-runs; counts BEFORE re-runs (section 8) come only
  from ``pre_rerun_infra_errors`` in a ``--rerun-evidence`` file, else they are reported
  as unavailable.
* Step 2: pass^1..pass^3 per simulator (``metrics.pass_hat_k``) with task-cluster
  bootstrap CIs (``metrics.cluster_bootstrap_ci``). Exploratory, NOT in the RQ1 analysis
  plan: the paired simulator contrast in pass^1 and pass^3
  (``metrics.paired_bootstrap_diff``), McNemar on per-task majority verdicts, and (with
  partial data only) pass^k on every task complete for one simulator.
* Step 3 (H1a, H1b): ``variance.decompose_variance(valid, sim_col="user_llm")``: four raw
  components, truncated and untruncated shares, p_bar, cell flip rate, within-cell and
  between-simulator disagreement. H1a / H1b decisions by the frozen rule (lower bound
  of the 95% CI > 0). ``excess_between_disagreement`` and ``flip_rate_mixed_tasks`` are
  exploratory (not in the plan).
* Section 11: p_bar floor / ceiling flag (p_bar < 0.1 or > 0.9 in an arm).
* Step 5: ``variance.noise_floor`` on the gpt-oss-120b cells (n_eval_tasks = analysed
  tasks; trials_per_run 3 and 1).
* Section 12: the required sensitivity analysis of the ``--cache-ram`` change
  (2026-10-12 20:23 IST): residual component and within-cell flip rate of simulations
  started before vs after. Descriptive: the period is confounded with task and
  simulator composition. The deviation entry does not fix a variant; the three variants
  reported were chosen after data collection began and none is primary.
* Exploratory: H1c first-divergence attribution (``whose_noise.divergence``; if that
  module cannot be imported or used, this section reports why and the rest still runs),
  split by the period class of the two repeats, with descriptive turn-order / exposure
  context; and the deterministic simulator-fidelity summary (``whose_noise.fidelity``,
  the logic of ``analyze --fidelity``).
* Step 8 (LLM-judge failure analysis) is out of scope here; see ``results/audit/``.

A run is labelled confirmatory only with FINAL data, the pre-registered task set, B,
seed and cache-ram cutoff, and no protocol warning.

Bootstrap settings (section 7): seed 20261011, B = 10,000, percentile intervals,
resampling tasks. Every bootstrap call uses the same seed. Timestamps in results.json
are naive local times; they are read as IST (UTC+05:30, the zone of the machine that
ran the experiment). Timestamps with an explicit offset are converted to IST.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import math
import platform
import sys
import warnings
import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd
import scipy

from whose_noise import fidelity, metrics, variance
from whose_noise.tau2_results import (
    DuplicateTrialError,
    load_runs,
    outcomes_by_task,
    read_results,
    resolve_results_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "whose_noise.rq1_analysis/v1"

# PREREGISTRATION section 4: the 29 judge-free retail test tasks, in split-file order.
PREREG_TASKS: tuple[str, ...] = (
    "5", "9", "12", "17", "18", "26", "27", "32", "33", "42", "49", "51", "53", "55", "56",
    "61", "64", "65", "71", "74", "77", "79", "86", "90", "94", "97", "100", "101", "102",
)  # fmt: skip
PREREG_REPEATS = 3  # section 5: R = 3 trials per cell
PREREG_N_BOOT = 10_000  # section 7
PREREG_SEED = variance.DEFAULT_BOOTSTRAP_SEED  # 20261011
ALPHA = 0.05
MAX_DROPPED_TASKS = 3  # section 8: more than 3 dropped RQ1 tasks is a deviation
P_BAR_LOW, P_BAR_HIGH = 0.1, 0.9  # section 11
MIN_TASKS_RELIABLE_CI = 20  # section 7: below this the percentile interval under-covers
MIN_TASKS_SENSITIVITY = 5  # section 12 classes with fewer tasks are flagged as too thin
EXPECTED_TAU2_COMMIT = "4ce7c039"  # section 16 header: RQ1 runs on main at 4ce7c039
EXPECTED_TAU2_SEED = 300
IST = timezone(timedelta(hours=5, minutes=30), "IST")
CACHE_RAM_CUTOFF = "2026-10-12T20:23:00"  # section 12, IST

GPTOSS, QWEN = "gpt-oss-120b", "qwen3.8-27b"
ROLES: tuple[str, ...] = (GPTOSS, QWEN)
ROLE_HINTS = {GPTOSS: "gpt-oss", QWEN: "qwen"}
ROLE_FLAGS = {GPTOSS: "--gptoss", QWEN: "--qwen"}

BEFORE, AFTER, MIXED, UNKNOWN = "before", "after", "mixed", "unknown"
PAIR_CLASSES: tuple[str, ...] = ("before_before", "after_after", "cross")
CELL_CLASSES: tuple[str, ...] = (BEFORE, AFTER, MIXED)

# Section 8. Cell states: COMPLETE, NOT_YET_RUN, INFRA_INCOMPLETE. Task statuses:
# COMPLETE, PENDING (a cell not yet run), AWAITING_RERUN (a cell still holds an infra
# error and its re-runs are not confirmed exhausted) and DROPPED (confirmed exhausted).
COMPLETE, PENDING, INFRA_INCOMPLETE = "complete", "pending", "infra_incomplete"
NOT_YET_RUN, AWAITING_RERUN, DROPPED = "not_yet_run", "awaiting_rerun", "dropped"
MAX_EXTRA_ATTEMPTS = 3  # section 8: up to 3 extra attempts per cell

EXPLORATORY = "exploratory (not in the RQ1 analysis plan)"
# tau2-bench main opens every (non-solo) conversation with this fixed agent message
# (tau2/orchestrator/orchestrator.py DEFAULT_FIRST_AGENT_MESSAGE): not a generation.
SCRIPTED_GREETING = "Hi! How can I help you today?"


class InputError(ValueError):
    """The inputs cannot be analysed as RQ1 (wrong files, duplicates, one simulator)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def natural_key(value: Any) -> tuple[int, float, str]:
    """Sort numeric-looking task ids numerically ('9' < '12'), others after them."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return (1, math.inf, str(value))
    return (0, number, str(value)) if math.isfinite(number) else (1, math.inf, str(value))


def natural_sorted(values: Sequence[Any]) -> list[Any]:
    return sorted(dict.fromkeys(values), key=natural_key)


def parse_time(value: Any) -> datetime | None:
    """ISO timestamp as a naive IST datetime (offsets converted, naive read as IST)."""
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def period_of(start: datetime | None, cutoff: datetime) -> str:
    if start is None:
        return UNKNOWN
    return BEFORE if start < cutoff else AFTER


def to_jsonable(obj: Any) -> Any:
    """Plain JSON types; NaN / inf / NA become None."""
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, bool | np.bool_):
        return bool(obj)
    if isinstance(obj, int | np.integer):
        return int(obj)
    if isinstance(obj, float | np.floating):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, Path):
        return str(obj)
    if obj is None or obj is pd.NA:
        return None
    return obj


def stat_dict(stat: variance.Statistic) -> dict[str, float]:
    return {"estimate": stat.estimate, "ci_low": stat.ci_low, "ci_high": stat.ci_high}


def fmt(x: Any, digits: int = 3) -> str:
    if x is None:
        return "n/a"
    try:
        value = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "n/a" if not math.isfinite(value) else f"{value:.{digits}f}"


def fmt_ci(d: Mapping[str, Any], digits: int = 3, key: str = "estimate") -> str:
    return (
        f"{fmt(d.get(key), digits)} [{fmt(d.get('ci_low'), digits)}, "
        f"{fmt(d.get('ci_high'), digits)}]"
    )


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def task_list(tasks: Sequence[Any]) -> str:
    return ", ".join(str(t) for t in tasks) if tasks else "(none)"


# ---------------------------------------------------------------------------
# Provenance: git commit read from .git files (git itself is never run)
# ---------------------------------------------------------------------------


def _packed_refs(git_dir: Path) -> dict[str, tuple[str, str | None]]:
    """``ref -> (sha, peeled sha or None)`` from ``packed-refs``."""
    out: dict[str, tuple[str, str | None]] = {}
    path = git_dir / "packed-refs"
    if not path.is_file():
        return out
    last: str | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("^") and last is not None:
            out[last] = (out[last][0], line[1:].strip())
            continue
        sha, _, ref = line.partition(" ")
        out[ref.strip()] = (sha.strip(), None)
        last = ref.strip()
    return out


def _resolve_ref(git_dir: Path, ref: str) -> tuple[str | None, str | None]:
    """``(sha, peeled)`` of a ref from a loose ref file or packed-refs."""
    loose = git_dir / ref
    if loose.is_file():
        text = loose.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("ref:"):
            return _resolve_ref(git_dir, text[4:].strip())
        return text, None
    return _packed_refs(git_dir).get(ref, (None, None))


def _peel_tag(git_dir: Path, sha: str, peeled: str | None) -> str:
    """Commit a tag points to: the packed-refs peel, or the loose tag object's target."""
    if peeled:
        return peeled
    obj = git_dir / "objects" / sha[:2] / sha[2:]
    if obj.is_file():
        try:
            raw = zlib.decompress(obj.read_bytes())
        except zlib.error:
            return sha
        header, _, body = raw.partition(b"\0")
        if header.startswith(b"tag "):
            for line in body.split(b"\n"):
                if line.startswith(b"object "):
                    return line[7:].decode("ascii", errors="replace").strip()
    return sha


def read_git_info(repo_root: Path, tag: str = "prereg-v1") -> dict[str, Any]:
    """HEAD commit, branch and the ``prereg-v1`` tag's commit, from ``.git`` files only."""
    info: dict[str, Any] = {
        "head_ref": None,
        "head_commit": None,
        "tag": tag,
        "tag_commit": None,
        "head_is_tag": None,
        "note": "read from .git files without running git; uncommitted changes are not "
        "visible this way (the sha256 of the analysis code is recorded instead)",
    }
    git_dir = repo_root / ".git"
    try:
        if git_dir.is_file():  # worktree or submodule: "gitdir: <path>"
            text = git_dir.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                git_dir = (repo_root / text.split(":", 1)[1].strip()).resolve()
        if not git_dir.is_dir():
            info["note"] = "no readable .git directory"
            return info
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            info["head_ref"] = head[4:].strip()
            info["head_commit"] = _resolve_ref(git_dir, info["head_ref"])[0]
        else:
            info["head_commit"] = head or None
        sha, peeled = _resolve_ref(git_dir, f"refs/tags/{tag}")
        if sha:
            info["tag_commit"] = _peel_tag(git_dir, sha, peeled)
        if info["head_commit"] and info["tag_commit"]:
            info["head_is_tag"] = info["head_commit"] == info["tag_commit"]
    except OSError as exc:
        info["note"] = f"could not read .git: {exc}"
    return info


def code_hashes() -> dict[str, str]:
    """sha256 of this script and of the whose_noise modules it relies on."""
    files = [Path(__file__).resolve()]
    package = Path(variance.__file__).resolve().parent
    files += [
        package / f"{name}.py"
        for name in ("metrics", "variance", "tau2_results", "fidelity", "divergence", "analyze")
    ]
    out: dict[str, str] = {}
    for path in files:
        try:
            key = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            key = path.name
        out[key] = sha256_file(path) if path.is_file() else "missing"
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputRun:
    role: str
    path_arg: str
    results_path: Path
    sha256: str
    run_name: str
    info: dict[str, Any]
    start_times: dict[tuple[str, int], str | None]


def read_input(role: str, path_arg: str) -> InputRun:
    results_path = resolve_results_path(path_arg)
    data = read_results(results_path)
    starts = {
        (str(sim.get("task_id")), int(sim.get("trial") or 0)): sim.get("start_time")
        or sim.get("timestamp")
        for sim in data.get("simulations") or []
    }
    return InputRun(
        role=role,
        path_arg=str(path_arg),
        results_path=results_path,
        sha256=sha256_file(results_path),
        run_name=results_path.parent.name,
        info=data.get("info") or {},
        start_times=starts,
    )


def load_rq1(
    inputs: Sequence[InputRun], cutoff: datetime
) -> tuple[pd.DataFrame, list[str], dict[str, str]]:
    """Long table of both runs with ``role``, ``start_time`` and ``period`` columns.

    Returns ``(runs, protocol_warnings, user_llm -> role)``.

    Raises:
        InputError: duplicate trials, several simulators in one file, or the same
            simulator in both files.
    """
    paths = [inp.results_path for inp in inputs]
    if len({p.parent.name for p in paths}) < len(paths):
        raise InputError("the two results files sit in directories with the same name")
    try:
        runs = load_runs(paths, on_duplicates="raise")
    except DuplicateTrialError as exc:
        raise InputError(f"duplicated trials: {exc}") from exc
    by_path = {str(inp.results_path): inp for inp in inputs}
    runs["role"] = runs["results_path"].map(lambda p: by_path[p].role)
    runs["start_time"] = [
        by_path[p].start_times.get((t, int(r)))
        for p, t, r in zip(runs["results_path"], runs["task_id"], runs["trial"], strict=True)
    ]
    runs["period"] = [period_of(parse_time(s), cutoff) for s in runs["start_time"]]

    problems: list[str] = []
    sim_role: dict[str, str] = {}
    for inp in inputs:
        sub = runs[runs["role"] == inp.role]
        llms = sorted({str(x) for x in sub["user_llm"].dropna()})
        if len(llms) != 1:
            raise InputError(f"{inp.role} file has user simulator(s) {llms}; expected exactly 1")
        if llms[0] in sim_role:
            raise InputError(
                f"both files use the same user simulator {llms[0]!r}: no simulator contrast"
            )
        sim_role[llms[0]] = inp.role
        if ROLE_HINTS[inp.role] not in llms[0].lower():
            problems.append(
                f"{ROLE_FLAGS[inp.role]} file uses simulator {llms[0]!r}, "
                f"which does not contain {ROLE_HINTS[inp.role]!r}: are the inputs swapped?"
            )
    problems += protocol_checks(inputs, runs)
    return runs, problems, sim_role


def protocol_checks(inputs: Sequence[InputRun], runs: pd.DataFrame) -> list[str]:
    """Compare the recorded run settings with PREREGISTRATION section 5 (warnings only)."""
    out: list[str] = []
    agents = sorted({str(x) for x in runs["agent_llm"].dropna()})
    if len(agents) != 1:
        out.append(f"agent differs between or within files: {agents} (RQ1 fixes the agent)")
    for inp in inputs:
        info = inp.info
        commit = str(info.get("git_commit") or "")
        if not commit.startswith(EXPECTED_TAU2_COMMIT):
            out.append(
                f"{inp.role}: tau2 git_commit {commit or 'not recorded'!s} is not "
                f"{EXPECTED_TAU2_COMMIT} (section 5)"
            )
        if info.get("seed") != EXPECTED_TAU2_SEED:
            out.append(f"{inp.role}: tau2 seed {info.get('seed')} != {EXPECTED_TAU2_SEED}")
        if info.get("num_trials") not in (None, PREREG_REPEATS):
            out.append(f"{inp.role}: num_trials {info.get('num_trials')} != {PREREG_REPEATS}")
        for side in ("agent_info", "user_info"):
            args = (info.get(side) or {}).get("llm_args") or {}
            if args.get("temperature") != 0.0:
                out.append(
                    f"{inp.role}: {side} temperature {args.get('temperature')} is not 0.0 "
                    "(H1a's estimand assumes temperature 0)"
                )
    return out


# ---------------------------------------------------------------------------
# Section 8: re-run evidence (what results.json cannot show)
# ---------------------------------------------------------------------------

EVIDENCE_KEYS = ("source", "pre_rerun_infra_errors", "extra_attempts")


@dataclass(frozen=True)
class RerunEvidence:
    """Section 8 facts that results.json cannot hold.

    tau2 ``--auto-resume`` deletes infra-error simulations from results.json before it
    re-runs them (tau2 ``runner/checkpoint.py``), so neither the number of re-run
    attempts per cell nor the infra-error count before re-runs is in the file.

    Attributes:
        retries_exhausted_cli: Tasks the user confirmed on the command line
            (``--retries-exhausted``): every cell of the task that still holds an infra
            error has had its 3 extra attempts.
        path, sha256, source: The ``--rerun-evidence`` file, its hash and its
            free-text ``source`` (how the numbers were obtained, e.g. from
            ``results/logs/<run_name>.log``).
        pre_rerun_infra_errors: role -> infra errors on FIRST attempts (before re-runs).
        extra_attempts: role -> task_id -> extra attempts made for that cell.
    """

    retries_exhausted_cli: tuple[str, ...] = ()
    path: str | None = None
    sha256: str | None = None
    source: str | None = None
    pre_rerun_infra_errors: Mapping[str, int] = field(default_factory=dict)
    extra_attempts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "retries_exhausted_cli": list(self.retries_exhausted_cli),
            "rerun_evidence_file": self.path,
            "rerun_evidence_sha256": self.sha256,
            "source": self.source,
            "pre_rerun_infra_errors": dict(self.pre_rerun_infra_errors),
            "extra_attempts": {r: dict(v) for r, v in self.extra_attempts.items()},
        }


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError(f"--rerun-evidence: {what} must be a non-negative integer, got {value!r}")
    return value


def _role_map(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InputError(f"--rerun-evidence: {what} must be an object keyed by simulator")
    unknown = sorted(set(value) - set(ROLES))
    if unknown:
        raise InputError(
            f"--rerun-evidence: {what} has unknown simulator(s) {unknown}; use {list(ROLES)}"
        )
    return dict(value)


def load_rerun_evidence(
    retries_exhausted: Sequence[Any] = (), path: str | Path | None = None
) -> RerunEvidence:
    """Validate ``--retries-exhausted`` and the optional ``--rerun-evidence`` JSON file.

    File format (every key but ``source`` optional; simulators are named by role)::

        {"source": "counted from results/logs/<run>.log on <date>",
         "pre_rerun_infra_errors": {"gpt-oss-120b": 3, "qwen3.8-27b": 6},
         "extra_attempts": {"qwen3.8-27b": {"33": 3}}}

    Raises:
        InputError: unreadable file, unknown key or simulator, or a bad count.
    """
    cli = tuple(dict.fromkeys(str(t) for t in retries_exhausted))
    if path is None:
        return RerunEvidence(retries_exhausted_cli=cli)
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read --rerun-evidence {p}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise InputError("--rerun-evidence must hold a JSON object")
    unknown = sorted(set(data) - set(EVIDENCE_KEYS))
    if unknown:
        raise InputError(f"--rerun-evidence: unknown key(s) {unknown}; allowed {EVIDENCE_KEYS}")
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise InputError("--rerun-evidence needs a non-empty 'source' (how it was obtained)")
    pre = {
        role: _count(n, f"pre_rerun_infra_errors[{role}]")
        for role, n in _role_map(
            data.get("pre_rerun_infra_errors", {}), "pre_rerun_infra_errors"
        ).items()
    }
    extra: dict[str, dict[str, int]] = {}
    for role, per_task in _role_map(data.get("extra_attempts", {}), "extra_attempts").items():
        if not isinstance(per_task, Mapping):
            raise InputError(f"--rerun-evidence: extra_attempts[{role}] must map task id -> count")
        extra[role] = {
            str(t): _count(n, f"extra_attempts[{role}][{t}]") for t, n in per_task.items()
        }
    return RerunEvidence(
        retries_exhausted_cli=cli,
        path=str(p),
        sha256=sha256_file(p),
        source=source.strip(),
        pre_rerun_infra_errors=pre,
        extra_attempts=extra,
    )


# ---------------------------------------------------------------------------
# Section 8: completeness, dropped and pending tasks
# ---------------------------------------------------------------------------


def _task_status(cell_states: Sequence[str], confirmed: bool) -> str:
    if INFRA_INCOMPLETE in cell_states and confirmed:
        return DROPPED
    if all(s == COMPLETE for s in cell_states):
        return COMPLETE
    if NOT_YET_RUN in cell_states:
        return PENDING
    return AWAITING_RERUN


def completeness(
    runs: pd.DataFrame,
    planned: Sequence[str],
    reps: int = PREREG_REPEATS,
    evidence: RerunEvidence | None = None,
) -> dict[str, Any]:
    """Classify every planned task by the section-8 rule.

    Per cell (task, simulator): ``complete`` (>= R valid repeats), ``not_yet_run``
    (fewer than R simulations exist at all) or ``infra_incomplete`` (R or more
    simulations exist but fewer than R are valid, i.e. an infra error is still in the
    file). A task is

    * ``dropped`` when a cell is infra-incomplete AND its re-runs are confirmed
      exhausted (``--retries-exhausted`` names the task, or the evidence file gives that
      cell ``extra_attempts >= 3``); one exhausted cell drops the whole task;
    * ``complete`` when both cells are complete;
    * ``pending`` when a cell is not yet run;
    * ``awaiting_rerun`` otherwise: tau2 ``--auto-resume`` re-runs the infra error with
      the same command, and nothing in results.json shows whether the 3 extra attempts
      have been used up.

    The data are ``final`` when every planned task is complete or dropped.
    ``complete_by_simulator`` never lists a dropped task.
    """
    evidence = evidence or RerunEvidence()
    cli = set(evidence.retries_exhausted_cli)
    rows: list[dict[str, Any]] = []
    for task in planned:
        row: dict[str, Any] = {"task_id": task}
        cell_states = []
        for role in ROLES:
            sub = runs[(runs["role"] == role) & (runs["task_id"] == task)]
            n_att = len(sub)
            n_infra = int(sub["is_infra_error"].sum())
            n_valid = n_att - n_infra
            valid = sub[~sub["is_infra_error"]]
            if n_valid >= reps:
                state = COMPLETE
            elif n_att < reps:
                state = NOT_YET_RUN
            else:
                state = INFRA_INCOMPLETE
            cell_states.append(state)
            row[role] = {
                "attempted": n_att,
                "infra": n_infra,
                "valid": n_valid,
                "successes": int(valid["success"].astype(bool).sum()) if n_valid else 0,
                "cell": state,
                "extra_attempts": evidence.extra_attempts.get(role, {}).get(task),
            }
        confirmations: list[dict[str, Any]] = []
        infra_roles = [r for r, s in zip(ROLES, cell_states, strict=True) if s == INFRA_INCOMPLETE]
        if infra_roles and task in cli:
            confirmations.append(
                {
                    "source": "--retries-exhausted (user confirmation)",
                    "simulators": infra_roles,
                }
            )
        for role in infra_roles:
            n_extra = row[role]["extra_attempts"]
            if n_extra is not None and n_extra >= MAX_EXTRA_ATTEMPTS:
                confirmations.append(
                    {
                        "source": f"--rerun-evidence {evidence.path} ({evidence.source})",
                        "simulators": [role],
                        "extra_attempts": n_extra,
                    }
                )
        row["status"] = _task_status(cell_states, bool(confirmations))
        row["retries_exhausted_evidence"] = confirmations
        rows.append(row)

    by_status = {
        s: [r["task_id"] for r in rows if r["status"] == s]
        for s in (COMPLETE, PENDING, AWAITING_RERUN, DROPPED)
    }
    final = not by_status[PENDING] and not by_status[AWAITING_RERUN]
    dropped = by_status[DROPPED]
    planned_set = set(planned)
    extra = natural_sorted([t for t in runs["task_id"].unique() if t not in planned_set])
    status_of = {r["task_id"]: r["status"] for r in rows}
    ignored = [
        {
            "task_id": t,
            "reason": "not a planned task"
            if t not in planned_set
            else f"task is {status_of[t]}: no cell still holds an infra error",
        }
        for t in evidence.retries_exhausted_cli
        if t not in planned_set or status_of[t] != DROPPED
    ]
    over_limit = [
        {"simulator": role, "task_id": t, "extra_attempts": n}
        for role, per_task in evidence.extra_attempts.items()
        for t, n in per_task.items()
        if n > MAX_EXTRA_ATTEMPTS
    ]
    return {
        "mode": "final" if final else "partial",
        "planned_tasks": list(planned),
        "n_planned": len(planned),
        "repeats_per_cell": reps,
        "analysed_tasks": by_status[COMPLETE],
        "n_analysed": len(by_status[COMPLETE]),
        "dropped_tasks": dropped,
        "n_dropped": len(dropped),
        "deviation_too_many_dropped": len(dropped) > MAX_DROPPED_TASKS,
        "pending_tasks": by_status[PENDING],
        "awaiting_rerun_tasks": by_status[AWAITING_RERUN],
        "infra_incomplete_tasks": natural_sorted(by_status[AWAITING_RERUN] + dropped),
        "extra_tasks_not_preregistered": extra,
        "complete_by_simulator": {
            role: [
                r["task_id"] for r in rows if r[role]["cell"] == COMPLETE and r["status"] != DROPPED
            ]
            for role in ROLES
        },
        "retries_exhausted_ignored": ignored,
        "extra_attempts_over_limit": over_limit,
        "per_task": rows,
    }


def analysis_frame(
    runs: pd.DataFrame, tasks: Sequence[str], reps: int = PREREG_REPEATS
) -> tuple[pd.DataFrame, int]:
    """Valid rows of ``tasks``, first ``reps`` repeats per cell by trial; also #cells cut."""
    valid = runs[runs["task_id"].isin(set(tasks)) & ~runs["is_infra_error"]].copy()
    valid = valid.sort_values(["role", "task_id", "trial"], kind="stable")
    pos = valid.groupby(["role", "task_id"], sort=False).cumcount()
    n_cut = int((valid.assign(_pos=pos).groupby(["role", "task_id"])["_pos"].max() >= reps).sum())
    valid = valid[pos < reps].copy()
    valid["success"] = valid["success"].astype(bool)
    order = {t: i for i, t in enumerate(natural_sorted(list(tasks)))}
    valid["_order"] = valid["task_id"].map(order)
    valid = valid.sort_values(["role", "_order", "trial"], kind="stable").drop(columns="_order")
    return valid.reset_index(drop=True), n_cut


# ---------------------------------------------------------------------------
# Step 1 and step 2
# ---------------------------------------------------------------------------


def condition_counts(sub: pd.DataFrame) -> dict[str, Any]:
    infra = sub[sub["is_infra_error"]]
    labels = infra["error_type"].fillna("unknown") + " @ " + infra["error_stage"].fillna("?")
    valid = sub[~sub["is_infra_error"]]
    return {
        "simulations": len(sub),
        "infra_errors": int(sub["is_infra_error"].sum()),
        "valid": len(valid),
        "successes": int(valid["success"].astype(bool).sum()) if len(valid) else 0,
        "tasks": int(sub["task_id"].nunique()),
        "termination_reasons": {
            str(k): int(v)
            for k, v in sorted(sub["termination_reason"].value_counts().items(), key=str)
        },
        "infra_breakdown": {str(k): int(v) for k, v in sorted(labels.value_counts().items())},
        "started_before_cutoff": int((sub["period"] == BEFORE).sum()),
        "started_after_cutoff": int((sub["period"] == AFTER).sum()),
        "start_time_unknown": int((sub["period"] == UNKNOWN).sum()),
    }


LOGGED_PRE_RERUN_INFRA = (
    "For reference only (read from the run_grid logs results/logs/<run_name>.log on "
    "2026-10-21, not recomputed by this script): gpt-oss-120b had 3 infra errors on first "
    "attempts (tasks 26, 27, 32, trial 2, all on 2026-10-12) and qwen3.8-27b had 6 (tasks "
    "12, 17, 18, 26, 27, 32, trial 2, all on 2026-10-11); each of those cells was refilled "
    "by the next --auto-resume (one extra attempt each)."
)


def pre_rerun_infra(
    runs: pd.DataFrame, evidence: RerunEvidence
) -> tuple[dict[str, Any], list[str]]:
    """Infra errors BEFORE re-runs per simulator (section 8), from ``--rerun-evidence`` only.

    Returns ``(block, problems)``; a problem is an evidence count below the number of
    infra errors still in the file (each of those is a (task, trial) whose first attempt
    failed, so it must be counted).
    """
    problems: list[str] = []
    per_sim: dict[str, Any] = {}
    for role in ROLES:
        sub = runs[runs["role"] == role]
        n_file, in_file = len(sub), int(sub["is_infra_error"].sum())
        n = evidence.pre_rerun_infra_errors.get(role)
        per_sim[role] = {
            "infra_errors_before_reruns": n,
            "simulations_in_file": n_file,
            "rate_before_reruns": n / n_file if n is not None and n_file else None,
            "infra_errors_in_file_now": in_file,
        }
        if n is not None and n < in_file:
            problems.append(
                f"{role}: --rerun-evidence gives {n} infra errors before re-runs, but the "
                f"file still holds {in_file} (each was a failed first attempt)"
            )
    available = all(per_sim[r]["infra_errors_before_reruns"] is not None for r in ROLES)
    return {
        "available": available,
        "source": evidence.source,
        "per_simulator": per_sim,
        "note": (
            "results.json cannot give these counts: tau2 --auto-resume deletes infra-error "
            "simulations before re-running them, so the file shows infra errors AFTER "
            "re-runs only."
        ),
        "rate_definition": (
            "the rate divides by the (task, trial) simulations in the file, each first "
            "attempted once"
        ),
        "logged_reference": None if available else LOGGED_PRE_RERUN_INFRA,
    }, problems


def pass_k_block(
    outcomes: Mapping[str, Sequence[bool]], ks: Sequence[int], n_boot: int, seed: int
) -> dict[str, Any]:
    out: dict[str, Any] = {"n_tasks": len(outcomes)}
    for k in ks:

        def stat(o: Mapping[Any, Sequence[bool]], k: int = k) -> float:
            return metrics.pass_hat_k(o, k)

        point, lo, hi = metrics.cluster_bootstrap_ci(
            outcomes, stat, n_boot=n_boot, alpha=ALPHA, seed=seed
        )
        out[f"pass^{k}"] = {"estimate": point, "ci_low": lo, "ci_high": hi}
    return out


def majority(outcomes: Mapping[str, Sequence[bool]]) -> dict[str, bool]:
    return {t: sum(v) * 2 > len(v) for t, v in outcomes.items()}


def step2(
    valid: pd.DataFrame,
    runs: pd.DataFrame,
    status: Mapping[str, Any],
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    ks = list(range(1, PREREG_REPEATS + 1))
    outcomes = {role: outcomes_by_task(valid[valid["role"] == role]) for role in ROLES}
    result: dict[str, Any] = {
        "per_simulator": {role: pass_k_block(outcomes[role], ks, n_boot, seed) for role in ROLES},
        "paired_contrast": {"direction": f"{GPTOSS} minus {QWEN}", "label": EXPLORATORY},
    }
    for k in (1, 3):
        diff, lo, hi = metrics.paired_bootstrap_diff(
            outcomes[GPTOSS], outcomes[QWEN], k=k, n_boot=n_boot, alpha=ALPHA, seed=seed
        )
        result["paired_contrast"][f"pass^{k}"] = {"estimate": diff, "ci_low": lo, "ci_high": hi}
    b01, b10, p_value = metrics.mcnemar_exact(majority(outcomes[GPTOSS]), majority(outcomes[QWEN]))
    result["mcnemar_majority"] = {
        "label": EXPLORATORY,
        "gptoss_fail_qwen_pass": b01,
        "gptoss_pass_qwen_fail": b10,
        "p_value": p_value,
    }
    # Partial data only: every task complete for ONE simulator (not the analysis set).
    # With final data this equals the analysis set, so it is left out. Dropped tasks are
    # never in complete_by_simulator (section 8: dropped from every condition).
    if status["mode"] != "final":
        supplementary: dict[str, Any] = {"label": EXPLORATORY}
        for role in ROLES:
            own = status["complete_by_simulator"][role]
            if len(own) >= 1:
                own_valid, _ = analysis_frame(runs[runs["role"] == role], own)
                supplementary[role] = pass_k_block(outcomes_by_task(own_valid), ks, n_boot, seed)
                supplementary[role]["tasks"] = own
        result["supplementary_own_complete_tasks"] = supplementary
    result["per_task"] = {
        role: {t: [bool(x) for x in v] for t, v in outcomes[role].items()} for role in ROLES
    }
    return result


# ---------------------------------------------------------------------------
# Step 3: decomposition, H1a / H1b, section 11
# ---------------------------------------------------------------------------


def _untruncated_share_stat(y: np.ndarray) -> np.ndarray:
    return variance.untruncated_shares(variance.crossed_components(y))


def step3(valid: pd.DataFrame, sim_role: Mapping[str, str], n_boot: int, seed: int) -> dict:
    dec = variance.decompose_variance(
        valid,
        sim_col="user_llm",
        n_reps=PREREG_REPEATS,
        on_unbalanced="raise",
        n_boot=n_boot,
        seed=seed,
        alpha=ALPHA,
    )
    arr = variance.to_crossed_array(
        valid, sim_col="user_llm", n_reps=PREREG_REPEATS, on_unbalanced="raise"
    )
    unt = variance.cluster_bootstrap(
        arr.y, _untruncated_share_stat, n_boot=n_boot, seed=seed, alpha=ALPHA
    )
    components: dict[str, Any] = {}
    for j, name in enumerate(variance.CROSSED_COMPONENTS):
        c = dec.components[name]
        components[name] = {
            "raw": c.raw,
            "ci_low": c.ci_low,
            "ci_high": c.ci_high,
            "frac_boot_nonpositive": c.frac_boot_nonpositive,
            "ci_excludes_zero": c.ci_excludes_zero,
            "truncated": c.truncated,
            "share_truncated": c.share,
            "share_truncated_ci_low": c.share_ci_low,
            "share_truncated_ci_high": c.share_ci_high,
            "share_untruncated": c.raw_share,
            "share_untruncated_ci_low": float(unt.ci_low[j]),
            "share_untruncated_ci_high": float(unt.ci_high[j]),
        }
    components["simulator"]["note"] = (
        "reported, not interpreted: with S = 2 simulators the task bootstrap treats them as "
        "fixed and this interval under-covers (PREREGISTRATION section 7 step 3)"
    )
    sim_means = {sim_role[str(s)]: float(v) for s, v in dec.simulator_means.items()}
    return {
        "n_tasks": dec.n_tasks,
        "n_simulators": dec.n_simulators,
        "n_reps": dec.n_reps,
        "simulators": {str(s): sim_role[str(s)] for s in dec.simulators},
        "p_bar": stat_dict(dec.p_bar),
        "components": components,
        "sum_of_raw_components": float(sum(c.raw for c in dec.components.values())),
        "p_bar_times_1_minus_p_bar": dec.p_bar.estimate * (1 - dec.p_bar.estimate),
        "cell_flip_rate": stat_dict(dec.flip_rate),
        "flip_rate_mixed_tasks": dec.flip_rate_mixed_tasks,
        "within_cell_disagreement": stat_dict(dec.within_cell_disagreement),
        "between_simulator_disagreement": stat_dict(dec.between_simulator_disagreement),
        "excess_between_disagreement": stat_dict(dec.excess_between_disagreement),
        "simulator_means": sim_means,
        "flip_rate_by_task": {str(t): float(v) for t, v in dec.flip_rate_by_task.items()},
        # Section 7 step 3 names cell_flip_rate, within_cell_disagreement and
        # between_simulator_disagreement; these two are extra.
        "labels": {
            "excess_between_disagreement": EXPLORATORY,
            "flip_rate_mixed_tasks": EXPLORATORY,
        },
    }


def decision(component: Mapping[str, Any], hypothesis: str, estimand: str) -> dict[str, Any]:
    lo = component["ci_low"]
    supported = bool(lo is not None and math.isfinite(lo) and lo > 0)
    return {
        "hypothesis": hypothesis,
        "estimand": estimand,
        "rule": "lower bound of the 95% task-bootstrap CI > 0",
        "estimate": component["raw"],
        "ci_low": component["ci_low"],
        "ci_high": component["ci_high"],
        "supported": supported,
        "verdict": "SUPPORTED" if supported else "NOT SUPPORTED",
        "reason": f"lower bound {fmt(lo, 4)} {'>' if supported else '<='} 0",
    }


def section11(p_bar: float, sim_means: Mapping[str, float]) -> dict[str, Any]:
    def outside(x: float) -> bool:
        return bool(math.isfinite(x) and (x < P_BAR_LOW or x > P_BAR_HIGH))

    arms = {role: {"p_bar": v, "outside": outside(v)} for role, v in sim_means.items()}
    flagged = outside(p_bar) or any(a["outside"] for a in arms.values())
    return {
        "rule": f"p_bar < {P_BAR_LOW} or > {P_BAR_HIGH} in an arm: components are floor- or "
        "ceiling-limited; shares are reported but flagged as uninformative",
        "pooled_p_bar": p_bar,
        "pooled_outside": outside(p_bar),
        "arms": arms,
        "shares_flagged_uninformative": flagged,
    }


# ---------------------------------------------------------------------------
# Step 5: noise floor
# ---------------------------------------------------------------------------


def step5(valid: pd.DataFrame, n_eval_tasks: int, n_boot: int, seed: int) -> dict[str, Any]:
    gpt = valid[valid["role"] == GPTOSS]
    out: dict[str, Any] = {"simulator": GPTOSS, "n_eval_tasks": n_eval_tasks}
    for k in (PREREG_REPEATS, 1):
        nf = variance.noise_floor(
            gpt,
            n_eval_tasks=n_eval_tasks,
            trials_per_run=k,
            sim_col=None,
            n_reps=PREREG_REPEATS,
            on_unbalanced="raise",
            n_boot=n_boot,
            seed=seed,
            alpha=ALPHA,
        )
        out[f"trials_per_run_{k}"] = {
            "residual": stat_dict(nf.residual),
            "sd_diff": stat_dict(nf.sd_diff),
            "expected_abs_diff": stat_dict(nf.expected_abs_diff),
            "band_95": stat_dict(nf.band),
            "level": nf.level,
        }
    return out


# ---------------------------------------------------------------------------
# Section 12: --cache-ram sensitivity (before vs after 2026-10-12 20:23 IST)
# ---------------------------------------------------------------------------


def pair_class(a: str, b: str) -> str | None:
    if UNKNOWN in (a, b):
        return None
    if a == b:
        return f"{a}_{a}"
    return "cross"


def _pair_stat(a: np.ndarray) -> np.ndarray:
    """Residual (= disagreement rate / 2) per pair class, then class 1 minus class 0.

    ``a`` is ``(..., T, classes, 2)`` with ``[n_pairs, n_disagreeing]`` per task and class.
    """
    n = a[..., 0].sum(axis=-2)
    d = a[..., 1].sum(axis=-2)
    with np.errstate(invalid="ignore", divide="ignore"):
        res = np.where(n > 0, d / np.where(n > 0, n, 1.0) / 2.0, np.nan)
    diff = res[..., 1] - res[..., 0]
    return np.concatenate([res, diff[..., None]], axis=-1)


def _cell_stat(a: np.ndarray, reps: int = PREREG_REPEATS) -> np.ndarray:
    """Per cell class: residual (MS_E), flip rate, p_bar; then after - before diffs."""
    s = a.sum(axis=-3)
    n_cells, ss, flips, succ, n_runs = (s[..., i] for i in range(5))
    with np.errstate(invalid="ignore", divide="ignore"):
        residual = np.where(
            n_cells > 0, ss / np.where(n_cells > 0, n_cells, 1.0) / (reps - 1), np.nan
        )
        flip = np.where(n_cells > 0, flips / np.where(n_cells > 0, n_cells, 1.0), np.nan)
        p_bar = np.where(n_runs > 0, succ / np.where(n_runs > 0, n_runs, 1.0), np.nan)
    diffs = np.stack([residual[..., 1] - residual[..., 0], flip[..., 1] - flip[..., 0]], axis=-1)
    return np.concatenate([residual, flip, p_bar, diffs], axis=-1)


def _boot_entry(boot: variance.BootstrapResult, i: int) -> dict[str, Any]:
    col = boot.draws[:, i]
    return {
        "estimate": float(boot.estimate[i]),
        "ci_low": float(boot.ci_low[i]),
        "ci_high": float(boot.ci_high[i]),
        "n_boot_undefined": int(np.isnan(col).sum()),
    }


def section12(
    valid: pd.DataFrame, cutoff_text: str, n_boot: int, seed: int, reps: int = PREREG_REPEATS
) -> dict[str, Any]:
    """Residual and within-cell flip rate before vs after the --cache-ram change.

    Pair level (uses every simulation): for each cell, each unordered pair of repeats is
    ``before_before``, ``after_after`` or ``cross`` by the two start times; the residual
    of a class is half its disagreement rate (``2 E[p(1-p)]`` = P(two repeats disagree)).
    Cell level: a cell is ``before`` / ``after`` when all its repeats started in that
    period, else ``mixed``; residual = MS_E and the cell flip rate of each class.
    Within mixed cells (matched on task and simulator): residual of the cross pairs
    versus the same-period pairs of the SAME cells. If the change shifted a cell's
    success probability, cross pairs disagree more (by about the squared shift).
    Intervals: task-cluster bootstrap (``variance.cluster_bootstrap``, same seed).
    """
    tasks = natural_sorted(valid["task_id"].tolist())
    t_pos = {t: i for i, t in enumerate(tasks)}
    pair_arr = np.zeros((len(tasks), len(PAIR_CLASSES), 2))
    matched_arr = np.zeros((len(tasks), 2, 2))  # mixed cells: [same period, cross]
    cell_arr = np.zeros((len(tasks), len(CELL_CLASSES), 5))
    comp_cells: dict[tuple[str, str], list[str]] = {}
    comp_pairs: dict[tuple[str, str], list[int]] = {}
    n_unknown_cells = 0
    for (role, task), grp in valid.groupby(["role", "task_id"], sort=False):
        grp = grp.sort_values("trial")
        ys = grp["success"].astype(float).to_numpy()
        periods = grp["period"].tolist()
        ti = t_pos[task]
        is_mixed = UNKNOWN not in periods and len(set(periods)) > 1
        for (ya, pa), (yb, pb) in itertools.combinations(zip(ys, periods, strict=True), 2):
            cls = pair_class(pa, pb)
            if cls is None:
                continue
            k = PAIR_CLASSES.index(cls)
            pair_arr[ti, k, 0] += 1
            pair_arr[ti, k, 1] += float(ya != yb)
            if is_mixed:
                j = 1 if cls == "cross" else 0
                matched_arr[ti, j, 0] += 1
                matched_arr[ti, j, 1] += float(ya != yb)
            counts = comp_pairs.setdefault((cls, role), [0, 0])
            counts[0] += 1
            counts[1] += int(ya != yb)
        if UNKNOWN in periods:
            n_unknown_cells += 1
            continue
        cell_cls = MIXED if is_mixed else periods[0]
        k = CELL_CLASSES.index(cell_cls)
        c = ys.sum()
        cell_arr[ti, k] += [1, ((ys - ys.mean()) ** 2).sum(), float(0 < c < len(ys)), c, len(ys)]
        comp_cells.setdefault((cell_cls, role), []).append(task)

    pair_boot = variance.cluster_bootstrap(
        pair_arr, _pair_stat, n_boot=n_boot, seed=seed, alpha=ALPHA
    )
    cell_boot = variance.cluster_bootstrap(
        cell_arr, lambda a: _cell_stat(a, reps), n_boot=n_boot, seed=seed, alpha=ALPHA
    )
    matched_boot = variance.cluster_bootstrap(
        matched_arr, _pair_stat, n_boot=n_boot, seed=seed, alpha=ALPHA
    )
    n_cls = len(PAIR_CLASSES)
    pair_level = {
        cls: {
            "n_pairs": int(pair_arr[:, k, 0].sum()),
            "n_disagreeing": int(pair_arr[:, k, 1].sum()),
            "n_tasks": int((pair_arr[:, k, 0] > 0).sum()),
            "residual": _boot_entry(pair_boot, k),
            "few_tasks": bool((pair_arr[:, k, 0] > 0).sum() < MIN_TASKS_SENSITIVITY),
        }
        for k, cls in enumerate(PAIR_CLASSES)
    }
    pair_level["after_after_minus_before_before"] = _boot_entry(pair_boot, n_cls)
    matched = {
        name: {
            "n_pairs": int(matched_arr[:, j, 0].sum()),
            "n_disagreeing": int(matched_arr[:, j, 1].sum()),
            "n_tasks": int((matched_arr[:, j, 0] > 0).sum()),
            "residual": _boot_entry(matched_boot, j),
        }
        for j, name in enumerate(("same_period", "cross"))
    }
    matched["cross_minus_same_period"] = _boot_entry(matched_boot, 2)
    m = len(CELL_CLASSES)
    cell_level = {
        cls: {
            "n_cells": int(cell_arr[:, k, 0].sum()),
            "n_tasks": int((cell_arr[:, k, 0] > 0).sum()),
            "residual": _boot_entry(cell_boot, k),
            "cell_flip_rate": _boot_entry(cell_boot, m + k),
            "p_bar": _boot_entry(cell_boot, 2 * m + k),
            "few_tasks": bool((cell_arr[:, k, 0] > 0).sum() < MIN_TASKS_SENSITIVITY),
            "p_bar_outside": bool(
                cell_arr[:, k, 4].sum() > 0
                and not P_BAR_LOW <= cell_arr[:, k, 3].sum() / cell_arr[:, k, 4].sum() <= P_BAR_HIGH
            ),
        }
        for k, cls in enumerate(CELL_CLASSES)
    }
    cell_level["after_minus_before"] = {
        "residual": _boot_entry(cell_boot, 3 * m),
        "cell_flip_rate": _boot_entry(cell_boot, 3 * m + 1),
    }
    composition = {
        "cells": [
            {
                "class": cls,
                "simulator": role,
                "n_cells": len(comp_cells.get((cls, role), [])),
                "tasks": natural_sorted(comp_cells.get((cls, role), [])),
            }
            for cls in CELL_CLASSES
            for role in ROLES
        ],
        "pairs": [
            {
                "class": cls,
                "simulator": role,
                "n_pairs": comp_pairs.get((cls, role), [0, 0])[0],
                "n_disagreeing": comp_pairs.get((cls, role), [0, 0])[1],
                "residual": (
                    comp_pairs[(cls, role)][1] / comp_pairs[(cls, role)][0] / 2
                    if comp_pairs.get((cls, role), [0, 0])[0]
                    else math.nan
                ),
            }
            for cls in PAIR_CLASSES
            for role in ROLES
        ],
        "simulations": {
            role: {
                p: int(((valid["role"] == role) & (valid["period"] == p)).sum())
                for p in (BEFORE, AFTER, UNKNOWN)
            }
            for role in ROLES
        },
    }
    before_tasks = {
        role: natural_sorted(
            valid.loc[(valid["role"] == role) & (valid["period"] == BEFORE), "task_id"].tolist()
        )
        for role in ROLES
    }
    return {
        "label": "descriptive (section 12 sensitivity analysis)",
        "variant_note": (
            "The deviation entry asks for a before/after comparison of the residual but "
            "does not fix how. The three variants here (pair level, within mixed cells, "
            "cell level) were chosen after RQ1 data collection began; none is primary."
        ),
        "cutoff_ist": cutoff_text,
        "cells_with_unknown_start_time": n_unknown_cells,
        "pair_level": pair_level,
        "within_mixed_cells": matched,
        "cell_level": cell_level,
        "composition": composition,
        "tasks_with_runs_before_cutoff": before_tasks,
        "confound_note": (
            "The period is not randomised: simulations before the change are the earliest "
            "runs (the first task chunk and, for each simulator, the first trials), and the "
            "after period holds all later tasks and calendar days. Period is therefore "
            "confounded with task, simulator mix, trial index and time (e.g. drift in the "
            "hosted simulator). Differences are descriptive and cannot be attributed to "
            "--cache-ram."
        ),
    }


# ---------------------------------------------------------------------------
# Exploratory: H1c (first divergence) and simulator fidelity
# ---------------------------------------------------------------------------


def import_divergence() -> tuple[ModuleType | None, str | None]:
    """``whose_noise.divergence`` or the reason it cannot be imported (never raises)."""
    try:
        return importlib.import_module("whose_noise.divergence"), None
    except Exception as exc:  # ImportError, or SyntaxError while it is being written
        return None, f"{type(exc).__name__}: {exc}"


def h1c(
    runs: pd.DataFrame,
    tasks: Sequence[str],
    paths: Sequence[Path],
    sim_role: Mapping[str, str],
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    """H1c share_user / share_agent (the pre-registered estimand), plus exploratory context.

    * ``summary``: per simulator and pooled, every analysed repeat pair.
    * ``by_period_class``: the same per simulator x period class of the two repeats
      (``before_before`` / ``cross`` / ``after_after`` around the section 12 cutoff). A
      cross pair compares two llama-server setups, not two re-runs under one setup.
    * ``after_after_only``: sensitivity row set using only after_after pairs.
    * ``context``: divergence-index distribution and identical-input generations per
      side (see :func:`divergence_context`).
    Every summary uses the same seed and B.
    """
    module, error = import_divergence()
    base = {"label": "exploratory (H1c)", "module": "whose_noise.divergence"}
    if module is None:
        return {**base, "status": "unavailable", "error": error}
    try:
        messages = module.load_messages(list(paths))
        sub = runs[runs["task_id"].isin(set(tasks))]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            pairs = module.divergence_pairs(
                sub,
                messages,
                sim_col="user_llm",
                n_reps=PREREG_REPEATS,
                on_incomplete="drop",
                carry=("period",),
            )
        pairs["simulator"] = [sim_role.get(str(s), str(s)) for s in pairs["simulator"]]
        pairs["period_class"] = [
            pair_class(str(a), str(b)) or UNKNOWN
            for a, b in zip(pairs["period_i"], pairs["period_j"], strict=True)
        ]
        pooled = getattr(module, "POOLED", "pooled")

        def summarize(df: pd.DataFrame, group_col: str) -> list[dict[str, Any]]:
            out = module.summarize_divergence(
                df, group_col=group_col, n_boot=n_boot, seed=seed, alpha=ALPHA
            )
            return out.to_dict("records")

        summary = summarize(pairs, "simulator")
        by_period = []
        sim_period = pairs.assign(_g=pairs["simulator"] + " | " + pairs["period_class"])
        for rec in summarize(sim_period, "_g"):
            if rec["group"] == pooled:  # all pairs again: same as the main pooled row
                continue
            rec["simulator"], rec["period_class"] = str(rec["group"]).split(" | ")
            by_period.append(rec)
        after_after = pairs[pairs["period_class"] == "after_after"]
        aa_rows = summarize(after_after, "simulator") if len(after_after) else []
        context = divergence_context(pairs, messages)
    except Exception as exc:  # the module is under development: report, do not crash
        return {**base, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    tool = pairs[pairs["label"] == "tool"]
    period_tasks = {
        role: {
            cls: natural_sorted(
                pairs.loc[
                    (pairs["simulator"] == role) & (pairs["period_class"] == cls), "task_id"
                ].tolist()
            )
            for cls in (*PAIR_CLASSES, UNKNOWN)
        }
        for role in ROLES
    }
    for role in ROLES:
        have = set(period_tasks[role]["after_after"])
        all_tasks = pairs.loc[pairs["simulator"] == role, "task_id"].tolist()
        period_tasks[role]["without_after_after"] = natural_sorted(
            [t for t in all_tasks if t not in have]
        )
    return {
        **base,
        "status": "ok",
        "n_pairs": len(pairs),
        "n_tasks": int(pairs["task"].nunique()) if len(pairs) else 0,
        "summary": summary,
        "by_period_class": by_period,
        "after_after_only": aa_rows,
        "period_tasks": period_tasks,
        "period_note": (
            "Pair classes by the start times of the two repeats around the section 12 "
            "cutoff. A cross pair compares two llama-server setups (prompt-cache restores "
            "can change the agent's floating-point results), not two re-runs under one "
            "setup. Period is confounded with task chunk, trial order and calendar date, "
            "so the split is descriptive."
        ),
        "context": context,
        "tool_divergences": [
            f"{r['simulator']} task {r['task_id']} trials "
            f"{r['trial_i']}/{r['trial_j']} at message {r['index']}"
            for r in tool.to_dict("records")
        ],
        "warnings": sorted({f"{w.category.__name__}: {w.message}" for w in caught}),
    }


GENERATION_SIDES = {"assistant": "agent", "user": "user"}


def _generation_side(message: Mapping[str, Any], position: int) -> str | None:
    """``agent`` / ``user`` for an LLM generation; None for tool results and the greeting.

    Message 0 is tau2's scripted greeting (no LLM call, so no ``usage``) when it is an
    assistant message without usage.
    """
    role = message.get("role")
    if position == 0 and role == "assistant" and not message.get("usage"):
        return None
    return GENERATION_SIDES.get(str(role))


def divergence_context(
    pairs: pd.DataFrame, messages: Mapping[tuple[str, str, int], Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    """Descriptive turn-order context for share_user / share_agent (exploratory).

    For each pair, every message up to and including the first divergence ``m`` was
    generated on IDENTICAL input in both repeats (for ``identical`` pairs: every
    message; for ``length`` pairs: messages before ``m``). Counting those generations
    per side gives the exposure each side had to diverge, and divergences / exposure is
    a per-generation divergence rate. The user simulator speaks first after the scripted
    greeting, so a user divergence at its first turn leaves the agent with no
    identical-input generation at all.

    Returns, per simulator and ``pooled``: ``generations`` (side -> identical-input
    generations, first divergences, rate), ``n_diverging``,
    ``n_diverging_before_any_agent_generation`` and ``divergence_index`` (label ->
    index -> number of pairs).
    """
    groups: dict[str, dict[str, Any]] = {}

    def group(name: str) -> dict[str, Any]:
        return groups.setdefault(
            name,
            {
                "exposure": Counter(),
                "diverged": Counter(),
                "n_diverging": 0,
                "n_before_agent": 0,
                "index": {},
            },
        )

    for rec in pairs.to_dict("records"):
        msgs = messages[(str(rec["run_name"]), str(rec["task_id"]), int(rec["trial_i"]))]
        label = str(rec["label"])
        if label == "identical":
            limit, m = len(msgs), None
        else:
            m = int(rec["index"])
            limit = m if label == "length" else m + 1
        exposure: Counter[str] = Counter()
        diverged: Counter[str] = Counter()
        for k in range(min(limit, len(msgs))):
            side = _generation_side(msgs[k], k)
            if side is None:
                continue
            exposure[side] += 1
            if k == m and label in GENERATION_SIDES:
                diverged[side] += 1
        for name in (str(rec["simulator"]), "pooled"):
            g = group(name)
            g["exposure"].update(exposure)
            g["diverged"].update(diverged)
            if label != "identical":
                g["n_diverging"] += 1
                g["index"].setdefault(label, Counter())[m] += 1
                if label == "user" and exposure["agent"] == 0:
                    g["n_before_agent"] += 1
    out: dict[str, Any] = {}
    for name in [*natural_sorted([n for n in groups if n != "pooled"]), "pooled"]:
        if name not in groups:
            continue
        g = groups[name]
        out[name] = {
            "generations": {
                side: {
                    "identical_input_generations": int(g["exposure"][side]),
                    "first_divergences": int(g["diverged"][side]),
                    "rate": g["diverged"][side] / g["exposure"][side]
                    if g["exposure"][side]
                    else math.nan,
                }
                for side in ("agent", "user")
            },
            "n_diverging": g["n_diverging"],
            "n_diverging_before_any_agent_generation": g["n_before_agent"],
            "divergence_index": {
                label: {int(i): int(n) for i, n in sorted(c.items())}
                for label, c in sorted(g["index"].items())
            },
        }
    return out


def _flag_ci(sub: pd.DataFrame, flag: str, n_boot: int, seed: int) -> dict[str, Any]:
    """Task-cluster bootstrap CI of the pooled rate of a fidelity flag (valid sims)."""
    sub = sub[~sub["is_infra_error"] & sub["customer_turns"].notna()]
    outcomes: dict[str, list[bool]] = {}
    for task, flag_value in zip(sub["task_id"], sub[flag], strict=True):
        value = False if pd.isna(flag_value) else bool(flag_value)
        outcomes.setdefault(str(task), []).append(value)
    if not outcomes:
        return {"estimate": math.nan, "ci_low": math.nan, "ci_high": math.nan, "n_tasks": 0}

    def pooled(o: Mapping[Any, Sequence[bool]]) -> float:
        values = [float(x) for v in o.values() for x in v]
        return float(np.mean(values))

    point, lo, hi = metrics.cluster_bootstrap_ci(
        outcomes, pooled, n_boot=n_boot, alpha=ALPHA, seed=seed
    )
    return {"estimate": point, "ci_low": lo, "ci_high": hi, "n_tasks": len(outcomes)}


def fidelity_section(
    runs: pd.DataFrame,
    tasks: Sequence[str],
    n_boot: int,
    seed: int,
    scope_tasks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Fidelity flags on the analysed tasks, and on every simulation of ``scope_tasks``.

    ``scope_tasks`` (default: every task in ``runs``) is the "all simulations" table; the
    caller passes the planned tasks minus those dropped under section 8.
    """
    base = {"label": "exploratory (deterministic transcript checks, analyze --fidelity logic)"}
    try:
        if scope_tasks is not None:
            runs = runs[runs["task_id"].isin(set(scope_tasks)) | runs["task_id"].isin(set(tasks))]
        fid = fidelity.add_fidelity(runs)
        out: dict[str, Any] = {
            **base,
            "status": "ok",
            "analysed_tasks": {},
            "all_simulations": {},
            "all_simulations_scope": (
                "every valid simulation of the planned tasks that are not dropped under "
                "section 8 (includes tasks outside the analysis set, e.g. pending ones)"
                if scope_tasks is not None
                else "every valid simulation in each file"
            ),
        }
        for role in ROLES:
            mine = fid[fid["role"] == role]
            analysed = mine[mine["task_id"].isin(set(tasks))]
            summary = fidelity.fidelity_summary(analysed)
            out["analysed_tasks"][role] = {
                "summary": summary.drop(columns=["results_path"], errors="ignore").to_dict(
                    "records"
                ),
                "text": fidelity.format_fidelity(analysed),
                "task_bootstrap_ci_all_outcomes": {
                    flag: _flag_ci(analysed, flag, n_boot, seed) for flag in fidelity.FLAG_COLUMNS
                },
            }
            out["all_simulations"][role] = {
                "summary": fidelity.fidelity_summary(mine)
                .drop(columns=["results_path"], errors="ignore")
                .to_dict("records"),
            }
        return out
    except Exception as exc:
        return {**base, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def step8_reference(repo_root: Path) -> dict[str, Any]:
    audit = repo_root / "results" / "audit"
    files = sorted(p.name for p in audit.glob("*")) if audit.is_dir() else []
    return {
        "status": "out of scope for this script",
        "reason": "step 8 labels failures with LLM judges; this script makes no LLM calls",
        "audit_dir": "results/audit/",
        "audit_files": files,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_analysis(
    gptoss: str,
    qwen: str,
    *,
    n_boot: int = PREREG_N_BOOT,
    seed: int = PREREG_SEED,
    planned: Sequence[str] = PREREG_TASKS,
    cutoff: str = CACHE_RAM_CUTOFF,
    analysis_timestamp: str | None = None,
    repo_root: Path = REPO_ROOT,
    retries_exhausted: Sequence[Any] = (),
    rerun_evidence: str | Path | None = None,
) -> dict[str, Any]:
    """Every RQ1 step; returns the JSON-ready result (raises :class:`InputError`)."""
    cutoff_dt = parse_time(cutoff)
    if cutoff_dt is None:
        raise InputError(f"cannot parse --cache-ram-cutoff {cutoff!r}")
    planned = [str(t) for t in planned]
    evidence = load_rerun_evidence(retries_exhausted, rerun_evidence)
    try:
        inputs = [read_input(GPTOSS, gptoss), read_input(QWEN, qwen)]
    except (OSError, ValueError) as exc:  # missing file, invalid JSON, not a tau2 file
        raise InputError(str(exc)) from exc
    caught_all: list[str] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runs, problems, sim_role = load_rq1(inputs, cutoff_dt)
        status = completeness(runs, planned, evidence=evidence)
        analysed = status["analysed_tasks"]
        valid, n_cut = analysis_frame(runs, analysed)
        pre_rerun, evidence_problems = pre_rerun_infra(runs, evidence)
        problems = problems + evidence_problems
        problems += [
            f"{o['simulator']}: task {o['task_id']} cell had {o['extra_attempts']} extra "
            f"attempts, more than the {MAX_EXTRA_ATTEMPTS} allowed by section 8"
            for o in status["extra_attempts_over_limit"]
        ]
        provisional_reasons = provisional(status, n_boot, seed, planned, cutoff_dt, problems)

        result: dict[str, Any] = {
            "schema": SCHEMA,
            "meta": {
                "analysis_timestamp": analysis_timestamp
                or datetime.now(IST).isoformat(timespec="seconds"),
                "preregistration": {
                    "file": "PREREGISTRATION.md",
                    "sha256": sha256_file(repo_root / "PREREGISTRATION.md")
                    if (repo_root / "PREREGISTRATION.md").is_file()
                    else None,
                },
                "repo_git": read_git_info(repo_root),
                "code_sha256": code_hashes(),
                "inputs": [
                    {
                        "role": inp.role,
                        "path": inp.path_arg,
                        "resolved_path": str(inp.results_path),
                        "sha256": inp.sha256,
                        "run_name": inp.run_name,
                        "user_llm": next(k for k, v in sim_role.items() if v == inp.role),
                        "agent_llm": (inp.info.get("agent_info") or {}).get("llm"),
                        "tau2_git_commit": inp.info.get("git_commit"),
                        "tau2_seed": inp.info.get("seed"),
                        "num_trials": inp.info.get("num_trials"),
                        "n_simulations": int((runs["role"] == inp.role).sum()),
                    }
                    for inp in inputs
                ],
                "settings": {
                    "n_boot": n_boot,
                    "seed": seed,
                    "alpha": ALPHA,
                    "interval": "percentile, resampling tasks",
                    "repeats_per_cell": PREREG_REPEATS,
                    "planned_tasks": planned,
                    "cache_ram_cutoff_ist": cutoff,
                    "rerun_evidence": evidence.as_dict(),
                },
                "software": {
                    "python": platform.python_version(),
                    "numpy": np.__version__,
                    "pandas": pd.__version__,
                    "scipy": scipy.__version__,
                },
            },
            "protocol_warnings": problems,
            "data_status": {**status, "cells_with_extra_repeats_truncated": n_cut},
            "confirmatory": not provisional_reasons,
            "provisional_reasons": provisional_reasons,
            "step1_conditions": {
                role: {
                    "file": condition_counts(runs[runs["role"] == role]),
                    "analysed_set": condition_counts(
                        runs[(runs["role"] == role) & runs["task_id"].isin(set(analysed))]
                    ),
                }
                for role in ROLES
            },
            "step1_pre_rerun_infra": pre_rerun,
            "step8_failure_analysis": step8_reference(repo_root),
        }
        if len(analysed) < 2:
            reason = f"only {len(analysed)} task(s) complete for both simulators; need >= 2"
            for key in ("step2", "step3", "decisions", "section11", "step5", "section12"):
                result[key] = {"status": "skipped", "reason": reason}
        else:
            result["step2"] = step2(valid, runs, status, n_boot, seed)
            s3 = step3(valid, sim_role, n_boot, seed)
            result["step3"] = s3
            result["decisions"] = {
                "H1a": decision(
                    s3["components"]["residual"], "H1a", "residual (re-run) component E[p(1-p)]"
                ),
                "H1b": decision(
                    s3["components"]["task_x_simulator"], "H1b", "task_x_simulator component"
                ),
            }
            result["section11"] = section11(s3["p_bar"]["estimate"], s3["simulator_means"])
            result["step5"] = step5(valid, len(analysed), n_boot, seed)
            result["section12"] = section12(valid, cutoff, n_boot, seed)
        paths = [inp.results_path for inp in inputs]
        result["h1c"] = (
            h1c(runs, analysed, paths, sim_role, n_boot, seed)
            if len(analysed) >= 1
            else {"status": "skipped", "reason": "no analysed task"}
        )
        dropped = set(status["dropped_tasks"])
        result["fidelity"] = fidelity_section(
            runs, analysed, n_boot, seed, scope_tasks=[t for t in planned if t not in dropped]
        )
        caught_all = [f"{w.category.__name__}: {w.message}" for w in caught]
    result["warnings"] = sorted(set(caught_all))
    return to_jsonable(result)


def provisional(
    status: Mapping[str, Any],
    n_boot: int,
    seed: int,
    planned: Sequence[str],
    cutoff: datetime | None,
    problems: Sequence[str],
) -> list[str]:
    """Why a run is NOT confirmatory (empty list = confirmatory).

    Confirmatory needs final data (section 8), the pre-registered B, seed, task set and
    cache-ram cutoff (sections 4, 7, 12), at most 3 dropped tasks, and no protocol
    warning (e.g. a temperature other than 0 breaks H1a's estimand).
    """
    reasons: list[str] = []
    if status["mode"] != "final":
        parts = []
        if status["pending_tasks"]:
            parts.append(f"{len(status['pending_tasks'])} task(s) not yet run")
        if status["awaiting_rerun_tasks"]:
            parts.append(
                f"{len(status['awaiting_rerun_tasks'])} task(s) awaiting --auto-resume re-runs "
                f"of infra errors ({task_list(status['awaiting_rerun_tasks'])}; section 8)"
            )
        reasons.append(
            f"partial data: {status['n_analysed']} of {status['n_planned']} planned tasks "
            "complete for both simulators (" + "; ".join(parts) + ")"
        )
    if n_boot != PREREG_N_BOOT:
        reasons.append(f"B = {n_boot}, not the pre-registered {PREREG_N_BOOT}")
    if seed != PREREG_SEED:
        reasons.append(f"seed {seed}, not the pre-registered {PREREG_SEED}")
    if tuple(planned) != PREREG_TASKS:
        reasons.append("task set differs from PREREGISTRATION section 4")
    if cutoff != parse_time(CACHE_RAM_CUTOFF):
        shown = cutoff.isoformat() if cutoff else "unparsed"
        reasons.append(
            f"cache-ram cutoff {shown} IST, not the pre-registered {CACHE_RAM_CUTOFF} IST "
            "(section 12)"
        )
    if status["deviation_too_many_dropped"]:
        reasons.append(f"{status['n_dropped']} tasks dropped (> {MAX_DROPPED_TASKS}): deviation")
    reasons += [f"protocol warning: {p}" for p in problems]
    return reasons


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _undercover_note(n_tasks: int) -> str:
    if n_tasks < MIN_TASKS_RELIABLE_CI:
        return (
            f" Intervals are approximate: {n_tasks} < {MIN_TASKS_RELIABLE_CI} tasks, so the "
            "percentile task bootstrap under-covers (section 7)."
        )
    return ""


def _header(res: Mapping[str, Any]) -> list[str]:
    meta = res["meta"]
    git = meta["repo_git"]
    settings = meta["settings"]
    status = res["data_status"]
    lines = ["# RQ1 analysis: whose noise is it?", ""]
    if status["mode"] != "final":
        lines += [
            f"> **PARTIAL DATA. Data collection is not finished (cells not yet run, or infra "
            f"errors awaiting `--auto-resume` re-runs). Only the "
            f"{status['n_analysed']} of {status['n_planned']} pre-registered tasks that are "
            f"complete for BOTH simulators are analysed:** {task_list(status['analysed_tasks'])}. "
            "Every number and decision below is provisional and is NOT the confirmatory RQ1 "
            "result.",
            "",
        ]
    if settings["n_boot"] != PREREG_N_BOOT:
        lines += [
            f"> **Preview bootstrap: B = {settings['n_boot']}** (pre-registered B = "
            f"{PREREG_N_BOOT}). Re-run with the default `--n-boot` for reportable intervals.",
            "",
        ]
    if res["confirmatory"]:
        lines += [
            "> Confirmatory run: final data, the pre-registered settings and no protocol warning.",
            "",
        ]
    else:
        lines += ["Provisional because: " + "; ".join(res["provisional_reasons"]) + ".", ""]
    tag_txt = (
        f"{git['tag']} -> {git['tag_commit']}"
        + (" (= HEAD)" if git.get("head_is_tag") else " (HEAD differs)")
        if git.get("tag_commit")
        else f"{git['tag']} not found"
    )
    rows = [
        ["Analysis timestamp", meta["analysis_timestamp"]],
        ["Repo commit (HEAD)", f"{git.get('head_commit') or 'unknown'} ({git.get('head_ref')})"],
        ["Pre-registration tag", tag_txt],
        ["Git note", git["note"]],
        ["PREREGISTRATION.md sha256", meta["preregistration"]["sha256"] or "missing"],
        [
            "Analysis code sha256 (first 12 hex; full in results.json)",
            "<br>".join(f"{k}: {v[:12]}" for k, v in meta["code_sha256"].items()),
        ],
        [
            "Bootstrap",
            f"seed {settings['seed']}, B = {settings['n_boot']}, {settings['interval']}, "
            f"95% intervals",
        ],
        ["Cache-ram cutoff (section 12)", f"{settings['cache_ram_cutoff_ist']} IST"],
    ]
    for inp in meta["inputs"]:
        rows.append(
            [
                f"Input {inp['role']}",
                f"`{inp['path']}`<br>sha256 `{inp['sha256']}`<br>run `{inp['run_name']}`, "
                f"user `{inp['user_llm']}`, agent `{inp['agent_llm']}`, "
                f"{inp['n_simulations']} simulations, tau2 commit "
                f"{str(inp['tau2_git_commit'])[:8]}, seed {inp['tau2_seed']}",
            ]
        )
    lines += [md_table(["Item", "Value"], rows), ""]
    return lines


def _summary_lines(res: Mapping[str, Any]) -> list[str]:
    status = res["data_status"]
    lines = ["## Key results", ""]
    lines.append(
        f"- Data: {status['mode'].upper()}; analysed tasks {status['n_analysed']} of "
        f"{status['n_planned']}; dropped {status['n_dropped']}; pending "
        f"{len(status['pending_tasks'])}; awaiting infra re-runs "
        f"{len(status['awaiting_rerun_tasks'])}."
    )
    if res["step3"].get("status") == "skipped":
        lines += [f"- Analyses skipped: {res['step3']['reason']}.", ""]
        return lines
    s2, s3 = res["step2"], res["step3"]
    for role in ROLES:
        block = s2["per_simulator"][role]
        lines.append(
            f"- pass^1 {role}: {fmt_ci(block['pass^1'])}; pass^3: {fmt_ci(block['pass^3'])}"
        )
    lines.append(
        f"- p_bar {fmt_ci(s3['p_bar'])}; residual "
        f"{fmt_ci(s3['components']['residual'], 4, 'raw')}; task_x_simulator "
        f"{fmt_ci(s3['components']['task_x_simulator'], 4, 'raw')}"
    )
    tag = "" if res["confirmatory"] else "PROVISIONAL "
    for h in ("H1a", "H1b"):
        d = res["decisions"][h]
        lines.append(f"- {tag}{h}: {d['verdict']} ({d['reason']})")
    s11 = res["section11"]
    lines.append(
        "- Section 11 floor/ceiling flag: "
        + (
            "FLAGGED (shares uninformative)"
            if s11["shares_flagged_uninformative"]
            else "not flagged"
        )
    )
    nf = res["step5"]["trials_per_run_1"]
    lines.append(
        f"- Noise floor (gpt-oss cells, {res['step5']['n_eval_tasks']} tasks, single runs): "
        f"95% band +/-{fmt(nf['band_95']['estimate'])}"
    )
    pl = res["section12"]["pair_level"]
    wm = res["section12"]["within_mixed_cells"]
    lines.append(
        "- Section 12 (descriptive; confounded with task/time; variants chosen post hoc, none "
        "primary): residual before_before "
        f"{fmt_ci(pl['before_before']['residual'], 4)} vs after_after "
        f"{fmt_ci(pl['after_after']['residual'], 4)}; within mixed cells, cross minus "
        f"same-period {fmt_ci(wm['cross_minus_same_period'], 4)}"
    )
    h = res["h1c"]
    if h.get("status") == "ok":
        pooled = _pooled_all(h["summary"])
        if pooled:
            text = (
                "- H1c (exploratory) share_user, pooled: "
                f"{fmt(pooled['share_user'])} [{fmt(pooled['share_user_ci_low'])}, "
                f"{fmt(pooled['share_user_ci_high'])}] of {pooled['n_diverging']} diverging pairs"
            )
            aa = _pooled_all(h.get("after_after_only") or [])
            if aa:
                text += (
                    f"; after_after pairs only {fmt(aa['share_user'])} "
                    f"[{fmt(aa['share_user_ci_low'])}, {fmt(aa['share_user_ci_high'])}] of "
                    f"{aa['n_diverging']} (the user simulator speaks first: see the H1c caveats)"
                )
            lines.append(text)
    else:
        lines.append(f"- H1c (exploratory): {h.get('status')}: {h.get('error') or h.get('reason')}")
    return [*lines, ""]


def _data_status_lines(res: Mapping[str, Any]) -> list[str]:
    status = res["data_status"]
    lines = ["## Section 8: missing data and the analysis set", ""]
    rule = (
        "Infra errors are re-run with the same command (`--auto-resume`), up to 3 extra "
        "attempts per cell; a task is dropped whole from every condition only when a cell is "
        "still incomplete after that. tau2 deletes an infra-error simulation from "
        "results.json before re-running it, so the file cannot show how many re-runs were "
        "made: a cell that still holds an infra error is **awaiting re-run** until the user "
        "confirms its re-runs are exhausted (`--retries-exhausted TASK ...`, or "
        "`extra_attempts >= 3` in a `--rerun-evidence` file built from the run logs)."
    )
    if status["mode"] == "final":
        lines.append(
            "**FINAL DATA:** every planned task is complete for both simulators or dropped "
            "under section 8. " + rule
        )
    else:
        lines.append(
            "**PARTIAL DATA:** some planned cells have not been run yet or still hold an "
            "infra error awaiting re-run. Only tasks complete for both simulators are "
            "analysed. " + rule
        )
    lines.append("")
    if status["mode"] != "final":
        lines.append(
            f"- Pending (not yet run for at least one simulator, "
            f"{len(status['pending_tasks'])}): {task_list(status['pending_tasks'])}"
        )
        lines.append(
            f"- Awaiting `--auto-resume` re-runs of infra errors "
            f"({len(status['awaiting_rerun_tasks'])}): "
            f"{task_list(status['awaiting_rerun_tasks'])}"
        )
    lines.append(f"- Dropped tasks ({status['n_dropped']}): {task_list(status['dropped_tasks'])}")
    for r in status["per_task"]:
        for ev in r["retries_exhausted_evidence"] if r["status"] == DROPPED else []:
            lines.append(
                f"  - task {r['task_id']}: re-runs confirmed exhausted for "
                f"{', '.join(ev['simulators'])} by {ev['source']}"
                + (f", {ev['extra_attempts']} extra attempts" if "extra_attempts" in ev else "")
            )
    if status["deviation_too_many_dropped"]:
        lines.append(
            f"- **DEVIATION: {status['n_dropped']} tasks dropped, more than "
            f"{MAX_DROPPED_TASKS} of {status['n_planned']} (section 8).**"
        )
    elif status["n_dropped"]:
        lines.append(f"- Not a deviation (at most {MAX_DROPPED_TASKS} tasks dropped).")
    for ig in status["retries_exhausted_ignored"]:
        lines.append(f"- `--retries-exhausted {ig['task_id']}` ignored: {ig['reason']}.")
    lines.append(
        f"- **Analysed (complete for both simulators, {status['n_analysed']}):** "
        f"{task_list(status['analysed_tasks'])}"
    )
    for role in ROLES:
        own = status["complete_by_simulator"][role]
        lines.append(f"- Complete for {role}, not dropped ({len(own)}): {task_list(own)}")
    if status["extra_tasks_not_preregistered"]:
        lines.append(
            "- Tasks in the data but not in the pre-registered set (excluded): "
            + task_list(status["extra_tasks_not_preregistered"])
        )
    if status["cells_with_extra_repeats_truncated"]:
        lines.append(
            f"- {status['cells_with_extra_repeats_truncated']} cell(s) had more than 3 valid "
            "repeats; the first 3 by trial index were kept."
        )
    lines.append("")
    rows = []
    for r in status["per_task"]:
        row = [r["task_id"]]
        for role in ROLES:
            c = r[role]
            row.append(
                f"{c['successes']}/{c['valid']} valid"
                + (f", {c['infra']} infra" if c["infra"] else "")
                + f" ({c['attempted']} run)"
                + (
                    f", {c['extra_attempts']} extra attempts"
                    if c.get("extra_attempts") is not None
                    else ""
                )
            )
        row.append(r["status"])
        rows.append(row)
    lines += [
        "Per task: successes / valid repeats (simulations run) for each simulator.",
        "",
        md_table(["task", GPTOSS, QWEN, "status"], rows),
        "",
    ]
    return lines


def _step1_lines(res: Mapping[str, Any]) -> list[str]:
    lines = ["## Step 1: simulations, infra errors, termination reasons", ""]
    rows = []
    for role in ROLES:
        for scope in ("file", "analysed_set"):
            c = res["step1_conditions"][role][scope]
            rows.append(
                [
                    role,
                    scope.replace("_", " "),
                    c["simulations"],
                    c["infra_errors"],
                    c["valid"],
                    c["successes"],
                    c["tasks"],
                    ", ".join(f"{k}={v}" for k, v in c["termination_reasons"].items()) or "-",
                    ", ".join(f"{k}={v}" for k, v in c["infra_breakdown"].items()) or "-",
                ]
            )
    lines += [
        md_table(
            [
                "simulator",
                "scope",
                "sims",
                "infra",
                "valid",
                "successes",
                "tasks",
                "termination reasons",
                "infra errors",
            ],
            rows,
        ),
        "",
        "Infra errors are missing data, not failures (section 3). The counts above are the "
        "current file state, i.e. AFTER `--auto-resume` re-runs replaced infra errors.",
        "",
        "**Infra errors before re-runs (section 8).**",
        "",
    ]
    pre = res["step1_pre_rerun_infra"]
    if pre["available"]:
        lines += [
            md_table(
                ["simulator", "infra before re-runs", "sims in file", "rate", "infra in file now"],
                [
                    [
                        role,
                        p["infra_errors_before_reruns"],
                        p["simulations_in_file"],
                        fmt(p["rate_before_reruns"]),
                        p["infra_errors_in_file_now"],
                    ]
                    for role, p in pre["per_simulator"].items()
                ],
            ),
            "",
            f"Source (`--rerun-evidence`): {pre['source']}. {pre['note']} The "
            f"{pre['rate_definition']}.",
            "",
        ]
    else:
        given = {
            role: p["infra_errors_before_reruns"]
            for role, p in pre["per_simulator"].items()
            if p["infra_errors_before_reruns"] is not None
        }
        lines += [
            "**Not available from results.json.** "
            + pre["note"]
            + " Pass `pre_rerun_infra_errors` for both simulators in a `--rerun-evidence` "
            "file counted from `results/logs/<run_name>.log`."
            + (f" Given for only: {given}." if given else ""),
            "",
            pre["logged_reference"],
            "",
        ]
    return lines


def _step2_lines(res: Mapping[str, Any]) -> list[str]:
    s2 = res["step2"]
    n = s2["per_simulator"][GPTOSS]["n_tasks"]
    lines = ["## Step 2: pass^k per simulator (task-cluster bootstrap 95% CI)", ""]
    rows = [
        [role, s2["per_simulator"][role]["n_tasks"]]
        + [fmt_ci(s2["per_simulator"][role][f"pass^{k}"]) for k in (1, 2, 3)]
        for role in ROLES
    ]
    lines += [
        md_table(["simulator", "tasks", "pass^1", "pass^2", "pass^3"], rows),
        "",
        "Unbiased pass^k estimator (Yao et al. 2024) averaged over the analysed tasks."
        + _undercover_note(n),
        "",
        f"### Exploratory, not in the RQ1 analysis plan: paired simulator contrast "
        f"({s2['paired_contrast']['direction']})",
        "",
        "Section 7 step 2 asks only for pass^k per condition in RQ1; the paired bootstrap "
        "and McNemar are pre-registered for H2, not for RQ1. These were added after RQ1 data "
        "collection began. An interval that excludes 0 here is not a pre-registered finding.",
        "",
    ]
    pc = s2["paired_contrast"]
    mc = s2["mcnemar_majority"]
    lines += [
        md_table(
            ["contrast", "estimate [95% CI]"],
            [["pass^1", fmt_ci(pc["pass^1"])], ["pass^3", fmt_ci(pc["pass^3"])]],
        ),
        "",
        f"McNemar on per-task majority verdicts (exploratory): gpt-oss fails & qwen passes "
        f"{mc['gptoss_fail_qwen_pass']}, gpt-oss passes & qwen fails "
        f"{mc['gptoss_pass_qwen_fail']}, exact p = {fmt(mc['p_value'])}.",
        "",
    ]
    supp = s2.get("supplementary_own_complete_tasks") or {}
    if any(role in supp for role in ROLES):
        lines += [
            "Supplementary, exploratory (partial data only; NOT the analysis set: each row "
            "uses every non-dropped task complete for that simulator, so the two rows cover "
            "different tasks and must not be compared):",
            "",
            md_table(
                ["simulator", "tasks", "pass^1", "pass^2", "pass^3"],
                [
                    [role, supp[role]["n_tasks"]]
                    + [fmt_ci(supp[role][f"pass^{k}"]) for k in (1, 2, 3)]
                    for role in ROLES
                    if role in supp
                ],
            ),
            "",
        ]
    return lines


def _step3_lines(res: Mapping[str, Any]) -> list[str]:
    s3 = res["step3"]
    flagged = res["section11"]["shares_flagged_uninformative"]
    lines = [
        "## Step 3: variance decomposition (Design A, H1a / H1b)",
        "",
        f"`variance.decompose_variance(valid, sim_col='user_llm')` on {s3['n_tasks']} tasks x "
        f"{s3['n_simulators']} simulators x {s3['n_reps']} repeats. Raw components are "
        "unbiased and may be negative; truncated shares are biased upward for small "
        "components; untruncated shares are approximately unbiased but may leave [0, 1]."
        + _undercover_note(s3["n_tasks"]),
        "",
    ]
    rows = []
    for name in variance.CROSSED_COMPONENTS:
        c = s3["components"][name]
        label = name + (" (not interpreted)" if name == "simulator" else "")
        rows.append(
            [
                label,
                fmt_ci(c, 4, "raw"),
                fmt(c["frac_boot_nonpositive"]),
                f"{fmt(c['share_truncated'])} [{fmt(c['share_truncated_ci_low'])}, "
                f"{fmt(c['share_truncated_ci_high'])}]",
                f"{fmt(c['share_untruncated'])} [{fmt(c['share_untruncated_ci_low'])}, "
                f"{fmt(c['share_untruncated_ci_high'])}]",
            ]
        )
    lines += [
        md_table(
            [
                "component",
                "raw [95% CI]",
                "frac boot <= 0",
                "share, truncated [CI]",
                "share, untruncated [CI]",
            ],
            rows,
        ),
        "",
    ]
    if flagged:
        lines += [
            "**Section 11 flag: the shares above are uninformative (p_bar floor/ceiling).**",
            "",
        ]
    lines += [
        md_table(
            ["statistic", "estimate [95% CI]"],
            [
                ["p_bar", fmt_ci(s3["p_bar"])],
                [f"cell_flip_rate (R = {s3['n_reps']})", fmt_ci(s3["cell_flip_rate"])],
                ["within_cell_disagreement", fmt_ci(s3["within_cell_disagreement"])],
                ["between_simulator_disagreement", fmt_ci(s3["between_simulator_disagreement"])],
                [
                    "excess_between_disagreement (exploratory, not in the plan)",
                    fmt_ci(s3["excess_between_disagreement"]),
                ],
                [
                    "flip_rate_mixed_tasks (point; exploratory, not in the plan)",
                    fmt(s3["flip_rate_mixed_tasks"]),
                ],
            ],
        ),
        "",
        "Simulator means: "
        + ", ".join(f"{k} {fmt(v)}" for k, v in s3["simulator_means"].items())
        + f". Sum of raw components {fmt(s3['sum_of_raw_components'], 4)} vs "
        f"p_bar (1 - p_bar) = {fmt(s3['p_bar_times_1_minus_p_bar'], 4)} (equal in "
        "expectation, not in a finite sample).",
        "",
        s3["components"]["simulator"]["note"] + ".",
        "",
    ]
    return lines


def _decision_lines(res: Mapping[str, Any]) -> list[str]:
    lines = ["## Pre-registered decisions (section 2)", ""]
    if not res["confirmatory"]:
        lines += [
            "**PROVISIONAL**: the frozen rule is applied mechanically, but these are not the "
            "confirmatory decisions (" + "; ".join(res["provisional_reasons"]) + ").",
            "",
        ]
    rows = [
        [
            d["hypothesis"],
            "confirmatory",
            d["estimand"],
            f"{fmt(d['estimate'], 4)} [{fmt(d['ci_low'], 4)}, {fmt(d['ci_high'], 4)}]",
            d["rule"],
            f"{d['verdict']} ({d['reason']})",
        ]
        for d in (res["decisions"]["H1a"], res["decisions"]["H1b"])
    ]
    lines += [
        md_table(["hypothesis", "type", "estimand", "estimate [95% CI]", "rule", "verdict"], rows),
        "",
        "A verdict of NOT SUPPORTED means not detected at this sample size, not evidence of "
        "absence. H1a is read together with the section 12 sensitivity analysis below. "
        "Everything else in this report is descriptive or exploratory.",
        "",
    ]
    s11 = res["section11"]
    lines += ["## Section 11: p_bar floor / ceiling check", "", s11["rule"] + ".", ""]
    rows = [["pooled (decomposition)", fmt(s11["pooled_p_bar"]), s11["pooled_outside"]]]
    rows += [[role, fmt(a["p_bar"]), a["outside"]] for role, a in s11["arms"].items()]
    lines += [
        md_table(["arm", "p_bar", f"outside [{P_BAR_LOW}, {P_BAR_HIGH}]"], rows),
        "",
        "Shares flagged as uninformative: "
        + ("**YES**" if s11["shares_flagged_uninformative"] else "no")
        + ".",
        "",
    ]
    return lines


def _step5_lines(res: Mapping[str, Any]) -> list[str]:
    s5 = res["step5"]
    lines = [
        "## Step 5: noise floor on the gpt-oss-120b cells",
        "",
        f"`variance.noise_floor(sim_col=None, n_eval_tasks={s5['n_eval_tasks']})`: the gap in "
        "pass^1 between two identical runs on the same tasks.",
        "",
    ]
    rows = []
    for k in (PREREG_REPEATS, 1):
        block = s5[f"trials_per_run_{k}"]
        rows.append(
            [
                k,
                fmt_ci(block["residual"], 4),
                fmt_ci(block["sd_diff"]),
                fmt_ci(block["expected_abs_diff"]),
                "+/-" + fmt_ci(block["band_95"]),
            ]
        )
    lines += [
        md_table(
            ["trials per run", "residual [CI]", "sd of D [CI]", "E|D| [CI]", "95% band [CI]"],
            rows,
        ),
        "",
    ]
    return lines


def _section12_lines(res: Mapping[str, Any]) -> list[str]:
    s12 = res["section12"]
    lines = [
        "## Section 12: `--cache-ram` sensitivity (descriptive)",
        "",
        f"Simulations started before vs after {s12['cutoff_ist']} IST (llama-server "
        "`--cache-ram 512` added). Task-cluster bootstrap 95% CIs; bootstrap draws in which "
        "a class is empty are left out (n undefined). Classes with fewer than "
        f"{MIN_TASKS_SENSITIVITY} tasks, or with p_bar outside [{P_BAR_LOW}, {P_BAR_HIGH}] "
        "(section 11), are flagged: their intervals are not interpretable. With fewer than "
        f"{MIN_TASKS_RELIABLE_CI} tasks per class the percentile intervals under-cover, so an "
        "interval that excludes 0 here is not by itself evidence of a --cache-ram effect.",
        "",
        "**Confound:** " + s12["confound_note"],
        "",
        "**Variants:** " + s12["variant_note"] + " H1c (exploratory section below) "
        "attributes the same residual and is also split by these period classes.",
        "",
        "**(a) Pair level (uses every simulation).** Every unordered pair of "
        "repeats in a cell is before_before, after_after or cross; residual = half the pair "
        "disagreement rate (unbiased for E[p(1-p)] within a period).",
        "",
    ]
    pl = s12["pair_level"]
    rows = [
        [
            cls,
            pl[cls]["n_pairs"],
            pl[cls]["n_disagreeing"],
            pl[cls]["n_tasks"],
            fmt_ci(pl[cls]["residual"], 4),
            pl[cls]["residual"]["n_boot_undefined"],
            "few tasks" if pl[cls]["few_tasks"] else "",
        ]
        for cls in PAIR_CLASSES
    ]
    diff = pl["after_after_minus_before_before"]
    rows.append(
        [
            "after_after - before_before",
            "",
            "",
            "",
            fmt_ci(diff, 4),
            diff["n_boot_undefined"],
            "",
        ]
    )
    lines += [
        md_table(
            ["pair class", "pairs", "disagreeing", "tasks", "residual [CI]", "n undefined", "flag"],
            rows,
        ),
        "",
        "**(b) Within mixed cells (matched on task and simulator).** Cells with repeats in "
        "both periods: cross-period pairs vs same-period pairs of the same cells. If the "
        "change shifted a cell's success probability, cross pairs disagree more. Still "
        "confounded with trial order and calendar time.",
        "",
    ]
    wm = s12["within_mixed_cells"]
    rows = [
        [
            name,
            wm[name]["n_pairs"],
            wm[name]["n_disagreeing"],
            wm[name]["n_tasks"],
            fmt_ci(wm[name]["residual"], 4),
            wm[name]["residual"]["n_boot_undefined"],
        ]
        for name in ("same_period", "cross")
    ]
    diff = wm["cross_minus_same_period"]
    rows.append(["cross - same_period", "", "", "", fmt_ci(diff, 4), diff["n_boot_undefined"]])
    lines += [
        md_table(
            ["pair type", "pairs", "disagreeing", "tasks", "residual [CI]", "n undefined"], rows
        ),
        "",
        "**(c) Cell level.** Cells whose 3 repeats all started in one period (else mixed); "
        "residual = MS_E; within-cell flip rate with R = 3.",
        "",
    ]
    cl = s12["cell_level"]
    rows = [
        [
            cls,
            cl[cls]["n_cells"],
            cl[cls]["n_tasks"],
            fmt_ci(cl[cls]["residual"], 4),
            fmt_ci(cl[cls]["cell_flip_rate"]),
            fmt(cl[cls]["p_bar"]["estimate"]),
            ", ".join(
                flag
                for flag, on in (
                    ("few tasks", cl[cls]["few_tasks"]),
                    ("p_bar floor/ceiling", cl[cls]["p_bar_outside"]),
                )
                if on
            ),
        ]
        for cls in CELL_CLASSES
    ]
    amb = cl["after_minus_before"]
    thin = cl[BEFORE]["few_tasks"] or cl[AFTER]["few_tasks"]
    thin = thin or cl[BEFORE]["p_bar_outside"] or cl[AFTER]["p_bar_outside"]
    rows.append(
        [
            "after - before",
            "",
            "",
            fmt_ci(amb["residual"], 4),
            fmt_ci(amb["cell_flip_rate"]),
            "",
            "not interpretable" if thin else "",
        ]
    )
    lines += [
        md_table(
            [
                "cell class",
                "cells",
                "tasks",
                "residual (MS_E) [CI]",
                "flip rate [CI]",
                "p_bar",
                "flag",
            ],
            rows,
        ),
        "",
        "Composition (who is in each class):",
        "",
    ]
    comp = s12["composition"]
    lines += [
        md_table(
            ["cell class", "simulator", "cells", "tasks"],
            [
                [c["class"], c["simulator"], c["n_cells"], task_list(c["tasks"])]
                for c in comp["cells"]
            ],
        ),
        "",
        md_table(
            ["pair class", "simulator", "pairs", "disagreeing", "residual (point)"],
            [
                [
                    p["class"],
                    p["simulator"],
                    p["n_pairs"],
                    p["n_disagreeing"],
                    fmt(p["residual"], 4),
                ]
                for p in comp["pairs"]
            ],
        ),
        "",
        "Simulations per period: "
        + "; ".join(
            f"{role} before {v['before']}, after {v['after']}, unknown {v['unknown']}"
            for role, v in comp["simulations"].items()
        )
        + ". Tasks with runs before the cutoff: "
        + "; ".join(
            f"{role}: {task_list(t)}" for role, t in s12["tasks_with_runs_before_cutoff"].items()
        )
        + ".",
        "",
    ]
    if s12["cells_with_unknown_start_time"]:
        lines += [f"{s12['cells_with_unknown_start_time']} cell(s) lack a start time.", ""]
    return lines


def _pooled_all(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((r for r in rows if r["subset"] == "all" and r["group"] == "pooled"), None)


def _share_ci(r: Mapping[str, Any], name: str) -> str:
    return f"{fmt(r[name])} [{fmt(r[f'{name}_ci_low'])}, {fmt(r[f'{name}_ci_high'])}]"


def _h1c_table(
    rows: Sequence[Mapping[str, Any]], keys: Sequence[str], key_values: Any
) -> list[str]:
    """H1c summary rows as a table, with n undefined, n_other and a few-tasks flag."""
    body = [
        [
            *key_values(r),
            r["n_tasks"],
            r["n_pairs"],
            r["n_identical"],
            r["n_diverging"],
            r["n_user"],
            r["n_assistant"],
            r["n_tool"],
            r["n_length"],
            r.get("n_other", 0),
            _share_ci(r, "share_user"),
            _share_ci(r, "share_agent"),
            r.get("n_boot_undefined", ""),
            "few tasks: not interpretable" if r["n_tasks"] < MIN_TASKS_SENSITIVITY else "",
        ]
        for r in rows
    ]
    headers = [
        *keys,
        "tasks",
        "pairs",
        "identical",
        "diverging",
        "user",
        "assistant",
        "tool",
        "length",
        "other",
        "share_user [CI]",
        "share_agent [CI]",
        "n undefined",
        "flag",
    ]
    return [md_table(headers, body), ""]


def _h1c_context_lines(ctx: Mapping[str, Any]) -> list[str]:
    if not ctx:
        return []
    lines = [
        "### H1c context: turn order and identical-input generations (descriptive)",
        "",
        "Per pair, every message up to and including the first divergence was generated on "
        "identical input in both repeats (all messages for identical pairs; those before the "
        "divergence for length pairs). Rate = first divergences / identical-input "
        "generations of that side. The scripted greeting and tool results are not "
        "generations. No interval: a description of exposure, not an estimand.",
        "",
    ]
    rows = []
    for group, g in ctx.items():
        for side in ("agent", "user"):
            s = g["generations"][side]
            rows.append(
                [
                    group,
                    side,
                    s["identical_input_generations"],
                    s["first_divergences"],
                    fmt(s["rate"]),
                ]
            )
    lines += [
        md_table(
            ["group", "side", "identical-input generations", "first divergences", "rate"], rows
        ),
        "",
    ]
    for group, g in ctx.items():
        dist = "; ".join(
            f"{label}: " + ", ".join(f"{i}x{n}" for i, n in idx.items())
            for label, idx in g["divergence_index"].items()
        )
        lines.append(
            f"- {group}: {g['n_diverging_before_any_agent_generation']} of {g['n_diverging']} "
            "diverging pairs first diverged at a user turn before any agent generation on "
            f"identical input. First-divergence index (message position x pairs): {dist or '-'}."
        )
    return [*lines, ""]


def _h1c_lines(res: Mapping[str, Any]) -> list[str]:
    h = res["h1c"]
    lines = ["## Exploratory: H1c first-divergence attribution", ""]
    if h.get("status") != "ok":
        lines += [
            f"**H1c section {h.get('status', 'unavailable').upper()}**: "
            f"{h.get('error') or h.get('reason')}. The rest of the report is unaffected; re-run "
            "this script once `whose_noise.divergence` is importable.",
            "",
        ]
        return lines
    lines += [
        f"`whose_noise.divergence` (RESEARCH_NOTES section 5) on the analysed tasks: "
        f"{h['n_pairs']} repeat pairs over {h['n_tasks']} tasks. share_user = pairs whose first "
        "differing message is a user (simulator) message / diverging pairs (the "
        "pre-registered H1c estimand). Task-cluster bootstrap 95% CIs; draws in which a row "
        "has no diverging pair define no share and are left out of its interval (n undefined). "
        f"Rows with fewer than {MIN_TASKS_SENSITIVITY} tasks are flagged as not interpretable. "
        "Exploratory, no prediction.",
        "",
        "**How to read the shares (caveats).** (1) Turn order: tau2 opens every conversation "
        f'with a scripted agent greeting ("{SCRIPTED_GREETING}", not a generation), then the '
        "user simulator generates first. A user divergence at its first turn means the agent "
        "never generated on identical input in that pair, so share_user mixes per-generation "
        "noise with the simulator's earlier chance to diverge. The context table below gives "
        "the identical-input generations per side and a per-generation divergence rate. "
        "(2) Seeds: tau2 derives one seed per trial and sends it to BOTH the agent and the "
        "user LLM, so two repeats send identical messages with different `seed` values. "
        "llama.cpp at temperature 0 is expected to ignore the seed (not verified for the "
        "pinned build) and Groq documents `seed` as best-effort, so a divergence names a "
        "component but cannot separate serving nondeterminism from a seed effect inside it.",
        "",
    ]
    lines += _h1c_table(h["summary"], ["subset", "group"], lambda r: [r["subset"], r["group"]])
    by_period = h.get("by_period_class") or []
    if by_period:
        lines += [
            "### H1c by period class of the two repeats (descriptive)",
            "",
            h["period_note"] + " This is the H1c side of the section 12 sensitivity analysis.",
            "",
        ]
        for role in ROLES:
            pt = h["period_tasks"][role]
            lines.append(
                f"- {role}: tasks with after_after pairs {task_list(pt['after_after'])}; "
                f"tasks with NO after_after pair (only before_before / cross): "
                f"{task_list(pt['without_after_after'])}."
            )
        lines.append("")
        order = {c: i for i, c in enumerate((*PAIR_CLASSES, UNKNOWN))}

        def sort_key(r: Mapping[str, Any]) -> tuple[str, int, int]:
            role_pos = ROLES.index(r["simulator"]) if r["simulator"] in ROLES else len(ROLES)
            return (r["subset"], role_pos, order.get(r["period_class"], len(order)))

        rows = sorted(by_period, key=sort_key)
        lines += _h1c_table(
            rows,
            ["subset", "simulator", "period class"],
            lambda r: [r["subset"], r["simulator"], r["period_class"]],
        )
        aa = h.get("after_after_only") or []
        lines += [
            "**Sensitivity: after_after pairs only** (both repeats under `--cache-ram 512`):",
            "",
        ]
        lines += (
            _h1c_table(aa, ["subset", "group"], lambda r: [r["subset"], r["group"]])
            if aa
            else ["No after_after pairs.", ""]
        )
    lines += _h1c_context_lines(h.get("context") or {})
    if h["tool_divergences"]:
        lines += [
            "**Tool-message divergences (environment nondeterminism, a bug to report):** "
            + "; ".join(h["tool_divergences"]),
            "",
        ]
    for w in h["warnings"]:
        lines.append(f"- warning: {w}")
    return [*lines, ""] if h["warnings"] else lines


def _fidelity_lines(res: Mapping[str, Any]) -> list[str]:
    f = res["fidelity"]
    lines = ["## Exploratory: simulator fidelity (deterministic transcript checks)", ""]
    if f.get("status") != "ok":
        return [*lines, f"Fidelity section failed: {f.get('error')}", ""]
    lines += [
        "`whose_noise.fidelity` (the `analyze --fidelity` logic) on the analysed tasks. "
        "Screens, not verdicts: flagged transcripts must be read. Rate CIs: task-cluster "
        "bootstrap of the pooled rate over valid simulations.",
        "",
    ]
    rows = []
    for role in ROLES:
        cis = f["analysed_tasks"][role]["task_bootstrap_ci_all_outcomes"]
        rows.append([role] + [fmt_ci(cis[flag]) for flag in fidelity.FLAG_COLUMNS])
    lines += [md_table(["simulator", *fidelity.FLAG_COLUMNS], rows), ""]
    for role in ROLES:
        lines += [
            f"### {role} (analysed tasks)",
            "",
            "```",
            f["analysed_tasks"][role]["text"],
            "```",
            "",
        ]
    rows = []
    for role in ROLES:
        for r in f["all_simulations"][role]["summary"]:
            if r["outcome"] == "all":
                rows.append(
                    [role, r["sims"]]
                    + [f"{r[flag]} ({fmt(r[f'{flag}_rate'], 2)})" for flag in fidelity.FLAG_COLUMNS]
                )
    scope = f.get("all_simulations_scope", "every valid simulation in each file")
    lines += [
        f"Wider scope: {scope}.",
        "",
        md_table(["simulator", "sims", *fidelity.FLAG_COLUMNS], rows),
        "",
    ]
    return lines


def render_report(res: Mapping[str, Any]) -> str:
    lines = _header(res)
    lines += _summary_lines(res)
    lines += _data_status_lines(res)
    lines += _step1_lines(res)
    if res["step3"].get("status") != "skipped":
        lines += _step2_lines(res)
        lines += _step3_lines(res)
        lines += _decision_lines(res)
        lines += _step5_lines(res)
        lines += _section12_lines(res)
    else:
        lines += [f"Steps 2, 3, 5 and section 12 skipped: {res['step3']['reason']}.", ""]
    lines += _h1c_lines(res)
    lines += _fidelity_lines(res)
    s8 = res["step8_failure_analysis"]
    lines += [
        "## Step 8: failure analysis (LLM judges; not run here)",
        "",
        f"{s8['reason']}. See `{s8['audit_dir']}`: {', '.join(s8['audit_files']) or '(empty)'}.",
        "",
        "## Protocol checks and warnings",
        "",
    ]
    issues = list(res["protocol_warnings"]) + list(res["warnings"])
    lines += [f"- {w}" for w in issues] if issues else ["- none"]
    lines += [
        "",
        "## Deviations and caveats (section 12 of the pre-registration)",
        "",
        "- RQ1 data collection began on 2026-10-11 against an unfrozen draft; the frozen "
        "document lists every RQ1-relevant change made since then.",
        "- `--cache-ram 512` was added at 2026-10-12 20:23 IST (sensitivity analysis above).",
        "- One qwen3.8 simulation (task 42, trial 0) stalled ~24 h and then completed; it is "
        "kept (duration is not an outcome).",
        "- Erratum to the deviations log (recorded in docs/RESULTS_LOG.md; the frozen text is "
        "not edited): the simulator `num_retries 8` / exponential-backoff change and the RQ1 "
        "run-name switch are dated 2026-10-12 there, but the data show that for qwen3.8-27b "
        "they happened on 2026-10-11 at about 16:51 IST (the unused run c813736 has no "
        "retries; cd30908, created with them, starts 16:52), before its first valid "
        "simulation; the gpt-oss-120b run ce7e638 was created with them and starts "
        "2026-10-12 20:13 IST.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def write_outputs(res: Mapping[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "results.json"
    md_path = out_dir / "report.md"
    json_path.write_text(
        json.dumps(res, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_report(res), encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_rq1.py",
        description="Pre-registered RQ1 analysis (PREREGISTRATION.md, tag prereg-v1).",
    )
    parser.add_argument("--gptoss", required=True, help="results.json of the gpt-oss-120b run")
    parser.add_argument("--qwen", required=True, help="results.json of the qwen3.8-27b run")
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "results" / "rq1_analysis"),
        help="output directory for report.md and results.json",
    )
    parser.add_argument(
        "--n-boot", type=int, default=PREREG_N_BOOT, help="bootstrap replicates (prereg 10000)"
    )
    parser.add_argument(
        "--seed", type=int, default=PREREG_SEED, help="bootstrap seed (prereg 20261011)"
    )
    parser.add_argument(
        "--analysis-timestamp",
        help="timestamp to record in the outputs (default: now, IST); fix it for byte-identical "
        "re-runs",
    )
    parser.add_argument(
        "--cache-ram-cutoff",
        default=CACHE_RAM_CUTOFF,
        help="section 12 cutoff as an ISO time (naive = IST)",
    )
    parser.add_argument(
        "--planned-tasks",
        nargs="+",
        default=list(PREREG_TASKS),
        help="override the 29 pre-registered task ids (testing only; makes the run provisional)",
    )
    parser.add_argument(
        "--retries-exhausted",
        nargs="+",
        default=[],
        metavar="TASK",
        help="tasks whose infra-error cells have had their 3 extra --auto-resume attempts "
        "(section 8); only these are dropped. Without it, a cell that still holds an infra "
        "error keeps the data partial",
    )
    parser.add_argument(
        "--rerun-evidence",
        metavar="JSON",
        help='re-run facts counted from the run logs: {"source": "...", '
        '"pre_rerun_infra_errors": {"gpt-oss-120b": n, ...}, '
        '"extra_attempts": {"qwen3.8-27b": {"33": n}, ...}}',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n_boot < 1:
        build_parser().error("--n-boot must be >= 1")
    try:
        res = run_analysis(
            args.gptoss,
            args.qwen,
            n_boot=args.n_boot,
            seed=args.seed,
            planned=args.planned_tasks,
            cutoff=args.cache_ram_cutoff,
            analysis_timestamp=args.analysis_timestamp,
            retries_exhausted=args.retries_exhausted,
            rerun_evidence=args.rerun_evidence,
        )
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    json_path, md_path = write_outputs(res, Path(args.out_dir))
    status = res["data_status"]
    print(
        f"RQ1 analysis ({status['mode']} data, {status['n_analysed']}/{status['n_planned']} "
        f"tasks analysed, {'confirmatory' if res['confirmatory'] else 'provisional'}) "
        f"written to:\n  {md_path}\n  {json_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
