---
name: snap
description: Silent, automatic shadow checkpoints before AI coding agent edits using hidden Git refs (`refs/snap/*`). Use when the user asks to checkpoint their working tree before risky changes, undo/rewind recent agent edits, inspect what changed across turns (`snap list`, `snap diff`, `snap undo`, `snap restore`), or configure automatic PreToolUse turn-level checkpoints without polluting `git log`, `git status`, or `git stash`.
---

# snap

`snap` keeps a flight-recorder timeline of your working tree under hidden Git refs (`refs/snap/*`), without touching `HEAD`, `.git/index`, `git status`, `git log`, or `git stash`.

Always locate `scripts/snap.py` relative to this `SKILL.md` folder. Invoke with `python3` (or `python` / `py -3` on Windows setups where `python3` is not mapped).

## Commands

- **Save a checkpoint right now (before a multi-file refactor):**
  ```bash
  python3 "<skill_dir>/scripts/snap.py" save --label "before refactor"
  ```
- **List recent checkpoints:**
  ```bash
  python3 "<skill_dir>/scripts/snap.py" list
  ```
- **See what changed since checkpoint `#1` (or `#2`, or SHA prefix):**
  ```bash
  python3 "<skill_dir>/scripts/snap.py" diff 1
  python3 "<skill_dir>/scripts/snap.py" diff 1 --stat
  ```
- **Undo / rewind to the latest checkpoint (`#1`) or a specific checkpoint:**
  ```bash
  python3 "<skill_dir>/scripts/snap.py" undo
  python3 "<skill_dir>/scripts/snap.py" restore 2
  ```
  `undo` and `restore` automatically save a `pre-restore safety` checkpoint as `#1` first, so even an undo can be undone.
- **Prune old checkpoints (auto-keeps newest 50 by default):**
  ```bash
  python3 "<skill_dir>/scripts/snap.py" prune --keep 50
  ```

## Safety Rules
- Never run `snap undo` or `snap restore` destructively without either confirming the user wants to rewind or relying on the built-in `pre-restore safety` checkpoint.
- Check `snap diff` first when diagnosing what went wrong across recent turns.
- When `scripts/hook_entry.py` is registered in `PreToolUse`, checkpoints run automatically before mutating edits/commands (debounced once every 5s per repo by default via `SNAP_DEBOUNCE_SECONDS`).
