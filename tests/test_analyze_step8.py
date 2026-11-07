import importlib.util
import math
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "analyze_step8", Path(__file__).resolve().parents[1] / "scripts/analyze_step8.py"
)
step8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(step8)


def test_cohen_kappa_by_hand():
    # 10 pairs, 7 agree. Rater x: A x6, S x4; rater y: A x5, S x5.
    # po = 0.7; pe = 0.6*0.5 + 0.4*0.5 = 0.5; kappa = (0.7 - 0.5) / 0.5 = 0.4
    x = ["A"] * 6 + ["S"] * 4
    y = ["A"] * 4 + ["S"] * 2 + ["A"] + ["S"] * 3
    assert math.isclose(step8.cohen_kappa(x, y), 0.4)
    assert step8.cohen_kappa(x, x) == 1.0
    assert math.isnan(step8.cohen_kappa(["A", "A"], ["A", "A"]))


def test_report_withholds_labels_until_human_done():
    cases = [{"case_id": f"s{i}", "simulator": "sim1" if i < 2 else "sim2"} for i in range(4)]

    def j(c, a, b, f):
        return {
            "id": c,
            "final": f,
            "agreed": a == b,
            "a": {"primary_cause": a},
            "b": {"primary_cause": b},
            "t": None,
        }

    judged = [
        j("s0", "AGENT_ERROR", "AGENT_ERROR", "AGENT_ERROR"),
        j("s1", "AGENT_ERROR", "SHARED", "SHARED"),
        j("s2", "SIMULATOR_ERROR", "SIMULATOR_ERROR", "SIMULATOR_ERROR"),
        j("s3", "SHARED", "SHARED", "SHARED"),
    ]
    text = step8.report(judged, cases, None)
    assert "3/4 = 0.75" in text and "tiebreaks: 1" in text
    assert "SIMULATOR_ERROR" not in text  # no label names or counts before the human labels
