"""Read JUnit XML test reports for the cin90 results payload.

JUnit XML is the one report format every mainstream runner outside Python can
write — Maven Surefire, Gradle, jest-junit, gotestsum, CTest — so it is how
the reporter grades a language it knows nothing about.

The document is produced by the student's own test run, so it is treated as
hostile input rather than as our own output:

* a DOCTYPE or entity declaration is refused unparsed. Expat expands internal
  entities while parsing, so a billion-laughs document is already gigabytes of
  memory by the time any post-parse check could look at it. `defusedxml` would
  do this for us and is not installable — the action runs bare `python` on a
  GitHub-hosted runner with no pip step — so the refusal is ours to make.
* the bytes per file and the number of files are capped.
* anything unreadable raises `JunitReportError`, which the caller turns into a
  reported error run. Skipping a file we could not read would lower the
  denominator, and a grade that is quietly out of 4 instead of 6 is worse than
  one that visibly did not happen.
"""

import glob
import os
import pathlib
import re
import xml.etree.ElementTree as ET

# 5 MiB is already two orders of magnitude past a real suite's report, and
# 500 files past a real repo's test classes. Both bound how much of a
# student-controlled tree this script will hold in memory at once.
MAX_BYTES_PER_FILE = 5 * 1024 * 1024
MAX_FILES = 500

# A <failure> body is conventionally the whole stack trace, where pytest's
# crash message is one line, so a class of failing tests is megabytes of
# payload without this. Per message rather than in total: every failure keeps
# its location and its first screenful, which is what a student reads.
MAX_MESSAGE_CHARS = 2000

_DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

# Order matters: a testcase carrying both a failure and a skipped child is a
# failure, not a skip.
_OUTCOME_TAGS = ("failure", "error", "skipped")

_GLOB_CHARS = "*?["

# `(Name.ext:LINE)` — the one shape a stack frame has in Java, Kotlin, Scala,
# JavaScript and TypeScript alike. The optional trailing group is Node's
# column. Deliberately one pattern and no per-language handling: a runner this
# does not fit degrades to the un-anchored failure it already produced.
_STACK_FRAME = re.compile(r"\(([^()\s:]+\.[A-Za-z0-9_]+):(\d{1,7})(?::\d{1,7})?\)")

# Only the head of a trace is scanned: the frame in the student's own code is
# near the top, under the assertion library's frames, and the tail is the
# runner's own reflection machinery.
MAX_TRACE_SCAN_CHARS = 20000

# Skipped wholesale when indexing the checkout. `target` and `build` are where
# a build puts its copies of the sources, and a copy is what turns a unique
# filename into an ambiguous one.
SKIP_DIRS = {".git", "node_modules", "target", "build"}

# The walk is enrichment inside a grading step, so it is bounded rather than
# thorough.
MAX_WORKSPACE_FILES = 20000


class JunitReportError(Exception):
    """A report we will not grade from: unreadable, malformed, or hostile."""


class _Workspace:
    """The checkout, indexed by filename, so a stack frame can be resolved.

    Built at most once per run and only when some failure actually needs it,
    so a passing suite never walks the tree.
    """

    def __init__(self, root):
        self._root = pathlib.Path(root)
        self._index = None

    def matches(self, filename):
        """Every repo-relative path carrying that filename."""
        if self._index is None:
            self._index = self._walk()
        return self._index.get(filename, ())

    def _walk(self):
        """filename -> paths, or nothing at all if the walk was cut short.

        A partial index is worse than none: a filename that looks unique only
        because the rest of the checkout went unread is exactly the wrong-path
        anchor this must never produce.
        """
        index = {}
        visited = 0
        try:
            for directory, subdirectories, filenames in os.walk(self._root):
                subdirectories[:] = [
                    name for name in subdirectories if name not in SKIP_DIRS
                ]
                for name in filenames:
                    visited += 1
                    if visited > MAX_WORKSPACE_FILES:
                        return {}
                    relative = os.path.relpath(
                        os.path.join(directory, name), self._root
                    )
                    index.setdefault(name, []).append(
                        pathlib.PurePath(relative).as_posix()
                    )
        except OSError:
            return {}
        return index


def locate(outcome, workspace):
    """`(path, line)` from the first stack frame naming a file in the checkout.

    Surefire and most JVM and Node runners write no `file` or `line` attribute
    at all, and cin90 keeps only failures carrying both — so without this every
    Java failure drops out of the inline review and the class silently gets
    summary comments instead.

    Frames are read in order and one naming no file here is skipped, not given
    up on: a JUnit assertion failure opens with the assertion library's own
    frames, which are not in the student's repo. A filename carried by two
    files stops the search — a review comment anchored on the wrong file is
    worse than one that was never anchored.

    Enrichment only. Any failure costs the anchor, never the payload.
    """
    try:
        text = (outcome.get("message") or "") + "\n" + (outcome.text or "")
        for frame in _STACK_FRAME.finditer(text[:MAX_TRACE_SCAN_CHARS]):
            found = workspace.matches(pathlib.PurePosixPath(frame.group(1)).name)
            if len(found) == 1:
                return found[0], int(frame.group(2))
            if found:
                return None
    except Exception:
        return None
    return None


def report_files(results_path):
    """`(files, notes)` for a path that may be a file, a directory or a glob.

    Surefire writes one `TEST-*.xml` per test class and Gradle one per class
    too, so treating the path as a single file would grade one slice of a
    suite and record it as the whole score.

    A path that exists is taken literally even when it looks like a glob: a
    directory legitimately named `[core]` is a real repo, and expanding it as
    a pattern would grade nothing and blame the student for it.
    """
    path = pathlib.Path(str(results_path))
    if path.is_dir():
        found = list(path.rglob("*.xml"))
    elif path.is_file():
        found = [path]
    elif any(char in str(results_path) for char in _GLOB_CHARS):
        found = []
        for match in map(pathlib.Path, glob.glob(str(results_path), recursive=True)):
            # A pattern can name the report directories rather than the
            # reports: `build/*/test-results` is one module per match.
            found.extend(match.rglob("*.xml") if match.is_dir() else [match])
    else:
        found = []

    files = sorted(candidate for candidate in found if candidate.is_file())
    notes = []
    if len(files) > MAX_FILES:
        # Deliberately not an "Autograder error:" line: that marker makes the
        # server discard the run as an infrastructure error, and this run has
        # a real, if partial, score. Say what was left out instead.
        notes.append(
            f"Note: only the first {MAX_FILES} of {len(files)} JUnit XML report "
            "files were read; the score below covers those files only."
        )
        files = files[:MAX_FILES]
    return files, notes


def _read(path):
    """The file's bytes, refusing one past the cap.

    Reading the cap and parsing the fragment is not an option: a truncated
    document either fails to parse or, worse, parses into a smaller suite and
    silently lowers the score.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES_PER_FILE + 1)
    except OSError as exc:
        raise JunitReportError(f"{path} could not be read ({exc})") from exc
    if len(raw) > MAX_BYTES_PER_FILE:
        raise JunitReportError(
            f"{path} is larger than the {MAX_BYTES_PER_FILE} byte limit"
        )
    return raw


def _root(path):
    raw = _read(path)
    declaration = _DOCTYPE.search(raw)
    if declaration:
        kind = declaration.group(1).decode("ascii").upper()
        raise JunitReportError(f"{path} declares a {kind}, which is refused")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise JunitReportError(f"{path} is not valid XML ({exc})") from exc


def _outcome(case):
    """The child marking this testcase as not passed, or None."""
    for tag in _OUTCOME_TAGS:
        child = case.find(tag)
        if child is not None:
            return child
    return None


def _line(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _message(outcome):
    text = (
        outcome.get("message")
        or (outcome.text or "").strip()
        or outcome.tag
    )
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[:MAX_MESSAGE_CHARS] + "\n(truncated)"


def parse(results_path, workspace=None):
    """`(total, passed, failures, notes, file_count)` over every report found.

    Mirrors the pytest reader: every `<testcase>` counts toward `max_score`,
    and only one with no failure, error or skipped child scores. A skipped
    test is part of the assignment the student did not do, so it is in the
    denominator and not the numerator.

    `file_count` is what distinguishes "no reports were written" from "the
    reports contain no tests" — both score 0/0, but only the caller can say
    which happened.

    `workspace` is the checkout a stack frame is resolved against.
    """
    files, notes = report_files(results_path)
    checkout = _Workspace(workspace or ".")
    total = 0
    passed = 0
    failures = []
    for path in files:
        for case in _root(path).iter("testcase"):
            total += 1
            outcome = _outcome(case)
            if outcome is None:
                passed += 1
                continue
            if outcome.tag == "skipped":
                continue
            # A runner that supplies `file` is stating where the test is; the
            # stack frame is only ever our reading of its prose.
            location = case.get("file")
            line = _line(case.get("line"))
            if not location:
                located = locate(outcome, checkout)
                if located:
                    location, line = located
            failures.append({
                "path": location or case.get("classname") or "",
                "line": line,
                "message": _message(outcome),
            })
    return total, passed, failures, notes, len(files)
