# snap

Silent per-turn shadow checkpoints for AI coding agents — rewind bad agent edits in one command without touching `git status`, `git log`, or `git stash`.

Zero third-party dependencies. Pure Python 3 stdlib + Git plumbing (`refs/snap/*`).

## Why not `git stash`?

- `git stash` pops into your working tree, drops entries if you forget, clutters your stash stack, and leaves untracked files out unless you pass `-u`.
- `snap` builds a snapshot tree using a temporary `GIT_INDEX_FILE` and stores it under hidden **`refs/snap/*`** references:
  - **Zero git noise:** never appears in `git status`, `git log`, `git branch`, or `git stash list`.
  - **Captures tracked + untracked files** while still honoring `.gitignore` (never copies `node_modules`, `.venv`, or build outputs).
  - **Turn-debounced `PreToolUse` hook:** takes one automatic snapshot before an agent starts editing (debounced 5s per repo so 8 parallel tool calls don't create 8 duplicate snaps) and fails open on errors.
  - **Undo is itself undoable:** `snap undo` automatically saves a `pre-restore safety` checkpoint before rolling back.

## Quickstart

Install via `skills`:

```bash
npx skills add jaxels1/snap
```

Or run the CLI directly inside any Git repo:

```bash
# Save a manual checkpoint
python3 scripts/snap.py save --label "before refactor"

# List recent checkpoints
python3 scripts/snap.py list

# Inspect exact diff since checkpoint #1 (includes newly created files)
python3 scripts/snap.py diff 1

# Rewind to #1 (auto-saves a safety checkpoint first)
python3 scripts/snap.py undo

# Restore an earlier checkpoint by index or SHA prefix
python3 scripts/snap.py restore 3

# Trim old shadow refs (auto-pruned at 50 by default)
python3 scripts/snap.py prune --keep 50
```

## Hook Setup (Automatic Per-Turn Checkpoints)

Register `scripts/hook_entry.py` as a `PreToolUse` hook in your agent:

- **Claude Code:** install plugin via `.claude-plugin/plugin.json` (uses `${CLAUDE_PLUGIN_ROOT}/scripts/hook_entry.py`).
- **Antigravity / Codex / Cursor:** merge `hooks.json` into your host `hooks.json` (or `.cursor/hooks.json`) pointing to `python3 "<path-to-snap>/scripts/hook_entry.py"`.

Environment toggles:
- `SNAP_DEBOUNCE_SECONDS=5.0` — minimum seconds between automatic hook checkpoints per repo (set `0` to snapshot every mutating call).
- `SNAP_STATE_DIR` — override directory for debounce timestamps.

*(Note: Ubuntu ships `/usr/bin/snap` for Snapcraft packages, so invoke this tool via `python3 scripts/snap.py` or alias it as `agent-snap`.)*

## License

MIT — see [LICENSE](./LICENSE).
