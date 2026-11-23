#!/usr/bin/env python3
"""Status and upstream distance for every git checkout under a directory.

One scanner feeds three surfaces so they can never disagree:

  codedash serve   HTTP dashboard + JSON API (the systemd unit)
  codedash list    scriptable table, or --json
  codedash tui     curses view with keyboard navigation

The scan is deliberately read-only: every git invocation runs with
GIT_OPTIONAL_LOCKS=0 and `status` additionally gets the global
--no-optional-locks flag, because a plain `git status` refreshes the index
and would make this service a writer of 200 repositories.

Nothing is fetched during a scan, so "behind" reflects whatever the
remote-tracking refs said the last time a human ran `git fetch`;
refs_fetched_at carries that timestamp so the UI can say so instead of
pretending the count is fresh. Updating those refs is one of exactly three
explicit writes — fetch, fast-forward, rebase — which are only reachable
through a CLI subcommand or a confirmed click, and which re-probe the
checkout and check its preconditions before touching it.

Discovery is the part that is easy to get wrong. ~/Documents/git is not flat:
there are repositories nested inside other repositories, and roughly half of
the checkouts are linked worktrees whose `.git` is a file pointing back into
a main repository. Checkouts are therefore keyed by their absolute per-worktree
git dir (a unique checkout) and grouped by their common dir (a unique physical
repository); keying by path or by the raw --git-common-dir output would either
split or collapse those cases.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import datetime
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"

DEFAULT_ROOT = os.path.expanduser("~/Documents/git")
DEFAULT_PORT = 8090
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
DEFAULT_STATE_DIR = "/var/lib/codedash"

# Where nginx mounts it. The handler also serves the unprefixed paths, so the
# page's relative fetch() calls resolve the same on loopback and behind nginx.
BASE_PATH = "/codedash"

# Depth is bounded because the tree carries node_modules and build outputs;
# four levels reach every real checkout here (deepest seen:
# `pastebin-r2/.worktrees/design`) without walking dependency trees.
MAX_DEPTH = 4
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".direnv",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    ".cache",
}
SKIP_PREFIXES = ("result-",)

CACHE_TTL = 30
SCAN_INTERVAL = 60
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 15

# Worst-first ordering shared by every surface.
STATE_RANK = {
    "error": 0,
    "conflicted": 1,
    "operation": 2,
    "dirty": 3,
    "ahead": 4,
    "behind": 5,
    "no-upstream": 6,
    "clean": 7,
}

ANSI = {
    "error": "\033[31m",
    "conflicted": "\033[31m",
    "operation": "\033[33m",
    "dirty": "\033[33m",
    "ahead": "\033[34m",
    "behind": "\033[35m",
    "no-upstream": "\033[2m",
    "clean": "\033[32m",
}
RESET = "\033[0m"


# --------------------------------------------------------------------------
# git plumbing


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            # A missing credential must fail fast rather than hang a scan or
            # leave a prompt on a tty nobody is watching.
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return env


GIT_ENV = _env()


def run_git(path: str, args: list[str], timeout: int, no_locks: bool = True, extra_env: dict[str, str] | None = None):
    """Run one git command against `path`, never prompting, never locking."""
    cmd = ["git"]
    if no_locks:
        # Global flag: it has to sit before `-C`/the subcommand, and
        # `git status --no-optional-locks` is a usage error.
        cmd.append("--no-optional-locks")
    cmd += ["-C", path, *args]
    env = dict(GIT_ENV)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_mtime(path: str) -> str | None:
    try:
        ts = os.stat(path).st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def discover(root: str, max_depth: int = MAX_DEPTH) -> list[str]:
    """Every directory under root that carries a .git entry, to max_depth."""
    found: list[str] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        # Check before pruning: a checkout at max_depth must still be seen.
        if ".git" in dirnames or ".git" in filenames:
            found.append(dirpath)
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(SKIP_PREFIXES)
        ]
        if depth >= max_depth:
            dirnames[:] = []
    return sorted(found)


def detect_operation(gitdir: str) -> str:
    """Which multi-step operation is paused mid-flight, if any."""
    join = os.path.join
    # Rebase state lives in rebase-merge/ or rebase-apply/; checked first
    # because a rebase can also leave a MERGE_HEAD-looking state behind.
    if os.path.isdir(join(gitdir, "rebase-merge")) or os.path.isdir(join(gitdir, "rebase-apply")):
        return "rebase"
    for marker, operation in (
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ):
        if os.path.exists(join(gitdir, marker)):
            return operation
    return "none"


def parse_status(text: str) -> dict:
    """Parse `status --porcelain=v2 --branch` into counters and refs."""
    info = {
        "branch": None,
        "detached": False,
        "head": "",
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
    }
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            oid = line[len("# branch.oid ") :].strip()
            info["head"] = "" if oid == "(initial)" else oid[:7]
        elif line.startswith("# branch.head "):
            head = line[len("# branch.head ") :].strip()
            if head == "(detached)":
                info["detached"] = True
            else:
                info["branch"] = head
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            match = re.match(r"\+(\d+)\s+-(\d+)", line[len("# branch.ab ") :])
            if match:
                info["ahead"], info["behind"] = int(match.group(1)), int(match.group(2))
        elif line.startswith("u "):
            info["conflicted"] += 1
        elif line.startswith("? "):
            info["untracked"] += 1
        elif line[:2] in ("1 ", "2 "):
            xy = line[2:4]
            if xy[0] not in ". ":
                info["staged"] += 1
            if len(xy) > 1 and xy[1] not in ". ":
                info["unstaged"] += 1
    return info


def compute_state(rec: dict) -> str:
    if rec.get("error"):
        return "error"
    if rec["conflicted"]:
        return "conflicted"
    if rec["operation"] != "none":
        return "operation"
    if rec["staged"] or rec["unstaged"] or rec["untracked"]:
        return "dirty"
    if rec["ahead"]:
        return "ahead"
    if rec["behind"]:
        return "behind"
    if not rec["upstream"]:
        return "no-upstream"
    return "clean"


def blank_record(path: str) -> dict:
    return {
        "path": path,
        "name": os.path.basename(path.rstrip("/")) or path,
        "main_repo": path,
        "kind": "main",
        "branch": None,
        "detached": False,
        "head": "",
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "refs_fetched_at": None,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
        "operation": "none",
        "stashes": 0,
        "last_commit": None,
        "state": "clean",
        "nested": False,
        "error": None,
    }


def probe(path: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Collect one checkout's record. Never raises; failures become records."""
    rec = blank_record(path)
    gitdir = commondir = None
    try:
        cp = run_git(
            path,
            ["rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir"],
            timeout,
        )
        if cp.returncode != 0:
            raise RuntimeError(first_line(cp.stderr) or "git rev-parse failed")
        lines = [line for line in cp.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            raise RuntimeError("git rev-parse returned no paths")
        gitdir, commondir = lines[0], lines[1]
        # The per-worktree git dir is unique per checkout; the common dir is
        # shared by every worktree of one physical repository.
        is_worktree = os.path.realpath(gitdir) != os.path.realpath(commondir)
        rec["kind"] = "worktree" if is_worktree else "main"
        if is_worktree:
            # `commondir` is <main checkout>/.git, so its parent is the main
            # checkout. Phase two replaces this with the authoritative path
            # from `git worktree list` when it can.
            rec["main_repo"] = os.path.dirname(os.path.abspath(commondir))

        st = run_git(
            path,
            ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"],
            timeout,
        )
        if st.returncode != 0:
            raise RuntimeError(first_line(st.stderr) or "git status failed")
        rec.update(parse_status(st.stdout))

        lg = run_git(path, ["log", "-1", "--format=%h%x00%aI%x00%an%x00%s"], timeout)
        # An unborn branch has no commit to report; that is not an error.
        if lg.returncode == 0 and lg.stdout.strip():
            parts = lg.stdout.rstrip("\n").split("\x00")
            if len(parts) == 4:
                rec["last_commit"] = {
                    "sha": parts[0],
                    "iso_time": parts[1],
                    "author": parts[2],
                    "subject": parts[3],
                }

        sl = run_git(path, ["stash", "list", "--format=%gd"], timeout)
        if sl.returncode == 0:
            rec["stashes"] = sum(1 for line in sl.stdout.splitlines() if line.strip())

        rec["operation"] = detect_operation(gitdir)
        # FETCH_HEAD is per-worktree, but the remote refs it refreshes are
        # shared through the common dir. A fetch in the main checkout is what
        # usually keeps a linked worktree's "behind" count honest, so take the
        # newer of the two stamps instead of reporting the worktree's as stale.
        stamps = [iso_mtime(os.path.join(gitdir, "FETCH_HEAD"))]
        if commondir and os.path.realpath(commondir) != os.path.realpath(gitdir):
            stamps.append(iso_mtime(os.path.join(commondir, "FETCH_HEAD")))
        rec["refs_fetched_at"] = max((stamp for stamp in stamps if stamp), default=None)
        rec["state"] = compute_state(rec)
    except subprocess.TimeoutExpired:
        rec["error"] = f"git timed out after {timeout}s"
        rec["state"] = "error"
    except (OSError, RuntimeError, ValueError) as exc:
        rec["error"] = str(exc)[:300]
        rec["state"] = "error"
    rec["_gitdir"] = gitdir
    rec["_commondir"] = commondir
    return rec


def worktree_paths(path: str, timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    try:
        cp = run_git(path, ["worktree", "list", "--porcelain"], timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    return [line[len("worktree ") :] for line in cp.stdout.splitlines() if line.startswith("worktree ")]


def _under(path: str, root: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def collect(root: str, workers: int = DEFAULT_WORKERS, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Scan the whole tree once and return the JSON payload every surface uses."""
    started = time.monotonic()
    root = os.path.abspath(os.path.expanduser(root))
    records: dict[str, dict] = {}

    candidates = discover(root) if os.path.isdir(root) else []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for rec in pool.map(lambda p: probe(p, timeout), candidates):
            records[rec["path"]] = rec

    # Group by physical repository so `git worktree list` can reveal checkouts
    # the walk missed (for example a linked worktree outside the depth window).
    groups: dict[str, list[dict]] = {}
    for rec in records.values():
        if rec["_commondir"]:
            groups.setdefault(rec["_commondir"], []).append(rec)

    external = 0
    missing: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        work = {}
        for commondir, members in groups.items():
            anchor = next(
                (m for m in members if m["_gitdir"] and os.path.realpath(m["_gitdir"]) == os.path.realpath(commondir)),
                members[0],
            )
            work[commondir] = pool.submit(worktree_paths, anchor["path"], timeout)
        # Resolve once: regenerating this set per worktree path turned a 1 s
        # scan into 4 s on a tree with this many worktrees.
        known = {os.path.realpath(rec["path"]) for rec in records.values()}
        for commondir, future in work.items():
            for path in future.result():
                resolved = os.path.realpath(path)
                if resolved in known:
                    continue
                if _under(path, root):
                    known.add(resolved)
                    missing.append(path)
                else:
                    external += 1

    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for rec in pool.map(lambda p: probe(p, timeout), sorted(set(missing))):
                records[rec["path"]] = rec

    # Authoritative main-repo path, and whether a checkout sits inside another.
    for commondir, members in groups.items():
        anchor = next(
            (m for m in members if m["_gitdir"] and os.path.realpath(m["_gitdir"]) == os.path.realpath(commondir)),
            None,
        )
        if anchor:
            for member in members:
                member["main_repo"] = anchor["path"]

    final = []
    paths = [rec["path"] for rec in records.values()]
    for rec in records.values():
        rec.pop("_gitdir", None)
        rec.pop("_commondir", None)
        rec["nested"] = any(
            other != rec["path"] and rec["path"].startswith(other + os.sep) for other in paths
        )
        final.append(rec)

    final.sort(key=lambda r: (STATE_RANK.get(r["state"], 99), r["path"]))
    return {
        "generated_at": iso_now(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "checkout_count": len(final),
        "external_worktrees": external,
        "records": final,
    }


# --------------------------------------------------------------------------
# upstream distance, and the only three writes
#
# The scan itself stays read-only. These actions are the sole writers, they are
# only reachable through an explicit request (a CLI subcommand, or a confirmed
# click in the dashboard/TUI), and every one re-probes the checkout first so a
# stale cache can never get a dirty repository rebased.

ACTIONS = ("fetch", "ff", "rebase")
ACTION_TIMEOUTS = {"fetch": 120, "ff": 60, "rebase": 300}


def preconditions(rec: dict, action: str) -> tuple[bool, str]:
    """Whether `action` is safe on this record, and why not when it is not."""
    if rec.get("error"):
        return False, f"checkout is broken: {rec['error']}"
    if action == "fetch":
        # Fetching only moves remote-tracking refs, so it is safe even on a
        # dirty or mid-rebase checkout.
        return True, ""
    if action not in ACTIONS:
        return False, f"unknown action {action}"
    if rec["conflicted"]:
        return False, "unresolved conflicts"
    if rec["operation"] != "none":
        return False, f"{rec['operation']} already in progress"
    if rec["staged"] or rec["unstaged"] or rec["untracked"]:
        return False, "working tree is dirty"
    if rec["detached"] or not rec["branch"]:
        return False, "detached HEAD"
    if not rec["upstream"]:
        return False, "no upstream configured"
    if action == "ff":
        if rec["ahead"]:
            return False, f"{rec['ahead']} local commit(s) are not upstream"
        if not rec["behind"]:
            return False, "already up to date"
        # A fresh clone has no FETCH_HEAD yet but a perfectly good upstream
        # ref, and --ff-only cannot lose a commit, so refs age is not a
        # precondition here -- it is only what the UI shows.
        return True, ""
    if action == "rebase":
        if not rec["ahead"] and not rec["behind"]:
            return False, "already up to date"
        return True, ""
    return False, "unreachable"


def commit_range(path: str, revision_range: str, timeout: int = DEFAULT_TIMEOUT, limit: int = 25) -> list[dict]:
    """Commits on one side of @{u}; the detail view's "how close am I"."""
    cp = run_git(path, ["log", f"-n{limit}", "--format=%h%x00%aI%x00%an%x00%s", revision_range], timeout)
    if cp.returncode != 0:
        return []
    commits = []
    for line in cp.stdout.splitlines():
        parts = line.split("\x00")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "iso_time": parts[1], "author": parts[2], "subject": parts[3]})
    return commits


def detail_of(path: str, rec: dict | None = None, limit: int = 25) -> dict:
    """One checkout with both sides of its upstream divergence and what may run."""
    rec = rec or probe(path)
    detail = {"record": rec, "incoming": [], "outgoing": [], "can": {}, "reasons": {}}
    if rec["upstream"] and not rec["error"]:
        detail["incoming"] = commit_range(path, "HEAD..@{u}", limit=limit)
        detail["outgoing"] = commit_range(path, "@{u}..HEAD", limit=limit)
    for action in ACTIONS:
        allowed, reason = preconditions(rec, action)
        detail["can"][action] = allowed
        detail["reasons"][action] = reason
    return detail


def perform_action(path: str, action: str, timeout: int | None = None) -> dict:
    """Run one of the three writes against a freshly probed checkout."""
    timeout = timeout or ACTION_TIMEOUTS.get(action, 120)
    result = {"ok": False, "action": action, "path": path, "output": "", "error": None}
    if action not in ACTIONS:
        result["error"] = f"unknown action {action}"
        return result
    rec = probe(path)
    allowed, reason = preconditions(rec, action)
    if not allowed:
        result["error"] = reason
        return result
    try:
        if action == "fetch":
            # Fetch the branch's own remote when there is one; otherwise every
            # remote, which is the only way to learn about an unset upstream.
            remote = (rec["upstream"] or "").split("/")[0] or None
            cp = run_git(path, ["fetch", "--quiet"] + ([remote] if remote else ["--all"]), timeout)
        elif action == "ff":
            cp = run_git(path, ["merge", "--ff-only", "@{u}"], timeout)
        else:
            # A rebase must never open an editor or ask a question.
            cp = run_git(
                path,
                ["rebase", "@{u}"],
                timeout,
                extra_env={"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"},
            )
    except subprocess.TimeoutExpired:
        result["error"] = f"git {action} timed out after {timeout}s"
        return result
    except OSError as exc:
        result["error"] = str(exc)
        return result
    result["output"] = (cp.stdout + cp.stderr).strip()[-4000:]
    result["ok"] = cp.returncode == 0
    if not result["ok"]:
        result["error"] = first_line(cp.stderr) or f"git {action} exited {cp.returncode}"
    return result


# --------------------------------------------------------------------------
# shared helpers


def filter_records(records: list[dict], states: list[str] | None, needle: str) -> list[dict]:
    out = []
    needle = (needle or "").lower()
    for rec in records:
        if states and rec["state"] not in states:
            continue
        if needle:
            haystack = " ".join(
                str(rec.get(key) or "") for key in ("name", "path", "branch", "upstream", "main_repo")
            ).lower()
            if needle not in haystack:
                continue
        out.append(rec)
    return out


def commit_ts(rec: dict) -> float:
    iso = (rec.get("last_commit") or {}).get("iso_time")
    if not iso:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def sort_records(records: list[dict], key: str) -> list[dict]:
    if key == "path":
        return sorted(records, key=lambda r: r["path"])
    if key == "recent":
        # Newest commit first; checkouts with no commit at all sink to the end.
        return sorted(records, key=lambda r: (-commit_ts(r), r["name"]))
    if key == "ahead":
        return sorted(records, key=lambda r: (-r["ahead"], r["path"]))
    if key == "behind":
        return sorted(records, key=lambda r: (-r["behind"], r["path"]))
    if key == "dirty":
        return sorted(records, key=lambda r: (-(r["staged"] + r["unstaged"] + r["untracked"]), r["path"]))
    return sorted(records, key=lambda r: (STATE_RANK.get(r["state"], 99), r["path"]))


def changes(rec: dict) -> str:
    total = rec["staged"] + rec["unstaged"] + rec["untracked"]
    return "—" if not total else f"{rec['staged']}/{rec['unstaged']}/{rec['untracked']}"


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = max(0.0, (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds())
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta / 60)}m ago"
    if delta < 172800:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def trunc(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text[:width]
    return text[: width - 1] + "…"


def fetch_service(url: str, timeout: float = 5.0, refresh: bool = False) -> dict:
    endpoint = url.rstrip("/") + "/api/repos" + ("?refresh=1" if refresh else "")
    with urllib.request.urlopen(endpoint, timeout=timeout) as response:
        return json.load(response)


def load(args, refresh: bool = False) -> tuple[dict, str]:
    """Prefer the running daemon (already warm); fall back to a local scan."""
    if not getattr(args, "local", False):
        try:
            return fetch_service(args.url, refresh=refresh), f"service {args.url}"
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            print(f"codedash: {args.url} unreachable ({reason}); scanning locally", file=sys.stderr)
    return collect(args.root, workers=getattr(args, "workers", DEFAULT_WORKERS)), "local scan"


# --------------------------------------------------------------------------
# web dashboard


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>codedash</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:13.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:20px 22px 6px;display:flex;flex-wrap:wrap;gap:12px;align-items:baseline}
h1{font-size:15px;margin:0;letter-spacing:.08em;text-transform:uppercase;color:#e6edf3}
.meta{color:#7d8590;font-size:12px}
.sum{display:flex;flex-wrap:wrap;gap:8px;padding:0 22px 12px}
.chip{border:1px solid #30363d;border-radius:999px;padding:2px 10px;font-size:12px;color:#8b949e}
.chip b{color:#e6edf3;font-weight:600}
.chip.alert{border-color:#5a1e1e;background:#2a1214;color:#ff7b72}
.chip.warn{border-color:#5a441e;background:#2a2112;color:#d29922}
.controls{display:flex;flex-wrap:wrap;gap:8px;padding:0 22px 10px;align-items:center}
input{background:#010409;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:5px 10px;font:inherit;min-width:280px}
input:focus{outline:1px solid #1f6feb;border-color:#1f6feb}
select{background:#010409;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:4px 8px;font:inherit;font-size:12px}
select:focus{outline:1px solid #1f6feb;border-color:#1f6feb}
.sort{color:#6e7681;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;display:inline-flex;align-items:center;gap:6px}
button{background:#161b22;border:1px solid #30363d;border-radius:6px;color:#8b949e;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer}
button:hover{border-color:#8b949e;color:#c9d1d9}
button.on{background:#1f6feb22;border-color:#1f6feb;color:#58a6ff}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;background:#0d1117;text-align:left;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:#6e7681;border-bottom:1px solid #30363d;padding:7px 10px;white-space:nowrap;z-index:1}
tbody td{padding:6px 10px;border-bottom:1px solid #161b22;vertical-align:top;white-space:nowrap}
tbody tr:hover{background:#161b22}
.name{color:#e6edf3}
.path{color:#6e7681;font-size:11px;display:block}
.tag{font-size:10px;border:1px solid #30363d;border-radius:4px;padding:0 4px;margin-left:6px;color:#7d8590}
.st{font-size:11px;border-radius:4px;padding:1px 7px;border:1px solid;white-space:nowrap}
.s-conflicted,.s-error{color:#ff7b72;border-color:#5a1e1e;background:#2a1214}
.s-operation,.s-dirty{color:#d29922;border-color:#5a441e;background:#2a2112}
.s-ahead{color:#58a6ff;border-color:#1f3a5f;background:#0d1b2a}
.s-behind{color:#bc8cff;border-color:#3c2a5e;background:#1b1226}
.s-no-upstream{color:#8b949e;border-color:#30363d}
.s-clean{color:#3fb950;border-color:#1b3a22;background:#0d1f13}
.num{font-variant-numeric:tabular-nums}
.dim{color:#6e7681}
.sub{color:#8b949e;display:block;max-width:460px;overflow:hidden;text-overflow:ellipsis}
.op{color:#d29922;font-size:11px;margin-left:6px}
footer{padding:10px 22px 28px;color:#6e7681;font-size:11px}
.modal{position:fixed;inset:0;background:#010409cc;display:none;align-items:flex-start;justify-content:center;padding:6vh 16px;overflow:auto;z-index:10}
.modal.open{display:flex}
.sheet{background:#0d1117;border:1px solid #30363d;border-radius:10px;max-width:860px;width:100%;padding:18px 20px;box-shadow:0 24px 70px #000c}
.sheet h2{font-size:14px;margin:0 0 3px;color:#e6edf3}
.kv{display:grid;grid-template-columns:96px 1fr;gap:3px 12px;font-size:12.5px;margin:12px 0}
.kv dt{color:#6e7681}
.kv dd{margin:0;color:#c9d1d9;word-break:break-all}
.acts{display:flex;gap:8px;margin:12px 0 6px;flex-wrap:wrap}
.acts button{border-color:#30363d}
.acts button:not(:disabled):hover{border-color:#1f6feb;color:#58a6ff}
.acts button:disabled{opacity:.4;cursor:not-allowed}
.reasons{color:#d29922;font-size:11px;margin:6px 0 12px;line-height:1.5}
.sec{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:#6e7681;margin:14px 0 4px}
.clist{margin:0;padding:0;list-style:none;font-size:12px}
.clist li{padding:1px 0;color:#8b949e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.clist code{color:#58a6ff;margin-right:8px}
.close{float:right;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#8b949e;cursor:pointer;padding:2px 8px}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#161b22;border:1px solid #30363d;border-radius:8px;padding:8px 14px;font-size:12px;color:#c9d1d9;opacity:0;pointer-events:none;transition:opacity .15s;max-width:82vw;z-index:20}
.toast.show{opacity:1}
tbody tr{cursor:pointer}
</style></head>
<body>
<header><h1>codedash</h1><span class="meta" id="meta">loading…</span></header>
<div class="sum" id="sum"></div>
<div class="controls">
  <input id="q" placeholder="filter by name, branch or path" autocomplete="off" spellcheck="false">
  <span id="filters"></span>
  <label class="sort">sort
    <select id="sort">
      <option value="recent" selected>last commit</option>
      <option value="state">state</option>
      <option value="name">name</option>
      <option value="behind">behind</option>
    </select>
  </label>
  <button id="refresh" title="ask the scanner to rescan now">rescan</button>
</div>
<table><thead><tr>
<th>state</th><th>repo</th><th>branch</th><th title="ahead / behind upstream">ahead/behind</th>
<th title="staged/unstaged/untracked">changes</th><th>last commit</th><th title="age of the remote-tracking refs">fetched</th>
</tr></thead><tbody id="rows"></tbody></table>
<footer id="foot"></footer>
<div class="modal" id="modal"><div class="sheet" id="sheet"></div></div>
<div class="toast" id="toast"></div>
<script>
let DATA = {records: []}, state = 'all', q = '', sortBy = 'recent';
const RANK = {error: 0, conflicted: 1, operation: 2, dirty: 3, ahead: 4, behind: 5, 'no-upstream': 6, clean: 7};
const commitTime = r => Date.parse((r.last_commit || {}).iso_time) || 0;
const FILTERS = ['all','dirty','ahead','behind','conflicted','operation','no-upstream','error'];
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ago = iso => {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (!t) return '?';
  const d = Math.max(0, (Date.now() - t) / 1000);
  if (d < 90) return Math.round(d) + 's ago';
  if (d < 5400) return Math.round(d / 60) + 'm ago';
  if (d < 172800) return Math.round(d / 3600) + 'h ago';
  return Math.round(d / 86400) + 'd ago';
};
function summarise(rs) {
  const c = {total: rs.length, dirty: 0, ahead: 0, behind: 0, conflicted: 0, operation: 0, no_upstream: 0, error: 0, worktrees: 0, stashes: 0};
  for (const r of rs) {
    if (r.kind === 'worktree') c.worktrees++;
    c.stashes += r.stashes || 0;
    if (r.error) c.error++;
    if (r.conflicted) c.conflicted++;
    if (r.operation !== 'none') c.operation++;
    if (r.ahead) c.ahead++;
    if (r.behind) c.behind++;
    if (!r.upstream) c.no_upstream++;
    if (r.state === 'dirty') c.dirty++;
  }
  return c;
}
function visible() {
  const needle = q.toLowerCase();
  const out = DATA.records.filter(r => (state === 'all' || r.state === state) &&
    (!needle || (r.name + ' ' + (r.branch || '') + ' ' + r.path).toLowerCase().includes(needle)));
  const byName = (a, b) => a.name.localeCompare(b.name) || a.path.localeCompare(b.path);
  if (sortBy === 'recent') out.sort((a, b) => commitTime(b) - commitTime(a) || byName(a, b));
  else if (sortBy === 'state') out.sort((a, b) => (RANK[a.state] ?? 9) - (RANK[b.state] ?? 9) || byName(a, b));
  else if (sortBy === 'behind') out.sort((a, b) => b.behind - a.behind || byName(a, b));
  else out.sort(byName);
  return out;
}
function chip(label, value, cls) {
  return '<span class="chip ' + (cls || '') + '"><b>' + value + '</b> ' + label + '</span>';
}
function render() {
  const c = summarise(DATA.records);
  document.getElementById('meta').textContent =
    c.total + ' checkouts · ' + c.worktrees + ' worktrees · scanned ' + ago(DATA.generated_at) +
    ' · ' + (DATA.duration_ms || 0) + ' ms';
  document.getElementById('sum').innerHTML =
    chip('dirty', c.dirty, c.dirty ? 'warn' : '') +
    chip('conflicted', c.conflicted, c.conflicted ? 'alert' : '') +
    chip('mid-operation', c.operation, c.operation ? 'warn' : '') +
    chip('ahead', c.ahead) + chip('behind', c.behind) +
    chip('no upstream', c.no_upstream) + chip('stashes', c.stashes) +
    chip('errors', c.error, c.error ? 'alert' : '');
  document.getElementById('filters').innerHTML = FILTERS.map(f =>
    '<button data-f="' + f + '" class="' + (state === f ? 'on' : '') + '">' + f +
    (f !== 'all' && c[f.replace('-', '_')] !== undefined && c[f.replace('-', '_')] ? ' ' + c[f.replace('-', '_')] : '') +
    '</button>').join(' ');
  const rows = visible().map(r => {
    const op = r.operation && r.operation !== 'none' ? '<span class="op">' + esc(r.operation) + '</span>' : '';
    const tag = r.kind === 'worktree' ? '<span class="tag">worktree</span>' : '';
    const nested = r.nested ? '<span class="tag">nested</span>' : '';
    const branch = r.detached ? '<span class="dim">detached</span>' : esc(r.branch || '?');
    const up = r.upstream ? '<span class="path">' + esc(r.upstream) + '</span>' : '<span class="path">none</span>';
    const commit = r.last_commit || {};
    return '<tr data-path="' + esc(r.path) + '">' +
      '<td><span class="st s-' + esc(r.state) + '">' + esc(r.state) + '</span>' + op + '</td>' +
      '<td><span class="name">' + esc(r.name) + '</span>' + tag + nested +
        '<span class="path">' + esc(r.path) + '</span></td>' +
      '<td>' + branch + up + '</td>' +
      '<td class="num">↑' + r.ahead + ' ↓' + r.behind + '</td>' +
      '<td class="num">' + esc(changes(r)) + (r.conflicted ? ' <span class="st s-conflicted">conflict</span>' : '') + '</td>' +
      '<td><span class="sub" title="' + esc(commit.subject || '') + '">' + esc(commit.subject || '—') + '</span>' +
        '<span class="path">' + esc(commit.author || '') + (commit.iso_time ? ' · ' + ago(commit.iso_time) : '') + '</span></td>' +
      '<td class="path">' + ago(r.refs_fetched_at) + '</td>' +
      '</tr>';
  }).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="7" class="dim" style="padding:18px 10px">nothing matches</td></tr>';
  document.getElementById('foot').textContent =
    'ahead/behind is measured against remote-tracking refs; nothing here fetches, so check the “fetched” column. ' +
    'read-only: the scanner never writes to a repository.';
}
function changes(r) {
  const total = (r.staged || 0) + (r.unstaged || 0) + (r.untracked || 0);
  return total ? r.staged + '/' + r.unstaged + '/' + r.untracked : '—';
}
let CUR = null, BUSY = false;
function toast(text) {
  const el = document.getElementById('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 6000);
}
function closeModal() {
  document.getElementById('modal').classList.remove('open');
  CUR = null;
}
async function openDetail(path) {
  const res = await fetch('api/detail?path=' + encodeURIComponent(path), {cache: 'no-store'});
  if (!res.ok) { toast('detail failed: HTTP ' + res.status); return; }
  CUR = await res.json();
  renderModal();
}
function commitList(items) {
  if (!items || !items.length) return '<div class="dim" style="font-size:12px">none</div>';
  return '<ul class="clist">' + items.map(c =>
    '<li><code>' + esc(c.sha) + '</code>' + esc(c.subject) + '</li>').join('') + '</ul>';
}
function renderModal() {
  const d = CUR.record, can = CUR.can || {}, why = CUR.reasons || {};
  const button = (action, label) =>
    '<button data-act="' + action + '"' + (can[action] ? '' : ' disabled title="' + esc(why[action] || '') + '"') + '>' +
    label + '</button>';
  const blockers = Object.keys(can).filter(k => !can[k]).map(k => k + ': ' + esc(why[k]));
  document.getElementById('sheet').innerHTML =
    '<button class="close" data-close>close</button>' +
    '<h2>' + esc(d.name) + ' <span class="tag">' + esc(d.kind) + '</span></h2>' +
    '<div class="meta">' + esc(d.path) + '</div>' +
    '<dl class="kv">' +
      '<dt>state</dt><dd><span class="st s-' + esc(d.state) + '">' + esc(d.state) + '</span>' +
        (d.operation !== 'none' ? ' <span class="op">' + esc(d.operation) + '</span>' : '') + '</dd>' +
      '<dt>branch</dt><dd>' + (d.detached ? 'detached' : esc(d.branch || '?')) + ' @ ' + esc(d.head || '?') + '</dd>' +
      '<dt>upstream</dt><dd>' + esc(d.upstream || 'none') + ' · ↑' + d.ahead + ' ↓' + d.behind +
        ' · refs ' + ago(d.refs_fetched_at) + '</dd>' +
      '<dt>changes</dt><dd>' + d.staged + ' staged · ' + d.unstaged + ' unstaged · ' + d.untracked + ' untracked' +
        (d.conflicted ? ' · ' + d.conflicted + ' conflicted' : '') + '</dd>' +
      '<dt>last</dt><dd>' + esc((d.last_commit && d.last_commit.subject) || '—') + '</dd>' +
    '</dl>' +
    '<div class="acts">' + button('fetch', 'fetch') + button('ff', 'fast-forward') + button('rebase', 'rebase') + '</div>' +
    (blockers.length ? '<div class="reasons">' + blockers.join(' · ') + '</div>' : '') +
    '<div class="sec">incoming — ' + d.behind + ' behind ' + esc(d.upstream || '') + '</div>' + commitList(CUR.incoming) +
    '<div class="sec">outgoing — ' + d.ahead + ' ahead</div>' + commitList(CUR.outgoing);
  document.getElementById('modal').classList.add('open');
}
async function runAction(action) {
  if (!CUR || BUSY) return;
  const d = CUR.record;
  if (action !== 'fetch' && !confirm(action + ' ' + d.name + ' onto ' + (d.upstream || 'upstream') + '?')) return;
  BUSY = true;
  try {
    const res = await fetch('api/action', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({path: d.path, action: action})
    });
    let body;
    try { body = await res.json(); } catch (e) { toast('action failed: HTTP ' + res.status); return; }
    if (body.payload) { DATA = body.payload; render(); }
    if (body.result) {
      toast(body.result.ok ? action + ' ok · ' + d.name : action + ' failed · ' + (body.result.error || 'git error'));
    } else {
      toast(body.error || 'action failed');
    }
    if (CUR) await openDetail(d.path);
  } finally {
    BUSY = false;
  }
}
document.getElementById('modal').addEventListener('click', e => {
  if (e.target.id === 'modal') return closeModal();
  const action = e.target.getAttribute('data-act');
  if (action) return runAction(action);
  if (e.target.hasAttribute('data-close')) return closeModal();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
document.getElementById('rows').addEventListener('click', e => {
  const row = e.target.closest('tr');
  if (row && row.dataset.path) openDetail(row.dataset.path);
});
async function load(refresh) {
  try {
    const res = await fetch('api/repos' + (refresh ? '?refresh=1' : ''), {cache: 'no-store'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    DATA = await res.json();
  } catch (err) {
    document.getElementById('meta').textContent = 'scanner unavailable: ' + err.message;
  }
  render();
}
document.getElementById('q').addEventListener('input', e => { q = e.target.value; render(); });
document.getElementById('sort').addEventListener('change', e => { sortBy = e.target.value; render(); });
document.getElementById('filters').addEventListener('click', e => {
  const f = e.target.getAttribute('data-f');
  if (f) { state = f; render(); }
});
document.getElementById('refresh').addEventListener('click', () => load(true));
load(false);
setInterval(() => load(false), 20000);
</script>
</body></html>
"""


class Store:
    """One scan at a time, served stale-while-refreshing."""

    def __init__(self, root: str, ttl: int = CACHE_TTL, state_dir: str | None = None, workers: int = DEFAULT_WORKERS):
        self.root = root
        self.ttl = ttl
        self.state_dir = state_dir
        self.workers = workers
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        # One write at a time; two rebases in one checkout would be carnage.
        self.action_lock = threading.Lock()
        self._data: dict = {"generated_at": None, "duration_ms": 0, "checkout_count": 0, "external_worktrees": 0, "records": []}
        self._at = 0.0

    def count(self) -> int:
        with self._lock:
            return len(self._data.get("records", []))

    def snapshot(self) -> dict:
        with self._lock:
            return self._data

    def warm(self) -> None:
        self._scan()

    def _scan(self) -> None:
        payload = collect(self.root, workers=self.workers)
        with self._lock:
            self._data = payload
            self._at = time.monotonic()
        self._persist(payload)

    def _persist(self, payload: dict) -> None:
        if not self.state_dir:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = os.path.join(self.state_dir, ".status.json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, os.path.join(self.state_dir, "status.json"))
        except OSError as exc:
            # Persistence is a convenience for debugging; losing it must not
            # take down the dashboard.
            print(f"codedash: cannot write state dir {self.state_dir}: {exc}", file=sys.stderr)

    def _safe_scan(self) -> None:
        try:
            self._scan()
        except Exception as exc:  # noqa: BLE001 - a failed scan must not kill the daemon
            print(f"codedash: scan failed: {exc}", file=sys.stderr)

    def _background(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return

        def run() -> None:
            try:
                self._safe_scan()
            finally:
                self._scan_lock.release()

        threading.Thread(target=run, daemon=True).start()

    def force_scan(self) -> None:
        """Scan now, waiting for any in-flight scan rather than queueing."""
        if self._scan_lock.acquire(timeout=30):
            try:
                self._safe_scan()
            finally:
                self._scan_lock.release()

    def data(self, refresh: bool = False) -> dict:
        if refresh:
            # An explicit refresh is a person pressing a button: block for the
            # scan (about a second) so the answer is actually fresh. Timer and
            # TTL rescans stay in the background so requests never wait on them.
            self.force_scan()
        elif (time.monotonic() - self._at) > self.ttl:
            self._background()
        return self.snapshot()

    def loop(self, interval: int) -> None:
        while True:
            time.sleep(interval)
            self._background()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codedash/" + VERSION

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parts = urllib.parse.urlsplit(self.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        # Mounted behind nginx at BASE_PATH; also usable directly on loopback.
        if path == BASE_PATH:
            return self._send(301, "text/plain; charset=utf-8", b"", {"Location": BASE_PATH + "/"})
        if path.startswith(BASE_PATH + "/"):
            path = path[len(BASE_PATH) :]

        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        if path == "/api/repos":
            refresh = query.get("refresh", ["0"])[0].lower() not in ("", "0", "false", "no")
            payload = self.server.store.data(refresh)  # type: ignore[attr-defined]
            return self._json(payload)
        if path == "/api/detail":
            target = (query.get("path") or [""])[0]
            rec = self._record(target)
            if rec is None:
                return self._json({"error": f"unknown checkout: {target}"}, status=404)
            return self._json(detail_of(rec["path"]))
        if path == "/healthz":
            return self._send(200, "text/plain; charset=utf-8", b"ok\n")
        if path == "/favicon.ico":
            return self._send(204, "text/plain; charset=utf-8", b"")
        return self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith(BASE_PATH + "/"):
            path = path[len(BASE_PATH) :]
        if path != "/api/action":
            return self._send(404, "text/plain; charset=utf-8", b"not found\n")
        if not self.server.allow_actions:  # type: ignore[attr-defined]
            return self._json(
                {"error": "actions are disabled on this daemon; start it with --allow-actions"},
                status=403,
            )
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            return self._json({"error": "bad request body"}, status=400)
        try:
            body = json.loads(self.rfile.read(length).decode())
            action = str(body.get("action", ""))
            target = str(body.get("path", ""))
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "bad json"}, status=400)
        if action not in ACTIONS:
            return self._json({"error": f"unknown action {action}"}, status=400)
        rec = self._record(target)
        if rec is None:
            # Only paths the scanner already knows may be written to; this is
            # what keeps a tailnet request from running git in /etc.
            return self._json({"error": f"unknown checkout: {target}"}, status=404)
        store = self.server.store  # type: ignore[attr-defined]
        with store.action_lock:
            result = perform_action(rec["path"], action)
            store.force_scan()
        return self._json({"result": result, "payload": store.snapshot()})

    def _record(self, target: str) -> dict | None:
        if not target:
            return None
        wanted = os.path.realpath(target)
        for rec in self.server.store.snapshot()["records"]:  # type: ignore[attr-defined]
            if os.path.realpath(rec["path"]) == wanted:
                return rec
        return None

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode())

    def _send(self, code: int, ctype: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store: Store, allow_actions: bool = False):
        super().__init__(address, Handler)
        self.store = store
        self.allow_actions = allow_actions


# --------------------------------------------------------------------------
# CLI


def print_summary(records: list[dict], source: str, duration_ms: int) -> None:
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec["state"]] = counts.get(rec["state"], 0) + 1
    parts = [f"{count} {state}" for state, count in sorted(counts.items(), key=lambda kv: STATE_RANK.get(kv[0], 99))]
    print(f"# {source} · {len(records)} checkouts · {duration_ms} ms · " + (" · ".join(parts) or "nothing"))


def print_table(records: list[dict], color: bool) -> None:
    header = ["STATE", "REPO", "BRANCH", "↑/↓", "CHANGES", "LAST"]
    rows = []
    for rec in records:
        branch = "detached" if rec["detached"] else (rec["branch"] or "?")
        last = rec["last_commit"] or {}
        last_text = f"{ago(last.get('iso_time'))} {last.get('author', '')}".strip() if last else "—"
        rows.append(
            [
                rec["state"],
                trunc(rec["name"] + (" ↳" if rec["kind"] == "worktree" else ""), 34),
                trunc(branch, 26),
                f"{rec['ahead']}/{rec['behind']}",
                changes(rec),
                trunc(last_text, 30),
            ]
        )
    widths = [len(h) for h in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    widths = [min(width, cap) for width, cap in zip(widths, (9, 34, 26, 7, 9, 30))]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip())
    for row in rows:
        cells = [cell.ljust(w)[:w] for cell, w in zip(row, widths)]
        if color:
            cells[0] = ANSI.get(row[0], "") + cells[0] + RESET
        print("  ".join(cells).rstrip())


def cmd_list(args) -> int:
    payload, source = load(args)
    records = filter_records(payload["records"], args.state, args.grep)
    records = sort_records(records, args.sort)
    if args.json:
        print(json.dumps({**payload, "checkout_count": len(records), "records": records}, ensure_ascii=False, indent=2))
    else:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print_table(records, color)
        if sys.stdout.isatty():
            print_summary(records, source, payload.get("duration_ms", 0))
        if not records:
            print("nothing matches", file=sys.stderr)
    if args.exit_code and any(rec["state"] in ("error", "conflicted", "operation") for rec in records):
        return 1
    return 0


def resolve_target(records: list[dict], target: str) -> tuple[dict | None, list[dict]]:
    """Fuzzy-match one checkout: exact name/path first, then substring."""
    wanted = target.strip().rstrip("/")
    exact = [rec for rec in records if rec["name"] == wanted or rec["path"] == wanted]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact
    needle = wanted.lower()
    matches = [rec for rec in records if needle in rec["name"].lower() or needle in rec["path"].lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def report_ambiguous(target: str, matches: list[dict]) -> int:
    if not matches:
        print(f"codedash: no checkout matches {target!r}", file=sys.stderr)
        return 2
    print(f"codedash: {target!r} matches {len(matches)} checkouts:", file=sys.stderr)
    for rec in matches[:20]:
        print(f"  {rec['name']:<34} {rec['path']}", file=sys.stderr)
    if len(matches) > 20:
        print(f"  … {len(matches) - 20} more", file=sys.stderr)
    return 2


def print_detail(detail: dict) -> None:
    rec = detail["record"]
    commit = rec["last_commit"] or {}
    print(f"{rec['name']}  ({rec['kind']})  {rec['path']}")
    print(f"  state      {rec['state']}" + (f"  [{rec['operation']}]" if rec["operation"] != "none" else ""))
    print(f"  branch     {'detached' if rec['detached'] else (rec['branch'] or '?')} @ {rec['head'] or '?'}")
    print(
        f"  upstream   {rec['upstream'] or 'none'}  ↑{rec['ahead']} ↓{rec['behind']}"
        f"  (refs {ago(rec['refs_fetched_at'])})"
    )
    print(f"  changes    {rec['staged']}/{rec['unstaged']}/{rec['untracked']}  stashes {rec['stashes']}")
    print(f"  last       {commit.get('sha', '—')}  {commit.get('subject', '—')}  ({ago(commit.get('iso_time'))})")
    if detail["incoming"]:
        print(f"  incoming   {len(detail['incoming'])} of {rec['behind']} behind:")
        for item in detail["incoming"]:
            print(f"    {item['sha']}  {item['iso_time'][:10]}  {trunc(item['subject'], 68)}")
    if detail["outgoing"]:
        print(f"  outgoing   {len(detail['outgoing'])} of {rec['ahead']} ahead:")
        for item in detail["outgoing"]:
            print(f"    {item['sha']}  {item['iso_time'][:10]}  {trunc(item['subject'], 68)}")
    actions = "  ".join(
        f"{name}={'yes' if detail['can'][name] else 'no (' + detail['reasons'][name] + ')'}" for name in ACTIONS
    )
    print(f"  actions    {actions}")


def cmd_detail(args) -> int:
    payload, _source = load(args)
    rec, matches = resolve_target(payload["records"], args.target)
    if rec is None:
        return report_ambiguous(args.target, matches)
    detail = detail_of(rec["path"])
    if args.json:
        print(json.dumps(detail, ensure_ascii=False, indent=2))
    else:
        print_detail(detail)
    return 0


def cmd_action(args) -> int:
    payload, _source = load(args)
    rec, matches = resolve_target(payload["records"], args.target)
    if rec is None:
        return report_ambiguous(args.target, matches)
    if not args.quiet:
        print(f"codedash: {args.action} {rec['name']}", file=sys.stderr)
    result = perform_action(rec["path"], args.action)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        print(f"codedash: {args.action} failed: {result['error']}", file=sys.stderr)
        if result["output"]:
            print(result["output"], file=sys.stderr)
        return 1
    if not args.json:
        fresh = probe(rec["path"])
        print(f"{args.action} ok · {fresh['branch']} ↑{fresh['ahead']} ↓{fresh['behind']} · {fresh['state']}")
        if result["output"]:
            print(result["output"])
    # Keep the daemon's cache from serving pre-action state to the dashboard.
    if not getattr(args, "local", False):
        try:
            fetch_service(args.url, refresh=True, timeout=20)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            pass
    return 0


def cmd_watch(args) -> int:
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    try:
        while True:
            payload, source = load(args)
            records = sort_records(filter_records(payload["records"], args.state, args.grep), args.sort)
            print("\033[2J\033[H", end="")
            print_summary(payload["records"], source, payload.get("duration_ms", 0))
            print_table(records, color)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_scan(args) -> int:
    payload = collect(args.root, workers=args.workers)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args) -> int:
    store = Store(args.root, ttl=args.ttl, state_dir=args.state_dir, workers=args.workers)
    print(f"codedash: scanning {args.root}", file=sys.stderr, flush=True)
    store.warm()
    try:
        server = Server(("127.0.0.1", args.port), store, allow_actions=args.allow_actions)
    except OSError as exc:
        print(f"codedash: cannot bind 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 2
    threading.Thread(target=store.loop, args=(args.interval,), daemon=True).start()
    print(
        f"codedash: {store.count()} checkouts on http://127.0.0.1:{args.port} "
        f"(mounted at {BASE_PATH}, rescans every {args.interval}s, "
        f"actions {'enabled' if args.allow_actions else 'disabled'})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


# --------------------------------------------------------------------------
# TUI

TUI_FILTERS = ["all", "dirty", "ahead", "behind", "conflicted", "operation", "no-upstream", "error"]
TUI_KEYS = "↑↓ move · g/G ends · / filter · f state · r rescan · Enter detail · q quit"


class Tui:
    def __init__(self, stdscr, args, payload: dict, source: str):
        self.stdscr = stdscr
        self.args = args
        self.payload = payload
        self.source = source
        self.filter = "all"
        self.search = ""
        self.cursor = 0
        self.offset = 0

    # -- data

    def records(self) -> list[dict]:
        states = None if self.filter == "all" else [self.filter]
        return sort_records(filter_records(self.payload["records"], states, self.search), "state")

    def reload(self) -> None:
        self.draw_message("rescanning…")
        self.payload, self.source = load(self.args, refresh=True)
        self.cursor = min(self.cursor, max(0, len(self.records()) - 1))

    # -- drawing

    def safe_add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            self.stdscr.addnstr(y, x, text, max(0, self.stdscr.getmaxyx()[1] - x - 1), attr)
        except curses.error:
            pass

    def draw_message(self, text: str) -> None:
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()
        self.safe_add(height // 2, max(0, (width - len(text)) // 2), text)
        self.stdscr.refresh()

    def draw(self) -> None:
        records = self.records()
        height, width = self.stdscr.getmaxyx()
        self.stdscr.erase()

        generated = ago(self.payload.get("generated_at"))
        self.safe_add(0, 1, f"codedash — {self.source} · {len(self.payload['records'])} checkouts · scanned {generated}", curses.A_BOLD)
        counts: dict[str, int] = {}
        for rec in self.payload["records"]:
            counts[rec["state"]] = counts.get(rec["state"], 0) + 1
        summary = "  ".join(f"{value} {key}" for key, value in sorted(counts.items(), key=lambda kv: STATE_RANK.get(kv[0], 99)))
        self.safe_add(1, 1, summary[: width - 2], curses.A_DIM)
        self.safe_add(2, 1, f"filter: {self.filter}   search: {self.search or '—'}", curses.A_DIM)

        header = "STATE       REPO                                BRANCH                      ↑/↓    CHANGES   LAST"
        self.safe_add(4, 1, header[: width - 2], curses.A_UNDERLINE)

        body_top, body_bottom = 5, height - 2
        visible = max(0, body_bottom - body_top)
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + visible:
            self.offset = self.cursor - visible + 1

        if not records:
            self.safe_add(body_top, 2, "nothing matches")
        for index, rec in enumerate(records[self.offset : self.offset + visible]):
            row = body_top + index
            branch = "detached" if rec["detached"] else (rec["branch"] or "?")
            marker = "↳ " if rec["kind"] == "worktree" else ""
            last = rec["last_commit"] or {}
            line = (
                f"{rec['state'][:10]:<11} {trunc(marker + rec['name'], 34):<34}  "
                f"{trunc(branch, 24):<24}  {rec['ahead']:>2}/{rec['behind']:<3} "
                f"{changes(rec):<9} {ago(last.get('iso_time')) if last else '—'}"
            )
            attr = curses.A_REVERSE if self.offset + index == self.cursor else 0
            if not attr and curses.has_colors():
                attr = {
                    "error": curses.color_pair(2),
                    "conflicted": curses.color_pair(2),
                    "operation": curses.color_pair(3),
                    "dirty": curses.color_pair(3),
                    "ahead": curses.color_pair(4),
                    "behind": curses.color_pair(5),
                    "clean": curses.color_pair(6),
                }.get(rec["state"], 0)
            self.safe_add(row, 1, line[: width - 2], attr)

        footer = f"{len(records)} shown · {TUI_KEYS}"
        self.safe_add(height - 1, 1, trunc(footer, width - 2), curses.A_DIM)
        self.stdscr.refresh()

    def detail(self) -> None:
        records = self.records()
        if not records:
            return
        # Fresh probe: the point of opening one checkout is to see it as it is
        # now, and to act on that, not on a cache from a minute ago.
        detail = detail_of(records[self.cursor]["path"])
        rec = detail["record"]
        commit = rec["last_commit"] or {}
        head = [
            f"{rec['name']}  ({rec['kind']})",
            f"path        {rec['path']}",
            f"main repo   {rec['main_repo']}" + ("   [nested]" if rec["nested"] else ""),
            f"state       {rec['state']}" + (f"   operation: {rec['operation']}" if rec["operation"] != "none" else ""),
            f"branch      {'detached' if rec['detached'] else (rec['branch'] or '?')}  @ {rec['head'] or '?'}",
            f"upstream    {rec['upstream'] or 'none'}   ↑{rec['ahead']} ↓{rec['behind']}   refs {ago(rec['refs_fetched_at'])}",
            f"changes     staged {rec['staged']}  unstaged {rec['unstaged']}  untracked {rec['untracked']}  conflicted {rec['conflicted']}",
            f"stashes     {rec['stashes']}",
            f"last commit {commit.get('sha', '—')}  {commit.get('author', '')}  {ago(commit.get('iso_time'))}",
            f"            {commit.get('subject', '—')}",
            "",
        ]
        if rec["error"]:
            head.append(f"error       {rec['error']}")
        hints = []
        for action, key, label in (("fetch", "f", "fetch"), ("ff", "F", "fast-forward"), ("rebase", "R", "rebase")):
            if detail["can"][action]:
                hints.append(f"[{key}] {label}")
            else:
                hints.append(f"[{key}] {label} — {detail['reasons'][action]}")
        head.append("  ".join(hints))
        head.append("")

        body = []
        if rec["upstream"]:
            body.append(f"incoming    {rec['behind']} behind {rec['upstream']}")
            body += [f"  {item['sha']}  {trunc(item['subject'], 66)}" for item in detail["incoming"][:6]]
            body.append(f"outgoing    {rec['ahead']} ahead")
            body += [f"  {item['sha']}  {trunc(item['subject'], 66)}" for item in detail["outgoing"][:6]]
        else:
            body.append("incoming    — no upstream configured")
        body.append("")
        body.append("q closes   ·   actions ask for confirmation")

        while True:
            height, width = self.stdscr.getmaxyx()
            self.stdscr.erase()
            lines = head + body
            for index, line in enumerate(lines[: height - 1]):
                self.safe_add(1 + index, 2, trunc(line, width - 4))
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return
            if key == ord("f"):
                self.act(rec, "fetch")
                return
            if key == ord("F") and detail["can"]["ff"]:
                self.act(rec, "ff")
                return
            if key == ord("R") and detail["can"]["rebase"]:
                self.act(rec, "rebase")
                return

    def act(self, rec: dict, action: str) -> None:
        if action != "fetch":
            answer = self.prompt(f"{action} {rec['name']}? [y/N]")
            if answer.strip().lower() != "y":
                return
        self.draw_message(f"{action}…")
        result = perform_action(rec["path"], action)
        if result["ok"]:
            self.draw_message(f"{action} ok")
        else:
            self.draw_message(f"{action} failed: {trunc(result['error'] or 'git failed', 60)}")
        self.stdscr.getch()
        self.reload()

    def prompt(self, label: str) -> str:
        height, width = self.stdscr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        try:
            self.stdscr.addstr(height - 1, 1, label + " " * max(0, width - len(label) - 2))
            self.stdscr.refresh()
            raw = self.stdscr.getstr(height - 1, 1 + len(label), 60)
            return raw.decode(errors="replace")
        except curses.error:
            return ""
        finally:
            curses.noecho()
            curses.curs_set(0)

    def run(self) -> None:
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_BLUE, -1)
            curses.init_pair(5, curses.COLOR_MAGENTA, -1)
            curses.init_pair(6, curses.COLOR_GREEN, -1)
        while True:
            self.draw()
            key = self.stdscr.getch()
            records = self.records()
            total = len(records)
            if key in (ord("q"), 27):
                return
            if key in (curses.KEY_UP, ord("k")):
                self.cursor = max(0, self.cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.cursor = min(max(0, total - 1), self.cursor + 1)
            elif key in (curses.KEY_HOME, ord("g")):
                self.cursor = 0
            elif key in (curses.KEY_END, ord("G")):
                self.cursor = max(0, total - 1)
            elif key == curses.KEY_PPAGE:
                self.cursor = max(0, self.cursor - max(1, self.stdscr.getmaxyx()[0] - 8))
            elif key == curses.KEY_NPAGE:
                self.cursor = min(max(0, total - 1), self.cursor + max(1, self.stdscr.getmaxyx()[0] - 8))
            elif key == ord("/"):
                self.search = self.prompt("/")
                self.cursor = 0
            elif key == ord("f"):
                index = TUI_FILTERS.index(self.filter)
                self.filter = TUI_FILTERS[(index + 1) % len(TUI_FILTERS)]
                self.cursor = 0
            elif key == ord("r"):
                self.reload()
            elif key in (curses.KEY_ENTER, 10, 13):
                self.detail()


def cmd_tui(args) -> int:
    if not sys.stdout.isatty():
        print("codedash tui: stdout is not a TTY", file=sys.stderr)
        return 2
    payload, source = load(args)
    curses.wrapper(lambda screen: Tui(screen, args, payload, source).run())
    return 0


# --------------------------------------------------------------------------
# entry point


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=os.environ.get("CODEDASH_ROOT", DEFAULT_ROOT), help="directory holding the checkouts")
    common.add_argument("--url", default=os.environ.get("CODEDASH_URL", DEFAULT_URL), help="daemon base URL")
    common.add_argument("--local", action="store_true", help="never contact the daemon; always scan locally")

    parser = argparse.ArgumentParser(prog="codedash", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"codedash {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", parents=[common], help="run the HTTP dashboard and JSON API")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="where to keep the last payload")
    serve.add_argument("--interval", type=int, default=SCAN_INTERVAL, help="seconds between background rescans")
    serve.add_argument("--ttl", type=int, default=CACHE_TTL, help="seconds before a request triggers a rescan")
    serve.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    serve.add_argument(
        "--allow-actions",
        action="store_true",
        help="enable fetch / fast-forward / rebase over HTTP (off by default: they write to repositories)",
    )
    serve.set_defaults(func=cmd_serve)

    listing = sub.add_parser("list", parents=[common], help="print the status table")
    listing.add_argument("--state", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    listing.add_argument("--grep", default="", help="substring over name, path, branch, upstream")
    listing.add_argument("--sort", choices=["state", "recent", "path", "ahead", "behind", "dirty"], default="state")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--exit-code", action="store_true", help="nonzero when anything needs attention")
    listing.set_defaults(func=cmd_list)

    watch = sub.add_parser("watch", parents=[common], help="redraw the table on an interval")
    watch.add_argument("interval", nargs="?", type=int, default=5)
    watch.add_argument("--state", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    watch.add_argument("--grep", default="")
    watch.add_argument("--sort", choices=["state", "recent", "path", "ahead", "behind", "dirty"], default="state")
    watch.set_defaults(func=cmd_watch)

    scan = sub.add_parser("scan", parents=[common], help="scan once and print JSON")
    scan.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    scan.set_defaults(func=cmd_scan)

    detail = sub.add_parser("detail", parents=[common], help="upstream distance and available actions for one checkout")
    detail.add_argument("target", help="checkout name, or any fragment of its path")
    detail.add_argument("--json", action="store_true")
    detail.set_defaults(func=cmd_detail)

    for action, helptext in (
        ("fetch", "update remote-tracking refs for one checkout"),
        ("ff", "fast-forward one checkout to its upstream"),
        ("rebase", "replay one checkout's local commits onto its upstream"),
    ):
        parser_action = sub.add_parser(action, parents=[common], help=helptext)
        parser_action.add_argument("target", help="checkout name, or any fragment of its path")
        parser_action.add_argument("--json", action="store_true")
        parser_action.add_argument("--quiet", action="store_true")
        parser_action.set_defaults(func=cmd_action, action=action)

    tui = sub.add_parser("tui", parents=[common], help="interactive terminal view")
    tui.set_defaults(func=cmd_tui)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
