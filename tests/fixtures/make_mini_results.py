"""Regenerate ``mini_results.json`` by trimming a real tau2 results file.

Usage (from the repo root)::

    uv run python tests/fixtures/make_mini_results.py \
        D:/Programming/Reaserch/tau2-bench/data/simulations/week2_local_qwen4b/results.json

The schema is kept intact; only bulky or sensitive values are changed:

* ``policy`` texts are set to None;
* per-message ``raw_data`` (provider response dumps) is set to None;
* every string longer than ``MAX_STR`` characters is truncated;
* every ``api_key`` value anywhere in the tree becomes ``"REDACTED"``;
* provider organisation ids (``org_...``, e.g. inside a Groq ``RateLimitError``
  message stored under ``info.error``) become ``org_REDACTED`` in every string, BEFORE
  truncation, so a partial id can never survive;
* ``error_traceback`` keeps only its tau2 source frames (``src/tau2/...``), which is
  what the loader uses to tell evaluation-stage from simulation-stage errors;
* only the first ``N_SIMS`` simulations are kept.

Token ``usage`` blocks, ``action_checks``, rewards and termination reasons are left
untouched, so loader tests on the fixture check real values.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_STR = 160
N_SIMS = 3
OUT = Path(__file__).with_name("mini_results.json")
ORG_ID_RE = re.compile(r"org_[A-Za-z0-9]+")


def redact(text: str) -> str:
    """Replace provider organisation ids (``org_<alnum>``) with ``org_REDACTED``."""
    return ORG_ID_RE.sub("org_REDACTED", text)


def tau2_frames(traceback: str) -> str:
    """Keep only the traceback lines that point into tau2's own source tree."""
    lines = [ln.strip() for ln in traceback.splitlines()]
    return "\n".join(ln for ln in lines if "src/tau2/" in ln.replace("\\", "/"))


def trim(node: Any) -> Any:
    """Recursively drop bulky fields, redact keys and org ids, truncate long strings."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "api_key":
                out[key] = "REDACTED"
            elif key in {"policy", "raw_data"}:
                out[key] = None
            elif key == "error_traceback" and isinstance(value, str):
                out[key] = redact(tau2_frames(value))
            else:
                out[key] = trim(value)
        return out
    if isinstance(node, list):
        return [trim(v) for v in node]
    if isinstance(node, str):
        node = redact(node)
        return node[:MAX_STR] + "...[trimmed]" if len(node) > MAX_STR else node
    return node


def main(src: str) -> None:
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    data["simulations"] = data["simulations"][:N_SIMS]
    kept = {s["task_id"] for s in data["simulations"]}
    data["tasks"] = [t for t in data.get("tasks", []) if t.get("id") in kept]
    OUT.write_text(json.dumps(trim(data), indent=1, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1])
