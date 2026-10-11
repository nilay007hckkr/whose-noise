"""Shared fixtures: the trimmed real tau2 run and a synthetic run with known answers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINI_RESULTS = FIXTURES / "mini_results.json"


def _msg(
    role: str,
    prompt: int | None = None,
    completion: int | None = None,
    tool_calls: list[str] | None = None,
) -> dict[str, Any]:
    usage = None if prompt is None else {"prompt_tokens": prompt, "completion_tokens": completion}
    calls = None if not tool_calls else [{"name": n, "arguments": {}} for n in tool_calls]
    return {"role": role, "content": "x", "tool_calls": calls, "usage": usage, "turn_idx": 0}


def _sim(
    task_id: str,
    trial: int,
    reward: float | None,
    *,
    write_ok: bool = True,
    termination: str = "user_stop",
    extra_call: str | None = None,
) -> dict[str, Any]:
    """A simulation with 2 agent turns, 2 user turns, 1 tool call and fixed token counts.

    Agent: 100+10 and 200+20 tokens -> prompt 300, completion 30.
    User:  50+5 and 60+6 tokens     -> prompt 110, completion 11.
    ``extra_call`` adds a second (ungolden) tool call to the second agent turn.
    Infra errors (reward None) carry an evaluator-stage AuthenticationError.
    """
    if reward is None:
        return {
            "id": f"{task_id}-{trial}",
            "task_id": task_id,
            "trial": trial,
            "seed": 7,
            "duration": 0.0,
            "termination_reason": "infrastructure_error",
            "reward_info": None,
            "messages": [],
            "info": {
                "error_type": "AuthenticationError",
                "error_traceback": r'File "D:\t\src\tau2\evaluator\evaluator.py", line 1',
            },
        }
    checks = [
        {
            "action": {"name": "get_order_details", "arguments": {}},
            "action_match": True,
            "tool_type": "read",
        },
        {
            "action": {"name": "cancel_pending_order", "arguments": {}},
            "action_match": write_ok,
            "tool_type": "write",
        },
    ]
    messages = [
        _msg("assistant", 100, 10),
        _msg("user", 50, 5),
        _msg(
            "assistant", 200, 20, tool_calls=["get_order_details"] + [extra_call] * bool(extra_call)
        ),
        _msg("tool"),
        _msg("user", 60, 6),
    ]
    return {
        "id": f"{task_id}-{trial}",
        "task_id": task_id,
        "trial": trial,
        "seed": 7,
        "duration": 10.0 * (trial + 1),
        "termination_reason": termination,
        "reward_info": {
            "reward": reward,
            "db_check": {"db_match": reward == 1.0},
            "action_checks": checks,
        },
        "messages": messages,
    }


@pytest.fixture
def synthetic_results(tmp_path: Path) -> Path:
    """3 tasks x 2 trials + 1 infra error, written deliberately out of order.

    Ground truth (valid trials, by trial index):
      task "2":  [True,  True ]
      task "10": [False, True ]   -> flips
      task "x":  [False, False]   + trial 2 is an infrastructure error
    pass^1 = (1 + 0.5 + 0) / 3 = 0.5 ; pass^2 = (1 + 0 + 0) / 3 = 1/3 ; flip rate = 1/3.
    """
    sims = [
        _sim("10", 1, 1.0),
        _sim("10", 0, 0.0, write_ok=False),
        _sim("2", 0, 1.0),
        _sim("2", 1, 1.0),
        _sim("x", 0, 0.0, termination="max_steps"),
        _sim("x", 1, 0.0, extra_call="modify_user_address"),
        _sim("x", 2, None),
    ]
    data = {
        "timestamp": "t",
        "info": {
            "num_trials": 2,
            "user_info": {"llm": "user-llm"},
            "agent_info": {"llm": "agent-llm"},
            "environment_info": {"domain_name": "retail"},
        },
        "tasks": [],
        "simulations": sims,
    }
    run_dir = tmp_path / "synth_run"
    run_dir.mkdir()
    path = run_dir / "results.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def mini_results() -> Path:
    return MINI_RESULTS
