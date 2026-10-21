"""Agent determinism audit for llama-server (RESEARCH_NOTES section 5, complementary check).

Rebuilds real agent requests from recorded tau2 conversations (retail policy as system prompt, the
retail tool schemas, a conversation prefix) and sends each one repeatedly to the local llama-server:
  A) N identical requests back to back,
  B) N identical requests, each preceded by a *different* request (prompt-cache disturbance).
Reports how many distinct outputs (content + tool calls) each request produced.
Run with the tau2-bench venv while llama-server is up and NO experiment is running (it would
disturb the batching being measured), from the tau2-bench checkout:
  PYTHONUTF8=1 .venv/Scripts/python ../whose-noise/scripts/determinism_audit.py \
      <copy of results.json>
"""

import json
import sys
import urllib.request

from tau2.domains.retail.environment import get_environment

URL = "http://127.0.0.1:8080/v1/chat/completions"
N = 10


def to_openai(messages):
    out = []
    for m in messages:
        role = m["role"]
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("id") or m.get("tool_call_id") or "x",
                    "content": m.get("content") or "",
                }
            )
            continue
        msg = {"role": role, "content": m.get("content") or ""}
        if m.get("tool_calls"):
            msg["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                for i, tc in enumerate(m["tool_calls"])
            ]
        out.append(msg)
    return out


def call(body):
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    r = json.load(urllib.request.urlopen(req, timeout=300))
    m = r["choices"][0]["message"]
    return json.dumps(
        {
            "content": m.get("content"),
            "tool_calls": [
                (t["function"]["name"], t["function"]["arguments"])
                for t in (m.get("tool_calls") or [])
            ],
        },
        sort_keys=True,
    )


def main(path):
    d = json.load(open(path, encoding="utf-8"))
    env = get_environment()
    tools = [t.openai_schema for t in env.get_tools()]
    sims = [s for s in d["simulations"] if s["termination_reason"] != "infrastructure_error"]
    # three probe requests: early, middle and late cut points of three different conversations
    probes = []
    for s, frac in zip(sims[:3], (0.3, 0.5, 0.8), strict=False):
        msgs = s["messages"]
        # cut just before an assistant message so the next turn is the agent's
        idx = [i for i, m in enumerate(msgs) if m["role"] == "assistant" and i > 0]
        cut = idx[min(len(idx) - 1, int(len(idx) * frac))]
        body = {
            "model": "qwen3-4b",
            "temperature": 0.0,
            "tools": tools,
            "messages": [{"role": "system", "content": s["policy"]}] + to_openai(msgs[:cut]),
        }
        probes.append((f"task {s['task_id']} trial {s['trial']} cut {cut}/{len(msgs)}", body))
    for name, body in probes:
        a = {call(body) for _ in range(N)}
        b = set()
        others = [p for n, p in probes if n != name]
        for i in range(N):
            call(others[i % len(others)])
            b.add(call(body))
        print(
            f"{name}: A back-to-back distinct outputs = {len(a)}/{N}; "
            f"B interleaved distinct = {len(b)}/{N}; A==B same set: {a == b}"
        )
        for k, o in enumerate(sorted(a | b)):
            print(f"   variant {k}: {o[:420]}")


if __name__ == "__main__":
    main(sys.argv[1])
