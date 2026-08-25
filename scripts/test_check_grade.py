"""Self-check for the grade gate: `python scripts/test_check_grade.py`.

No framework on purpose — this repo has no test runner and the action itself
runs on a bare runner. The gate decides whether a student's work is reported as
passing, so the cases below are the contract, not a smoke test.
"""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from check_grade import verdict  # noqa: E402

CASES = [
    ("every test passed", {"score": 6, "max_score": 6}, 0),
    ("partial credit is not a pass", {"score": 5, "max_score": 6}, 1),
    ("nothing passed", {"score": 0, "max_score": 6}, 1),
    ("collection error", {"score": 0, "max_score": 0}, 1),
    # int() would truncate both of these to 1 and call it a pass.
    ("fractional shortfall", {"score": 1.1, "max_score": 1.9}, 1),
    ("fractional exact", {"score": 2.5, "max_score": 2.5}, 0),
    ("bool is not a score", {"score": True, "max_score": 1}, 1),
    ("negative denominator", {"score": 0, "max_score": -3}, 1),
    ("over-scored still passes", {"score": 7, "max_score": 6}, 0),
    ("non-numeric", {"score": "x", "max_score": 6}, 1),
    ("numeric strings", {"score": "6", "max_score": "6"}, 0),
]


def main():
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, (label, payload, expected) in enumerate(CASES):
            path = pathlib.Path(tmp) / f"case{index}.json"
            path.write_text(json.dumps(payload))
            code, message = verdict(str(path))
            if code != expected:
                failures.append(f"{label}: got {code}, wanted {expected} ({message})")

        missing = str(pathlib.Path(tmp) / "absent.json")
        if verdict(missing)[0] != 1:
            failures.append("a missing results.json must not pass")

        corrupt = pathlib.Path(tmp) / "corrupt.json"
        corrupt.write_text("{not json")
        if verdict(str(corrupt))[0] != 1:
            failures.append("an unreadable results.json must not pass")

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    print(f"{len(CASES) + 2 - len(failures)}/{len(CASES) + 2} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
