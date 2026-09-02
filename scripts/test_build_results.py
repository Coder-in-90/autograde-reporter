"""Self-check for the results builder: `python scripts/test_build_results.py`.

No framework, same as the other two self-checks — the action runs bare
`python` on a GitHub-hosted runner. What is pinned here is the contract with
the cin90 server: the payload shape, the "Autograder error:" markers it keys
needs-attention state off, and the guarantee that results.json exists when the
script ends. The JUnit reader is graded student-controlled input, so its
hostile cases are pinned beside the happy ones.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from build_results import build_results  # noqa: E402

SCRIPT = str(pathlib.Path(__file__).parent / "build_results.py")
MARKER = "Autograder error:"

FAILURES = []
CHECKS = 0


def expect(label, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, wanted {want!r}")


def expect_in(label, needle, haystack):
    global CHECKS
    CHECKS += 1
    if needle not in haystack:
        FAILURES.append(f"{label}: {needle!r} not in {haystack!r}")


def expect_not_in(label, needle, haystack):
    global CHECKS
    CHECKS += 1
    if needle in haystack:
        FAILURES.append(f"{label}: {needle!r} unexpectedly in {haystack!r}")


def write(directory, name, text):
    path = pathlib.Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


PASSING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="AppTest" tests="2">
  <testcase classname="AppTest" name="adds" file="src/AppTest.java" line="12"/>
  <testcase classname="AppTest" name="subtracts" file="src/AppTest.java" line="20"/>
</testsuite>
"""

FAILING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="AppTest" tests="4">
  <testcase classname="AppTest" name="ok" file="src/AppTest.java" line="5"/>
  <testcase classname="AppTest" name="bad" file="src/AppTest.java" line="9">
    <failure message="expected 4 but was 5" type="AssertionError">stack</failure>
  </testcase>
  <testcase classname="AppTest" name="blew-up" line="14">
    <error type="NullPointerException">boom in setUp</error>
  </testcase>
  <testcase classname="AppTest" name="todo">
    <skipped/>
  </testcase>
</testsuite>
"""


def check_pytest_json_is_unchanged():
    """The default path must behave exactly as it did before --format existed."""
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "report.json", json.dumps({"tests": [
            {"nodeid": "tests/test_a.py::test_one", "outcome": "passed"},
            {"nodeid": "tests/test_a.py::test_two", "outcome": "passed"},
        ]}))
        log = write(tmp, "pytest_output.txt", "2 passed in 0.1s")
        got = build_results(report, log)
        expect("pytest passing payload", got, {
            "score": 2,
            "max_score": 2,
            "output": "2/2 tests passed",
            "failures": [],
        })

        report = write(tmp, "fail.json", json.dumps({"tests": [
            {"nodeid": "tests/test_a.py::test_one", "outcome": "passed"},
            {
                "nodeid": "tests/test_a.py::test_two",
                "outcome": "failed",
                "call": {"crash": {
                    "path": "tests/test_a.py", "lineno": 7, "message": "assert 1 == 2",
                }},
            },
        ]}))
        got = build_results(report, log)
        expect("pytest failing score", (got["score"], got["max_score"]), (1, 2))
        expect("pytest failure entry", got["failures"], [
            {"path": "tests/test_a.py", "line": 7, "message": "assert 1 == 2"},
        ])

        empty = write(tmp, "empty.json", json.dumps({"tests": []}))
        got = build_results(empty, log)
        expect(
            "pytest collected nothing",
            got["output"],
            "Autograder error: no tests were collected.\n\n2 passed in 0.1s",
        )

        got = build_results(str(pathlib.Path(tmp) / "absent.json"), log)
        expect(
            "pytest report missing",
            got["output"],
            "Autograder error: pytest produced no usable report "
            "(it may have crashed or been killed).\n\n2 passed in 0.1s",
        )

        got = build_results(
            str(pathlib.Path(tmp) / "absent.json"),
            str(pathlib.Path(tmp) / "absent.txt"),
        )
        expect(
            "pytest report and log both missing",
            got["output"],
            "Autograder error: pytest produced no usable report "
            "(it may have crashed or been killed).\n\n"
            "(no pytest output was captured: the failure happened before "
            "pytest ran; check the Actions log)",
        )

        expect(
            "explicit pytest-json matches the default",
            build_results(report, log, "pytest-json"),
            build_results(report, log),
        )


def check_junit_passing():
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "TEST-AppTest.xml", PASSING_XML)
        got = build_results(report, "", "junit-xml")
        expect("junit passing payload", got, {
            "score": 2,
            "max_score": 2,
            "output": "2/2 tests passed",
            "failures": [],
        })


def check_junit_failing():
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "TEST-AppTest.xml", FAILING_XML)
        got = build_results(report, "", "junit-xml")
        expect("junit score counts skipped in the total", (got["score"], got["max_score"]), (1, 4))
        expect("junit failure entries", got["failures"], [
            {
                "path": "src/AppTest.java",
                "line": 9,
                "message": "expected 4 but was 5",
            },
            # No `file`, so `classname`. No `message`, so the child's text.
            {"path": "AppTest", "line": 14, "message": "boom in setUp"},
        ])


def check_junit_message_falls_back_to_the_tag():
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "r.xml", """<testsuite>
  <testcase classname="T" name="x"><failure/></testcase>
</testsuite>""")
        got = build_results(report, "", "junit-xml")
        expect("bare failure element", got["failures"], [
            {"path": "T", "line": 0, "message": "failure"},
        ])


def check_junit_testsuites_wrapper():
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "r.xml", """<testsuites>
  <testsuite name="one">
    <testcase classname="One" name="a"/>
  </testsuite>
  <testsuite name="two">
    <testcase classname="Two" name="b"/>
    <testcase classname="Two" name="c"><failure message="nope"/></testcase>
  </testsuite>
</testsuites>""")
        got = build_results(report, "", "junit-xml")
        expect("testsuites wrapper", (got["score"], got["max_score"]), (2, 3))


def check_junit_directory_aggregate():
    """Surefire writes one file per class; grading one of them grades a slice."""
    with tempfile.TemporaryDirectory() as tmp:
        reports = pathlib.Path(tmp) / "surefire-reports"
        write(reports, "TEST-A.xml", PASSING_XML)
        write(reports, "TEST-B.xml", FAILING_XML)
        write(reports, "A.txt", "not a report")
        got = build_results(str(reports), "", "junit-xml")
        expect("directory aggregate", (got["score"], got["max_score"]), (3, 6))
        expect("directory aggregate failures", len(got["failures"]), 2)

        got = build_results(str(reports / "TEST-*.xml"), "", "junit-xml")
        expect("glob aggregate", (got["score"], got["max_score"]), (3, 6))

    with tempfile.TemporaryDirectory() as tmp:
        # A multi-module build: the pattern names the report directories.
        write(pathlib.Path(tmp) / "core/build/test-results/test", "A.xml", PASSING_XML)
        write(pathlib.Path(tmp) / "web/build/test-results/test", "B.xml", PASSING_XML)
        got = build_results(
            str(pathlib.Path(tmp) / "*/build/test-results/test"), "", "junit-xml",
        )
        expect("glob over directories", (got["score"], got["max_score"]), (4, 4))

    with tempfile.TemporaryDirectory() as tmp:
        # A real directory whose name looks like a pattern must be read as a
        # directory, not expanded and found empty.
        literal = pathlib.Path(tmp) / "[core]-reports"
        write(literal, "A.xml", PASSING_XML)
        got = build_results(str(literal), "", "junit-xml")
        expect("a literal path wins over glob syntax", got["max_score"], 2)


def check_junit_zero_testcases():
    with tempfile.TemporaryDirectory() as tmp:
        log = write(tmp, "out.txt", "BUILD FAILURE: could not resolve dependencies")
        report = write(tmp, "r.xml", "<testsuite name='none'></testsuite>")
        got = build_results(report, log, "junit-xml")
        expect("empty suite score", (got["score"], got["max_score"]), (0, 0))
        expect(
            "empty suite output",
            got["output"],
            "Autograder error: no tests were collected.\n\n"
            "BUILD FAILURE: could not resolve dependencies",
        )

        empty_dir = pathlib.Path(tmp) / "surefire-reports"
        empty_dir.mkdir()
        got = build_results(str(empty_dir), log, "junit-xml")
        expect_in("empty directory is reported", MARKER, got["output"])
        expect("empty directory scores nothing", got["max_score"], 0)
        expect_in(
            "empty directory carries the console tail",
            "BUILD FAILURE",
            got["output"],
        )

        got = build_results(str(pathlib.Path(tmp) / "nowhere"), log, "junit-xml")
        expect_in("missing path is reported", MARKER, got["output"])


def check_junit_malformed():
    with tempfile.TemporaryDirectory() as tmp:
        log = write(tmp, "out.txt", "surefire crashed")
        report = write(tmp, "r.xml", "<testsuite><testcase/>")
        got = build_results(report, log, "junit-xml")
        expect("malformed scores nothing", (got["score"], got["max_score"]), (0, 0))
        expect_in("malformed is reported", MARKER, got["output"])
        expect_in("malformed says what happened", "not valid XML", got["output"])

        # One bad file among good ones must not report the good ones' score:
        # a partial denominator is a wrong grade nobody would notice.
        write(tmp, "reports/TEST-A.xml", PASSING_XML)
        write(tmp, "reports/TEST-B.xml", "<testsuite>")
        got = build_results(str(pathlib.Path(tmp) / "reports"), log, "junit-xml")
        expect("one bad file voids the aggregate", (got["score"], got["max_score"]), (0, 0))
        expect_in("bad file in an aggregate is reported", MARKER, got["output"])


def check_junit_doctype_is_refused():
    bomb = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<testsuite><testcase classname="T" name="&lol3;"/></testsuite>
"""
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "r.xml", bomb)
        got = build_results(report, "", "junit-xml")
        expect("bomb scores nothing", (got["score"], got["max_score"]), (0, 0))
        expect_in("bomb is reported", MARKER, got["output"])
        expect_in("bomb names the cause", "DOCTYPE", got["output"])

        report = write(tmp, "entity.xml", '<!ENTITY a "b"><testsuite/>')
        got = build_results(report, "", "junit-xml")
        expect_in("a bare entity declaration is refused", MARKER, got["output"])


# What Surefire actually writes: no `file` and no `line` attribute, and a
# body whose first frames belong to the assertion library rather than to the
# student's repo.
SUREFIRE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.StackTest" tests="1" failures="1">
  <testcase classname="com.example.StackTest" name="testPop" time="0.01">
    <failure message="expected: &lt;4&gt; but was: &lt;5&gt;" type="AssertionFailedError">
org.opentest4j.AssertionFailedError: expected: &lt;4&gt; but was: &lt;5&gt;
	at org.junit.jupiter.api.AssertionFailureBuilder.build(AssertionFailureBuilder.java:151)
	at org.junit.jupiter.api.AssertEquals.failNotEqual(AssertEquals.java:197)
	at com.example.StackTest.testPop(StackTest.java:42)
	at java.base/java.lang.reflect.Method.invoke(Method.java:568)
    </failure>
  </testcase>
</testsuite>
"""


def check_junit_locates_a_surefire_failure():
    """Surefire gives a classname and no line, so the anchor is in the trace.

    Without this every Java failure arrives with line 0, `_located_failures`
    on the server drops it, and inline review comments — the product's
    headline — degrade to a summary comment for the whole language.
    """
    with tempfile.TemporaryDirectory() as tmp:
        write(tmp, "src/test/java/com/example/StackTest.java", "class StackTest {}")
        report = write(tmp, "TEST-com.example.StackTest.xml", SUREFIRE_XML)
        got = build_results(report, "", "junit-xml", workspace=tmp)
        expect("surefire failure is anchored", got["failures"], [{
            "path": "src/test/java/com/example/StackTest.java",
            "line": 42,
            "message": "expected: <4> but was: <5>",
        }])


def check_junit_location_skips_frames_outside_the_repo():
    """A frame naming no file in the checkout is skipped, not given up on."""
    with tempfile.TemporaryDirectory() as tmp:
        # Only the library frames resolve nowhere; nothing here matches at all.
        report = write(tmp, "r.xml", SUREFIRE_XML)
        got = build_results(report, "", "junit-xml", workspace=tmp)
        expect("no match leaves it unset", got["failures"], [{
            "path": "com.example.StackTest",
            "line": 0,
            "message": "expected: <4> but was: <5>",
        }])


def check_junit_ambiguous_filename_is_left_unset():
    """Two files of that name is a coin flip, and a wrong anchor is worse."""
    with tempfile.TemporaryDirectory() as tmp:
        write(tmp, "core/src/StackTest.java", "class StackTest {}")
        write(tmp, "web/src/StackTest.java", "class StackTest {}")
        report = write(tmp, "r.xml", SUREFIRE_XML)
        got = build_results(report, "", "junit-xml", workspace=tmp)
        expect("ambiguous path", got["failures"][0]["path"], "com.example.StackTest")
        expect("ambiguous line", got["failures"][0]["line"], 0)


def check_junit_location_ignores_skipped_directories():
    """A build output copy must not make the source ambiguous."""
    with tempfile.TemporaryDirectory() as tmp:
        write(tmp, "src/test/java/com/example/StackTest.java", "class StackTest {}")
        write(tmp, "target/generated-test-sources/StackTest.java", "copy")
        write(tmp, "node_modules/pkg/StackTest.java", "copy")
        report = write(tmp, "r.xml", SUREFIRE_XML)
        got = build_results(report, "", "junit-xml", workspace=tmp)
        expect(
            "build output does not shadow the source",
            got["failures"][0]["path"],
            "src/test/java/com/example/StackTest.java",
        )


def check_junit_location_reads_a_node_frame():
    """One regex, not one per language: jest frames carry a column too."""
    trace = "Error: nope\n    at Object.&lt;anonymous&gt; (/home/runner/work/r/r/src/cart.js:17:5)"
    with tempfile.TemporaryDirectory() as tmp:
        write(tmp, "src/cart.js", "module.exports = {}")
        report = write(tmp, "junit.xml", f"""<testsuite>
  <testcase classname="cart" name="totals"><failure>{trace}</failure></testcase>
</testsuite>""")
        got = build_results(report, "", "junit-xml", workspace=tmp)
        expect("node frame is anchored", (
            got["failures"][0]["path"], got["failures"][0]["line"],
        ), ("src/cart.js", 17))


def check_junit_file_attribute_wins():
    """A runner that supplies `file` is authoritative; the trace is a guess."""
    with tempfile.TemporaryDirectory() as tmp:
        write(tmp, "src/test/java/com/example/StackTest.java", "class StackTest {}")
        report = write(tmp, "r.xml", SUREFIRE_XML.replace(
            '<testcase classname="com.example.StackTest" name="testPop" time="0.01">',
            '<testcase classname="com.example.StackTest" name="testPop"'
            ' file="given/Other.java" line="7">',
        ))
        got = build_results(report, "", "junit-xml", workspace=tmp)
        expect("the file attribute wins", (
            got["failures"][0]["path"], got["failures"][0]["line"],
        ), ("given/Other.java", 7))


def check_junit_location_cap():
    """A walk that hit its cap resolves nothing rather than half the tree.

    A basename unique only because the rest of the checkout went unread is
    exactly the wrong-path anchor this must never produce.
    """
    import junit_report

    with tempfile.TemporaryDirectory() as tmp:
        write(tmp, "src/test/java/com/example/StackTest.java", "class StackTest {}")
        report = write(tmp, "r.xml", SUREFIRE_XML)
        saved = junit_report.MAX_WORKSPACE_FILES
        try:
            junit_report.MAX_WORKSPACE_FILES = 1
            got = build_results(report, "", "junit-xml", workspace=tmp)
        finally:
            junit_report.MAX_WORKSPACE_FILES = saved
        expect("a truncated walk anchors nothing", got["failures"][0]["line"], 0)


def check_junit_caps():
    from junit_report import MAX_BYTES_PER_FILE, MAX_FILES, MAX_MESSAGE_CHARS

    with tempfile.TemporaryDirectory() as tmp:
        trace = "at com.example.App.main(App.java:1)\n" * 500
        report = write(tmp, "r.xml", f"""<testsuite>
  <testcase classname="T" name="x"><failure>{trace}</failure></testcase>
</testsuite>""")
        got = build_results(report, "", "junit-xml")
        message = got["failures"][0]["message"]
        expect("a stack trace is capped", len(message) <= MAX_MESSAGE_CHARS + 12, True)
        expect_in("the cap says so", "(truncated)", message)
        expect_in("the cap keeps the head of the trace", "com.example.App", message)

    with tempfile.TemporaryDirectory() as tmp:
        padding = " " * (MAX_BYTES_PER_FILE + 1)
        report = write(tmp, "big.xml", f"<testsuite>{padding}</testsuite>")
        got = build_results(report, "", "junit-xml")
        expect("oversized file scores nothing", got["max_score"], 0)
        expect_in("oversized file is reported", MARKER, got["output"])

    with tempfile.TemporaryDirectory() as tmp:
        reports = pathlib.Path(tmp) / "many"
        for index in range(MAX_FILES + 3):
            write(reports, f"TEST-{index:04d}.xml", PASSING_XML)
        got = build_results(str(reports), "", "junit-xml")
        expect("file cap bounds the aggregate", got["max_score"], MAX_FILES * 2)
        expect_in("file cap is said out loud", str(MAX_FILES), got["output"])
        # The marker means infra_error on the server, which discards the
        # result — a capped-but-real score must not be thrown away.
        expect_not_in("file cap is not an error run", MARKER, got["output"])


def check_unknown_format_is_refused():
    """It raises here and `main` turns that into a reported error run.

    Never a silent fallback to pytest-json: a repo asking for a format we do
    not have would then be graded 0/0 by the wrong parser and read as a
    student who wrote no tests.
    """
    global CHECKS
    CHECKS += 1
    with tempfile.TemporaryDirectory() as tmp:
        report = write(tmp, "r.xml", PASSING_XML)
        try:
            build_results(report, "", "junit-json")
        except ValueError as exc:
            expect_in("the refusal names both formats", "junit-xml", str(exc))
            return
        FAILURES.append("an unknown format was accepted")


def check_results_json_is_always_written():
    """The end-to-end contract: the script leaves a results.json behind."""
    cases = [
        ("junit passing", ["--format", "junit-xml", "TEST-A.xml"], 2, 2),
        ("junit bomb", ["--format", "junit-xml", "bomb.xml"], 0, 0),
        ("junit missing dir", ["--format", "junit-xml", "nowhere"], 0, 0),
        ("unknown format", ["--format", "nope", "TEST-A.xml"], 0, 0),
        ("format with no value", ["--format"], 0, 0),
        ("format=value spelling", ["--format=junit-xml", "TEST-A.xml"], 2, 2),
    ]
    for label, args, score, max_score in cases:
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "TEST-A.xml", PASSING_XML)
            write(tmp, "bomb.xml", '<!DOCTYPE x []><testsuite/>')
            environ = dict(os.environ, RUNNER_TEMP=tmp)
            run = subprocess.run(
                [sys.executable, SCRIPT] + args,
                cwd=tmp, env=environ, capture_output=True, text=True,
            )
            expect(f"{label} exits 0", run.returncode, 0)
            written = pathlib.Path(tmp) / "results.json"
            if not written.exists():
                FAILURES.append(f"{label}: no results.json was written")
                continue
            payload = json.loads(written.read_text())
            expect(f"{label} score", (payload["score"], payload["max_score"]), (score, max_score))
            expect(f"{label} shape", sorted(payload), ["failures", "max_score", "output", "score"])


def check_action_yml_passes_the_format_through():
    """The script half is inert unless action.yml declares and forwards it."""
    global CHECKS
    text = (pathlib.Path(__file__).parents[1] / "action.yml").read_text()
    build_step = text.split("- name: Build results.json", 1)[-1]
    build_step = build_step.split("\n    - name: ", 1)[0]
    for label, needle, haystack in [
        ("action.yml declares a format input", "\n  format:\n", text),
        ("action.yml defaults format to pytest-json", "default: 'pytest-json'", text),
        ("the build step forwards it", "${{ inputs.format }}", build_step),
    ]:
        CHECKS += 1
        if needle not in haystack:
            FAILURES.append(f"{label}: {needle!r} missing")


def main():
    for check in (
        check_pytest_json_is_unchanged,
        check_junit_passing,
        check_junit_failing,
        check_junit_message_falls_back_to_the_tag,
        check_junit_testsuites_wrapper,
        check_junit_directory_aggregate,
        check_junit_zero_testcases,
        check_junit_malformed,
        check_junit_doctype_is_refused,
        check_junit_locates_a_surefire_failure,
        check_junit_location_skips_frames_outside_the_repo,
        check_junit_ambiguous_filename_is_left_unset,
        check_junit_location_ignores_skipped_directories,
        check_junit_location_reads_a_node_frame,
        check_junit_file_attribute_wins,
        check_junit_location_cap,
        check_junit_caps,
        check_unknown_format_is_refused,
        check_results_json_is_always_written,
        check_action_yml_passes_the_format_through,
    ):
        try:
            check()
        except Exception as exc:
            FAILURES.append(f"{check.__name__} raised {exc!r}")

    for line in FAILURES:
        print(f"FAIL {line}", file=sys.stderr)
    print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
