#!/usr/bin/env python3
"""Authenticated autograder result uploader.

IMPORTANT: this file does NOT run inside the cin90 Django app. It is a
reference artifact that instructors copy into each assignment *template*
repository (alongside ``autograde.yml``). It executes inside the student
repo's GitHub Actions CI run, after the test step has produced a results
JSON file.

What it does:
  1. Reads the results JSON file named on argv[1]
     ({"score", "max_score", "output", "failures"}).
  2. Builds the cin90 ingest payload, adding repo / sha / pull_request from
     the CI environment.
  3. Asks GitHub to mint a short-lived OIDC token for this run and POSTs the
     payload to ``{CIN90_BASE_URL}/classroom/autograde/ingest/`` with
     ``Authorization: Bearer <jwt>``.

The token is signed by GitHub and carries a ``repository`` claim naming this
repo. cin90 verifies both in ``classroom/github/oidc.py``. This path *reads* no
secret, which is the point: a student has `push` on their own repo and can read
any Actions secret it holds, so an HMAC key kept there was never a credential
they could not also forge with.

(cin90's provisioning still WRITES ``CLASSROOM_AUTOGRADE_REPO_SECRET`` into new
repos for the legacy path below, and the server still accepts it. The stored
secret only stops existing when that path is retired — until then this file not
reading it is a smaller claim than the repo not holding it.)

LEGACY FALLBACK. When the OIDC request variables are absent — a repo still
running a workflow without ``permissions: id-token: write`` — this falls back
to signing the body with the repo's ``CLASSROOM_AUTOGRADE_REPO_SECRET`` and
sending ``X-Hub-Signature-256``. The server accepts both while repos migrate.
The fallback is chosen on the *absence* of OIDC, never on its failure: a
mint that errors is reported and the run fails, because silently downgrading
to the weaker credential is how a broken migration goes unnoticed for a term.

Stdlib only (hmac, hashlib, json, os, sys, urllib) — no third-party deps, so
it runs on a bare ``actions/setup-python`` step.

Environment variables:
  CIN90_BASE_URL              Base URL of the cin90 site, e.g. https://coderin90.com
  ACTIONS_ID_TOKEN_REQUEST_URL    Set by Actions when the job grants
  ACTIONS_ID_TOKEN_REQUEST_TOKEN  ``id-token: write``. Actions sets both
                              together; this file requires both anyway rather
                              than trusting that.
  CLASSROOM_AUTOGRADE_REPO_SECRET  Legacy: this repo's derived HMAC key
                              (REPO-scoped Actions secret, written by cin90
                              provisioning). Falls back in turn to
                              CLASSROOM_AUTOGRADE_SECRET, the older name.
  GITHUB_REPOSITORY           "owner/name" (provided automatically by Actions)
  GITHUB_SHA                  Commit SHA being graded (provided by Actions)
  PR_NUMBER                   Pull request number, or empty/unset for a push
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

INGEST_PATH = "/classroom/autograde/ingest/"

# The audience GitHub is asked to mint for, and the only one cin90 accepts. It
# must match `classroom/github/oidc.py:AUDIENCE` byte for byte; a test in the
# cin90 repo fails if the two drift. Do NOT edit this to match your own
# deployment's hostname — it is an opaque identifier the two ends agree on, not
# a URL anything fetches, so it stays `coderin90.com` even on a self-hosted
# deployment (where `AUDIENCE` is hardcoded to the same value). Changing it
# here alone rejects every grade.
OIDC_AUDIENCE = "https://coderin90.com/classroom/autograde"


def _require_env(name):
    """Return a required env var or exit non-zero with a clear message."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"report_autograde: missing required env var {name}", file=sys.stderr)
        raise SystemExit(2)
    return value


def _parse_pr_number(raw):
    """Map the PR_NUMBER env value to an int, or None when absent/invalid.

    GitHub sets ``github.event.pull_request.number`` to an empty string on
    ``push`` events, so anything non-numeric becomes ``None`` (no PR).
    """
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def build_payload(results, repo, sha, pr_number):
    """Combine the test-step results with CI metadata into the ingest body."""
    return {
        "repo": repo,
        "sha": sha,
        "pull_request": pr_number,
        "score": int(results.get("score", 0)),
        "max_score": int(results.get("max_score", 0)),
        "output": str(results.get("output", "")),
        "failures": results.get("failures", []),
    }


def sign(secret, body_bytes):
    """Return the ``sha256=<hex>`` signature for the raw request body."""
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def fetch_oidc_token(audience, request_url, request_token, attempts=3):
    """Ask Actions for an OIDC token addressed to ``audience``.

    ``request_url`` already carries a query string (``?api-version=...``), so
    the audience is appended with ``&``.

    Retried, because this replaced an `hmac.new()` call that could not fail with
    a network round-trip that can, on the only path that reports a grade. A lost
    grade is invisible to everyone: the submission stays "submitted" and the
    student's red workflow run is the sole signal. GitHub's own `OidcClient`
    retries this same call for the same reason.

    Raises after the last attempt — the caller must not fall back to the legacy
    secret when a mint that was *attempted* goes wrong.
    """
    url = f"{request_url}&audience={urllib.parse.quote(audience, safe='')}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {request_token}",
            "Accept": "application/json; api-version=2.0",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
            token = (body.get("value") or "").strip()
            if not token:
                raise ValueError("the Actions OIDC endpoint returned no token value")
            return token
        except Exception as exc:  # noqa: BLE001 — retry anything, then re-raise
            if attempt == attempts:
                raise
            print(
                f"report_autograde: OIDC token request failed ({exc!r}), "
                f"retry {attempt}/{attempts - 1}",
                file=sys.stderr,
            )
            time.sleep(2**attempt)


def post(url, body_bytes, headers):
    """POST the body with the given auth headers; return the HTTP status code."""
    request = urllib.request.Request(
        url,
        data=body_bytes,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.status


def main(argv):
    if len(argv) < 2:
        print("usage: report_autograde.py <results.json>", file=sys.stderr)
        return 2

    base_url = _require_env("CIN90_BASE_URL").rstrip("/")
    repo = _require_env("GITHUB_REPOSITORY")
    sha = _require_env("GITHUB_SHA")
    pr_number = _parse_pr_number(os.environ.get("PR_NUMBER"))

    with open(argv[1], encoding="utf-8") as handle:
        results = json.load(handle)

    payload = build_payload(results, repo, sha, pr_number)
    # Serialize once, sign and send the exact same bytes — on the legacy path
    # any re-encoding would change the digest and the server would reject it.
    body_bytes = json.dumps(payload).encode("utf-8")
    url = f"{base_url}{INGEST_PATH}"

    oidc_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    oidc_request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if oidc_url and oidc_request_token:
        try:
            token = fetch_oidc_token(OIDC_AUDIENCE, oidc_url, oidc_request_token)
        except Exception as exc:  # noqa: BLE001 — every failure is fatal here
            print(
                f"report_autograde: could not mint a GitHub OIDC token: {exc!r}",
                file=sys.stderr,
            )
            return 1
        headers = {"Authorization": f"Bearer {token}"}
    else:
        # Actions injected no OIDC request variables. Usually that means the
        # workflow lacks `permissions: id-token: write`, but a fork's
        # `pull_request` run does not get them either however the block is
        # written — so name both rather than sending the reader to fix a file
        # that is already correct.
        #
        # Accept the older secret name too, so re-copying this file into a repo
        # whose secret is still called CLASSROOM_AUTOGRADE_SECRET keeps working;
        # both hold the same *derived* per-repo value and the server only checks
        # the value.
        print(
            "report_autograde: no Actions OIDC token available (the workflow may "
            "lack `permissions: id-token: write`, or this is an event that does "
            "not grant one, e.g. a pull request from a fork) — falling back to "
            "the legacy repo secret",
            file=sys.stderr,
        )
        if os.environ.get("CLASSROOM_AUTOGRADE_REPO_SECRET", "").strip():
            secret = _require_env("CLASSROOM_AUTOGRADE_REPO_SECRET")
        else:
            secret = _require_env("CLASSROOM_AUTOGRADE_SECRET")
        headers = {"X-Hub-Signature-256": sign(secret, body_bytes)}

    try:
        status = post(url, body_bytes, headers)
    except urllib.error.HTTPError as exc:
        print(f"report_autograde: ingest returned HTTP {exc.code}", file=sys.stderr)
        print(exc.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"report_autograde: network error posting to {url}: {exc}", file=sys.stderr)
        return 1

    if 200 <= status < 300:
        print(f"report_autograde: ingest accepted (HTTP {status})")
        return 0
    print(f"report_autograde: ingest rejected (HTTP {status})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
