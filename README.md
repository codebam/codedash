# codedash

A small, dependency-free service that reports the state of every git checkout
under a directory — branch, staged/unstaged/untracked counts, stashes, how far
ahead or behind its upstream it is, and how stale the remote-tracking refs are
— with fetch, fast-forward and rebase for the checkouts that need them.

One scanner feeds three surfaces, so they cannot disagree:

| surface | command | notes |
| --- | --- | --- |
| dashboard | `codedash serve` | HTML + JSON on loopback, meant to sit behind a reverse proxy |
| CLI | `codedash list` | table, `--json`, filters, `--exit-code` for scripts |
| TUI | `codedash tui` | curses, keyboard navigation, actions from the detail panel |

The dashboard orders by last commit, newest first; the sort menu switches
to state, name or behind, and `codedash list --sort recent` gives the same
order in the terminal.

The scan itself never writes: `status` runs with `--no-optional-locks` and
every git call gets `GIT_OPTIONAL_LOCKS=0`, so scanning 200 checkouts does not
rewrite 200 index files.

## Why it is not `for d in */; do git status; done`

`~/Documents/git` is not flat. Roughly half the checkouts are linked worktrees
whose `.git` is a *file* pointing back into a main repository, repositories
nest inside other repositories, and some worktrees live deeper than a naive
walk goes. codedash keys a checkout by its absolute per-worktree git dir and
groups checkouts by their common dir, then asks each repository
`git worktree list --porcelain` so worktrees outside the walked depth still
appear. The detail view tells you which commits you are behind and ahead.

Nothing is fetched during a scan, so `behind` is only as fresh as the last
`git fetch`. That is what `refs_fetched_at` is for: the dashboard shows
"fetched 3d ago" instead of pretending the count is news.

## Run it

Standalone, no packaging needed:

```console
$ ./codedash.py serve --root ~/Documents/git --port 8090 --allow-actions
$ ./codedash.py list --state dirty
$ ./codedash.py tui
$ ./codedash.py detail some-repo
$ ./codedash.py fetch some-repo
$ ./codedash.py ff some-repo
$ ./codedash.py rebase some-repo
```

`list`, `detail` and the actions prefer the running daemon (`--url`, default
`http://127.0.0.1:8090`) and fall back to a local scan when it is not there;
they say which on stderr. `--local` skips the daemon entirely.

## NixOS

```nix
{
  inputs.codedash.url = "github:codebam/codedash";
  inputs.codedash.inputs.nixpkgs.follows = "nixpkgs";

  # in a host module
  imports = [ inputs.codedash.nixosModules.default ];

  services.codedash = {
    enable = true;
    user = "alice";
    root = "/home/alice/Documents/git";
    port = 8090;
    allowActions = true;
  };
}
```

The module installs the `codedash` CLI, runs the daemon as `user` with
`StateDirectory=codedash`, keeps the filesystem read-only except the checkout
root, and binds loopback only. Expose it with a reverse proxy; the module does
not open a firewall port.

## HTTP

`GET /` — dashboard.
`GET /api/repos` — every record; `?refresh=1` scans before answering.
`GET /api/detail?path=<abs path>` — one record plus incoming/outgoing commits
and whether each action is currently allowed.
`POST /api/action` — `{"path": "...", "action": "fetch" | "ff" | "rebase"}`.
Only paths the scanner already knows are accepted. Actions are disabled unless
the daemon was started with `--allow-actions`.

Mounted at `/codedash/` by a reverse proxy; it also serves the unprefixed paths
on loopback, and redirects `/codedash` to `/codedash/`.

## Actions and safety

| action | does | refused when |
| --- | --- | --- |
| `fetch` | `git fetch` of the branch's remote (all remotes if there is no upstream) | never, except a broken checkout |
| `ff` | `git merge --ff-only @{u}` | dirty tree, detached HEAD, no upstream, local commits, already up to date, never fetched |
| `rebase` | `git rebase @{u}` | dirty tree, detached HEAD, no upstream, already up to date, conflicts or another operation in progress |

Every action re-probes the checkout first, so a stale cache cannot get a dirty
repository rewritten. A conflicted rebase is left in progress — that is git's
state to resolve, and the dashboard shows it as `conflicted` + `rebase` until
you continue or abort. Nothing here force-pushes, prunes, or runs `gc`.

The dashboard has no authentication of its own. Keep it on loopback or behind
something that authenticates; if it is merely tailnet-reachable, every device
on that tailnet can rebase every checkout.

## Record

```json
{
  "path": "/home/alice/Documents/git/foo",
  "name": "foo",
  "main_repo": "/home/alice/Documents/git/foo",
  "kind": "main",
  "branch": "main",
  "detached": false,
  "head": "1a2b3c4",
  "upstream": "origin/main",
  "ahead": 2,
  "behind": 5,
  "refs_fetched_at": "2026-11-23T12:00:00Z",
  "staged": 0,
  "unstaged": 1,
  "untracked": 2,
  "conflicted": 0,
  "operation": "none",
  "stashes": 1,
  "last_commit": { "sha": "1a2b3c4", "iso_time": "...", "author": "...", "subject": "..." },
  "state": "dirty",
  "nested": false,
  "error": null
}
```

`state` is one of `error`, `conflicted`, `operation`, `dirty`, `ahead`,
`behind`, `no-upstream`, `clean`, in that precedence. `kind` is `main` or
`worktree`; `nested` means the checkout sits inside another checkout.

## Development

The script is Python 3 standard library only — no virtualenv, no dependencies.
`nix flake check` compiles it; `nix fmt` formats the Nix files.
