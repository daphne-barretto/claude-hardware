"""Smoke test the live session API of a running app.

  session_smoke.py BASE_URL [--out FILE] [--wait-for-commit SHA]

Creates a session (`POST /api/sessions`, expects 201), posts a message to it
(`POST /api/sessions/{id}/messages`, expects 202) and fetches its parameters
(`GET /api/sessions/{id}/parameters`, expects 200 and a list), the calls a new chat
makes. Exits non-zero on any other status, a 5xx included, or if the app can't be
reached.
With `--out` it also writes {"session": "pass"|"fail", "detail": "..."} to FILE. With `--wait-for-commit` it first polls `/health` (up to
15 minutes) until the app reports that commit, as after a staging deploy.

CI runs it against the built image, so a file the image is
missing can't hide behind a checkout that has it.
"""

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MESSAGE = "make a box"
WAIT_SECONDS = 15 * 60


def browser():
    """A `request` with its own cookie jar, as a browser has: the app gives each browser its
    own sessions through a cookie, so later requests must send it back."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    return lambda method, url, body: request(method, url, body, opener.open)


def request(method, url, body, open_url=urllib.request.urlopen):
    """(status, response text) for a JSON request; HTTP errors return their status."""
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with open_url(req, timeout=120) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def get_commit(base):
    """The commit `/health` reports, or None if the app doesn't answer."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/health", timeout=30) as resp:
            return str(json.loads(resp.read()).get("commit") or "")
    except (OSError, ValueError):
        return None


def wait_for_commit(base, sha, read=get_commit, sleep=time.sleep, seconds=WAIT_SECONDS):
    """True once `/health` reports `sha` (either may be abbreviated), False on timeout."""
    deadline = time.monotonic() + seconds
    while True:
        commit = read(base)
        if commit and (commit.startswith(sha) or sha.startswith(commit)):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(10)


def smoke(base, send=None):
    """The problems found, empty if a session starts and takes a message."""
    send = send or browser()
    base = base.rstrip("/")
    try:
        status, text = send("POST", f"{base}/api/sessions", {})
        if status != 201:
            return [f"POST /api/sessions returned {status}, expected 201: {text[:500]}"]
        sid = json.loads(text)["id"]
        url = f"{base}/api/sessions/{sid}/messages"
        status, text = send("POST", url, {"text": MESSAGE})
        if status != 202:
            return [
                f"POST /api/sessions/{sid}/messages returned {status}, expected 202: {text[:500]}"
            ]
        status, text = send("GET", f"{base}/api/sessions/{sid}/parameters", None)
        if status != 200:
            return [
                f"GET /api/sessions/{sid}/parameters returned {status}, expected 200: {text[:500]}"
            ]
        if not isinstance(json.loads(text), list):
            return [f"GET /api/sessions/{sid}/parameters is not a list: {text[:500]}"]
    except (OSError, ValueError, KeyError) as exc:
        return [f"the session API could not be reached or answered badly: {exc!r}"]
    return []


def main(argv):
    args = argv[1:]
    opts = {}
    for flag in ("--out", "--wait-for-commit"):
        if flag in args:
            i = args.index(flag)
            opts[flag] = args[i + 1]
            del args[i : i + 2]
    if len(args) != 1:
        raise SystemExit(__doc__)
    out = Path(opts["--out"]) if "--out" in opts else None
    sha = opts.get("--wait-for-commit")
    if sha and not wait_for_commit(args[0], sha):
        problems = [f"/health did not report commit {sha} within {WAIT_SECONDS // 60} minutes"]
    else:
        problems = smoke(args[0])
    detail = "; ".join(problems) or "a session started, took a message and listed its parameters"
    print(("FAIL: " if problems else "ok: ") + detail)
    if out is not None:
        out.write_text(json.dumps({"session": "fail" if problems else "pass", "detail": detail}))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
