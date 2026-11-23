# codedash — build brief

The prompt this repository was built from, kept as the design contract.
It describes what the code must do; when behaviour changes, change this file
in the same commit.

## Task

Build a status service for every git checkout under `~/Documents/git`, with
three surfaces over one scanner: a web dashboard, a command-line client, and a
terminal UI. Show how far each checkout is from its upstream, and let a person
fetch, fast-forward or rebase one checkout from those surfaces.

## Outcome

- `codedash serve` runs as a systemd service and serves an auto-refreshing
  HTML dashboard at `/codedash/` on loopback, plus JSON at `/codedash/api/repos`.
- `codedash list` prints the same records as a scriptable table or `--json`.
- `codedash tui` shows them in curses with keyboard navigation.
- `codedash detail <checkout>` shows the commits behind and ahead.
- `codedash fetch|ff|rebase <checkout>`, the dashboard's detail panel, and the
  TUI's detail panel perform those actions.
- Scanning leaves every repository byte-identical; only the three explicit
  actions write, and each checks preconditions against a fresh probe.

## Discovery

The tree is not flat: 212 checkouts, 115 physical repositories, 97 linked
worktrees, nested repositories up to depth 4.

- Walk to depth 4 for `.git` entries that are a directory *or* a file (a file
  is a linked worktree).
- Do not follow symlinks. Skip `.git`, `node_modules`, `.direnv`, `.venv`,
  `__pycache__`, `target`, `result`, `result-*`.
- Do not skip hidden directories wholesale: `pastebin-r2/.worktrees/design`
  is a real checkout.
- Repositories nested inside repositories are real and must appear.
- After finding a physical repo, also read `git worktree list --porcelain` so
  a worktree outside the walked depth is still seen; include only paths under
  the root, count the rest as external.
- Deduplicate by `git rev-parse --path-format=absolute --git-common-dir`,
  never by path: without `--path-format=absolute` every repo collapses into
  the same relative key.
- A broken checkout is a record with an `error`, not a missing row.

## Record

`path`, `name`, `main_repo`, `kind` (`main`/`worktree`), `branch`, `detached`,
`head`, `upstream`, `ahead`, `behind`, `refs_fetched_at`, `staged`, `unstaged`,
`untracked`, `conflicted`, `operation` (`none`/`merge`/`rebase`/`cherry-pick`/
`revert`/`bisect`), `stashes`, `last_commit`, `state`, `nested`, `error`.
Top level: `generated_at`, `duration_ms`, `checkout_count`,
`external_worktrees`, `records`.

`state` precedence: `error`, `conflicted`, `operation`, `dirty`, `ahead`,
`behind`, `no-upstream`, `clean`.

Status comes from `git --no-optional-locks -C <path> status --porcelain=v2
--branch --untracked-files=normal`. The flag is global; inside `status` it is
a usage error. It is what keeps a scan from rewriting every index.

## Scanning

One scan at a time, at most 8 workers; the full tree takes ~1.2 s. Per-checkout
timeout 15 s; a timeout is a record, never fatal. Results are cached and
served stale-while-refreshing; an explicit refresh blocks for a fresh scan.
Bind loopback only. No external assets in the page: it must render with the
network down.

## Actions

| action | command | refused when |
| --- | --- | --- |
| fetch | `git fetch` of the branch remote, or all remotes | broken checkout |
| ff | `git merge --ff-only @{u}` | dirty, detached, no upstream, local commits, up to date, never fetched |
| rebase | `git rebase @{u}` | dirty, detached, no upstream, up to date, conflicts/operation in progress |

Never force, never prune, never `gc`, never prompt (`GIT_TERMINAL_PROMPT=0`,
`BatchMode=yes`, editors set to `true`). Actions are opt-in on the daemon
(`--allow-actions`) and only accept paths the scanner already knows.

## NixOS integration

One host module, one service, one CLI wrapper. Run as the checkout owner —
`DynamicUser` cannot read `/home` and `ProtectHome=true` sees zero repositories.
`ProtectSystem=strict`, `ProtectHome=read-only`, and `ReadWritePaths` naming
only the checkout root. State lives in `/var/lib/codedash` via `StateDirectory`.

## Must not

- No writes during a scan: no `fetch`, `pull`, `gc`, `prune`, `fsck`.
- No modification of anything under the root outside the three actions.
- No public vhost, no authentication invented in the service; keep it loopback
  or behind a proxy that authenticates.
- No new runtime dependencies: Python 3 standard library only.

## Done when

1. `nixos-rebuild build --flake .#nixos-desktop` (in the consuming flake)
   exits 0, and `nix flake check` passes here.
2. A scan's JSON `checkout_count` matches the discovery result, including
   worktrees outside the walked depth.
3. `ff` refuses a dirty checkout; `ff` fast-forwards a clean, behind one;
   `rebase` replays a diverged one and leaves a conflict in progress.
4. A scan creates no `*.lock` and touches no `index` under the root.
5. `codedash list --json` works with the daemon stopped.
