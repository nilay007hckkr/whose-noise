"""Regenerate the trimmed real-transcript fixtures used by ``tests/test_fidelity.py``.

Usage (from the repo root; the sources are only read, never written)::

    uv run python tests/fixtures/make_fidelity_fixtures.py \
        --gptoss  ../tau2-bench/data/simulations/rq1_qwen4b_gptoss120b_test-nonl_t3_s300_ce7e638 \
        --qwen38  ../tau2-bench/data/simulations/rq1_qwen4b_qwen3.8-27b_test-nonl_t3_s300_cd30908 \
        --hindi   ../tau2-bench-multilingual/data/simulations/smoke_hi_romanized_qwen38 \
        --pilot   ../tau2-bench/data/simulations/pilot1_qwen4b_gptoss120b

A results file that a running job is appending to can be passed as a copy.

Only the simulations in ``SELECTION`` are kept. The fidelity detectors read the text of
customer and agent messages, so that text is kept **in full** (unlike
``make_mini_results.py``); everything else is cut down:

* ``info`` keeps the model names, llm_args (``api_key`` -> ``"REDACTED"``), seed, domain
  and the tau-multilingual provenance keys (``text_input_style`` etc.); prompts,
  policies and tool definitions are dropped;
* tool-result message content is cut to ``TOOL_CHARS`` characters; ``raw_data`` and
  other per-message provider dumps are dropped;
* ``reward_info`` keeps reward, db_check and action_checks;
* the output is scanned for provider-key prefixes (``gsk_``, ``sk-``, ``AIza``, ...) and
  organisation ids; the script refuses to write if any survive.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).with_name("fidelity")
TOOL_CHARS = 200
SELECTION: dict[str, tuple[str, list[tuple[str, int]]]] = {
    # output run directory: (source argument, [(task_id, trial), ...])
    "fid_rq1_gptoss120b": (
        "gptoss",
        [
            ("18", 0),  # audit: "Yes.###STOP###" after "Please confirm with a yes"
            ("9", 1),  # audit: "yes\n###STOP###"
            ("32", 0),  # audit: "Yes, please cancel ... ###STOP###"
            ("18", 1),  # pass: "That works for me. ###STOP###" after a sign-off
            ("17", 2),  # pass: bare ###STOP###
            ("12", 1),  # pass: asks for a human, agent transfers, ###TRANSFER###
            ("12", 2),  # pass: "I will proceed with the return" (announcement, not a request)
            ("17", 1),  # fail: "I can't proceed without verifying" (refusal, not a request)
            ("27", 0),  # fail (agent error): bare ###STOP### after a sign-off
        ],
    ),
    "fid_rq1_qwen38": (
        "qwen38",
        [
            ("27", 2),  # pass: "I'll proceed with the exchange" then bare ###STOP###
            ("32", 0),  # fail: "Can you transfer me to a human agent?"
            ("9", 0),  # pass: thanks + ###STOP###
        ],
    ),
    "fid_hi_smoke": ("hindi", [("0_hi", 0)]),  # romanized style, Devanagari leakage
    # Regression cases for false positives fixed after the fidelity review; separate
    # directories so that the counts asserted on the fixtures above stay as they are.
    "fid_rq1_gptoss120b_post_write": (
        "gptoss",
        [("55", 0)],  # fail: list bullet "- Confirm it's received by us" after all writes
    ),
    "fid_pilot1_gptoss120b": (
        "pilot",
        [("1", 2)],  # pass: link offer after the exchange, "Yes, please send ...###STOP###"
    ),
}
SOURCES = ("gptoss", "qwen38", "hindi", "pilot")
INFO_KEYS = (
    "git_commit",
    "num_trials",
    "seed",
    "task_set_name",
    "user_persona_id",
    "text_input_style",
    "communicate_judge_mode",
)
MESSAGE_KEYS = ("role", "content", "tool_calls", "turn_idx", "usage")
SIM_KEYS = ("id", "task_id", "trial", "seed", "duration", "termination_reason")
# Anchored at a token start so that e.g. "task_set_name" does not match "sk_".
KEY_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:gsk_|sk-(?!no-key)|sk_|AIza|hf_|xai-|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}"
)
ORG_ID_RE = re.compile(r"org_[A-Za-z0-9]+")


def _agent_or_user(block: dict[str, Any] | None) -> dict[str, Any]:
    block = block or {}
    args = dict(block.get("llm_args") or {})
    if "api_key" in args:
        args["api_key"] = "REDACTED"
    return {k: block.get(k) for k in ("implementation", "llm")} | {"llm_args": args}


def trim_info(info: dict[str, Any]) -> dict[str, Any]:
    out = {k: info[k] for k in INFO_KEYS if k in info}
    out["user_info"] = _agent_or_user(info.get("user_info"))
    out["agent_info"] = _agent_or_user(info.get("agent_info"))
    out["environment_info"] = {
        "domain_name": (info.get("environment_info") or {}).get("domain_name")
    }
    return out


def trim_message(msg: dict[str, Any]) -> dict[str, Any]:
    out = {k: msg.get(k) for k in MESSAGE_KEYS}
    if out["role"] == "tool" and isinstance(out["content"], str):
        out["content"] = out["content"][:TOOL_CHARS]
    if out["tool_calls"]:
        out["tool_calls"] = [
            {k: c.get(k) for k in ("id", "name", "arguments", "requestor")}
            for c in out["tool_calls"]
        ]
    return out


def trim_sim(sim: dict[str, Any]) -> dict[str, Any]:
    out = {k: sim.get(k) for k in SIM_KEYS}
    ri = sim.get("reward_info")
    out["reward_info"] = (
        None if ri is None else {k: ri.get(k) for k in ("reward", "db_check", "action_checks")}
    )
    out["messages"] = [trim_message(m) for m in sim.get("messages") or []]
    return out


def build(src: Path, wanted: list[tuple[str, int]]) -> dict[str, Any]:
    path = src / "results.json" if src.is_dir() else src
    data = json.loads(path.read_text(encoding="utf-8"))
    by_key = {(str(s["task_id"]), int(s["trial"])): s for s in data["simulations"]}
    missing = [k for k in wanted if k not in by_key]
    if missing:
        raise SystemExit(f"{path}: simulations not found: {missing}")
    return {
        "timestamp": data.get("timestamp"),
        "info": trim_info(data.get("info") or {}),
        "tasks": [],
        "simulations": [trim_sim(by_key[k]) for k in wanted],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for key in SOURCES:
        parser.add_argument(f"--{key}", type=Path, required=True)
    args = parser.parse_args()
    for run_dir, (key, wanted) in SELECTION.items():
        text = json.dumps(build(getattr(args, key), wanted), indent=1, ensure_ascii=False)
        text = ORG_ID_RE.sub("org_REDACTED", text)
        leaked = KEY_PREFIX_RE.findall(text)
        if leaked:
            raise SystemExit(f"refusing to write {run_dir}: secret-like values {leaked}")
        out = OUT_DIR / run_dir / "results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
