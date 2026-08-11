#!/usr/bin/env python3
"""Push brief files to the xxy20010606/briefs repo via GitHub REST API.

Why: `git push` to github.com:443 times out on this machine, but the
GitHub REST API (api.github.com) is reachable through the proxy. This
script builds a new commit on top of the *remote* main head (blob ->
tree -> commit -> update ref), so it never depends on local git state
and cannot diverge from the remote.

Token: read from env GITHUB_TOKEN, else from a local file `.ghtoken`
in this directory. The token is NEVER hard-coded here so it is not
committed to the repo (GitHub secret scanning would block it).

Usage:
    python3 push_brief.py                 # pushes semiconductor.html + today's archive
    python3 push_brief.py <file> [<f>..]  # pushes the given files (relative to this dir)
"""
import base64
import datetime
import json
import os
import sys
import urllib.request
import urllib.error

REPO = "xxy20010606/briefs"
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
    "User-Agent": "briefs-pusher",
    "X-GitHub-Api-Version": "2022-11-28",
}


def get_token():
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    p = os.path.join(REPO_DIR, ".ghtoken")
    if os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    raise SystemExit("No token: set GITHUB_TOKEN or create .ghtoken")


def api(method, path, data=None):
    url = BASE + path
    headers = dict(HEADERS)
    headers["Authorization"] = f"Bearer {get_token()}"
    req = urllib.request.Request(url, method=method, headers=headers)
    if data is not None:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        raise SystemExit(f"API {method} {path} failed {e.code}: {body}")


def main():
    today = datetime.date.today().strftime("%Y-%m-%d")
    if len(sys.argv) > 1:
        rel_paths = sys.argv[1:]
    else:
        rel_paths = [
            "semiconductor.html",
            f"archive/semiconductor-{today}.html",
        ]

    ref = api("GET", "/git/ref/heads/main")
    parent_sha = ref["object"]["sha"]
    head_commit = api("GET", f"/git/commits/{parent_sha}")
    base_tree_sha = head_commit["tree"]["sha"]

    tree_entries = []
    pushed = []
    for rel in rel_paths:
        abs_path = os.path.join(REPO_DIR, rel)
        if not os.path.isfile(abs_path):
            print(f"  SKIP (missing) {rel}")
            continue
        with open(abs_path, "rb") as fh:
            raw = fh.read()
        blob = api("POST", "/git/blobs", {
            "content": base64.b64encode(raw).decode(),
            "encoding": "base64",
        })
        tree_entries.append({
            "path": rel,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        pushed.append(rel)

    if not tree_entries:
        print("Nothing to push (no files found).")
        return

    tree = api("POST", "/git/trees", {
        "base_tree": base_tree_sha,
        "tree": tree_entries,
    })
    commit = api("POST", "/git/commits", {
        "message": f"更新半导体简报 - {today}",
        "tree": tree["sha"],
        "parents": [parent_sha],
    })
    api("PATCH", "/git/refs/heads/main", {"sha": commit["sha"]})

    print(f"Pushed {len(pushed)} file(s) -> {commit['sha'][:10]}: " + ", ".join(pushed))


if __name__ == "__main__":
    main()
