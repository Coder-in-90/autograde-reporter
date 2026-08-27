"""Self-check for the reported sha: `python scripts/test_report_autograde.py`.

No framework, same as test_check_grade.py — the action runs on a bare runner.
Which sha a run reports decides whether the server sees one grade or two for
one commit, so these cases are the contract.
"""

import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from report_autograde import graded_sha  # noqa: E402

HEAD = "a" * 40
MERGE = "b" * 40

CASES = [
    # A pull_request run: Actions sets GITHUB_SHA to the throwaway merge
    # commit, and the action derives the pushed head into CIN90_SHA. Reporting
    # the merge sha here is what graded one commit twice (#397).
    ("pull_request run", {"CIN90_SHA": HEAD, "GITHUB_SHA": MERGE}, HEAD),
    ("push run", {"CIN90_SHA": HEAD, "GITHUB_SHA": HEAD}, HEAD),
    # A workflow calling this script directly, without the action.
    ("no CIN90_SHA", {"GITHUB_SHA": HEAD}, HEAD),
    ("blank CIN90_SHA", {"CIN90_SHA": "", "GITHUB_SHA": HEAD}, HEAD),
    ("whitespace CIN90_SHA", {"CIN90_SHA": "  ", "GITHUB_SHA": HEAD}, HEAD),
    ("padded CIN90_SHA", {"CIN90_SHA": f" {HEAD} ", "GITHUB_SHA": MERGE}, HEAD),
]


def check_action_yml():
    """The script half is inert unless action.yml still derives the head.

    Without this, deleting the CIN90_SHA line leaves every case above green
    while pull_request runs go back to reporting the merge commit.
    """
    text = (pathlib.Path(__file__).parents[1] / "action.yml").read_text()
    wanted = "CIN90_SHA: ${{ github.event.pull_request.head.sha || github.sha }}"
    if wanted not in text:
        return [f"action.yml does not set {wanted!r}"]
    # It has to be on the step that runs the reporter, not merely present.
    report_step = text.split("- name: Report to Coder in 90", 1)[-1]
    report_step = re.split(r"\n    - name: ", report_step, maxsplit=1)[0]
    if wanted not in report_step:
        return ["action.yml sets CIN90_SHA outside the report step"]
    return []


def main():
    failures = check_action_yml()
    for label, environ, expected in CASES:
        saved = {k: os.environ.get(k) for k in ("CIN90_SHA", "GITHUB_SHA")}
        try:
            for key in saved:
                os.environ.pop(key, None)
            os.environ.update(environ)
            got = graded_sha()
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value
        if got != expected:
            failures.append(f"{label}: expected {expected!r}, got {got!r}")

    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    print(f"{len(CASES) - len(failures)}/{len(CASES)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
