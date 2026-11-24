#!/usr/bin/env python3
"""
snap: Silent, zero-dependency Git shadow checkpoints for AI coding agents.
Stores snapshots under hidden `refs/snap/*` without touching HEAD, .git/index,
`git status`, `git log`, or `git stash`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REF_PREFIX = "refs/snap/"


def _run_git(
    repo_root: Path,
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc


def find_repo_root(path: str | Path | None = None) -> Path | None:
    target = Path(path or os.getcwd())
    if target.is_file():
        target = target.parent
    if not target.exists():
        # Walk up to nearest existing parent directory
        for parent in target.parents:
            if parent.exists():
                target = parent
                break
        else:
            return None
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(target),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out) if out else None


def _git_dir(repo_root: Path) -> Path:
    proc = _run_git(repo_root, ["rev-parse", "--git-dir"])
    gd = Path(proc.stdout.strip())
    return gd if gd.is_absolute() else (repo_root / gd)


def _temp_index_tree(repo_root: Path) -> tuple[str, set[str]]:
    """
    Build a Git tree object reflecting the current working tree (tracked +
    untracked, respecting .gitignore) using an isolated temporary index file.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="snap-idx-"))
    tmp_index = tmp_dir / "index"
    try:
        real_index = _git_dir(repo_root) / "index"
        if real_index.is_file():
            shutil.copy2(real_index, tmp_index)

        env_extra = {"GIT_INDEX_FILE": str(tmp_index)}
        _run_git(repo_root, ["add", "-A", "--", "."], env_extra=env_extra)
        tree_sha = _run_git(repo_root, ["write-tree"], env_extra=env_extra).stdout.strip()
        files_out = _run_git(
            repo_root, ["ls-files", "-z"], env_extra=env_extra
        ).stdout
        files = {f for f in files_out.split("\0") if f}
        return tree_sha, files
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def list_snapshots(repo_root: Path, limit: int = 20) -> list[dict[str, Any]]:
    fmt = "%(refname)\t%(objectname)\t%(tree)\t%(creatordate:unix)\t%(subject)"
    proc = _run_git(
        repo_root,
        ["for-each-ref", "--sort=-refname", f"--format={fmt}", REF_PREFIX],
        check=False,
    )
    if proc.returncode != 0:
        return []
    items: list[dict[str, Any]] = []
    for idx, line in enumerate(proc.stdout.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        ref, sha, tree_sha, ts_str, subject = parts
        try:
            created_ts = int(ts_str)
        except ValueError:
            created_ts = 0
        items.append(
            {
                "index": idx,
                "ref": ref,
                "sha": sha,
                "short_sha": sha[:8],
                "tree_sha": tree_sha,
                "created_ts": created_ts,
                "subject": subject,
            }
        )
        if limit > 0 and len(items) >= limit:
            break
    return items


def prune_snapshots(repo_root: Path, keep: int = 50) -> int:
    snaps = list_snapshots(repo_root, limit=0)
    if keep < 0:
        keep = 0
    to_delete = snaps[keep:]
    deleted = 0
    for item in to_delete:
        proc = _run_git(repo_root, ["update-ref", "-d", item["ref"]], check=False)
        if proc.returncode == 0:
            deleted += 1
    return deleted


def create_snapshot(
    repo_root: Path,
    *,
    label: str = "manual",
    tool: str = "",
    target: str = "",
    session_id: str = "",
    max_snaps: int = 50,
) -> dict[str, Any]:
    tree_sha, _ = _temp_index_tree(repo_root)
    existing = list_snapshots(repo_root, limit=1)
    if existing and existing[0]["tree_sha"] == tree_sha:
        return {
            "created": False,
            "reason": "unchanged",
            "tree_sha": tree_sha,
            "latest": existing[0],
        }

    parts = [label]
    if tool:
        parts.append(f"before {tool}")
    if target:
        base = os.path.basename(target.rstrip("/\\")) or target
        parts.append(base)
    if session_id:
        sid_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
        parts.append(f"[{sid_digest}]")
    message = " · ".join(parts)

    commit_env = {
        "GIT_AUTHOR_NAME": "snap",
        "GIT_AUTHOR_EMAIL": "snap@local",
        "GIT_COMMITTER_NAME": "snap",
        "GIT_COMMITTER_EMAIL": "snap@local",
    }
    commit_sha = _run_git(
        repo_root,
        ["commit-tree", tree_sha, "-m", message],
        env_extra=commit_env,
    ).stdout.strip()

    ts_ms = int(time.time() * 1000)
    rand = secrets.token_hex(3)
    ref_name = f"{REF_PREFIX}{ts_ms:016d}-{rand}"
    _run_git(repo_root, ["update-ref", ref_name, commit_sha])

    if max_snaps > 0:
        prune_snapshots(repo_root, keep=max_snaps)

    return {
        "created": True,
        "ref": ref_name,
        "sha": commit_sha,
        "short_sha": commit_sha[:8],
        "tree_sha": tree_sha,
        "subject": message,
    }


def resolve_snapshot(repo_root: Path, selector: str | None = None) -> dict[str, Any]:
    snaps = list_snapshots(repo_root, limit=0)
    if not snaps:
        raise ValueError("No snap checkpoints found in this repository.")
    if selector is None or selector == "" or selector == "latest":
        return snaps[0]

    sel = selector.lstrip("#").strip()
    if sel.isdigit():
        num = int(sel)
        if 1 <= num <= len(snaps):
            return snaps[num - 1]

    for item in snaps:
        if item["sha"].startswith(sel) or item["ref"] == selector or item["ref"].endswith(sel):
            return item

    raise ValueError(f"Checkpoint '{selector}' not found.")


def diff_snapshot(
    repo_root: Path,
    selector: str | None = None,
    *,
    stat_only: bool = False,
) -> tuple[dict[str, Any], str]:
    snap = resolve_snapshot(repo_root, selector)
    cur_tree, _ = _temp_index_tree(repo_root)
    if cur_tree == snap["tree_sha"]:
        return snap, ""
    args = ["diff"]
    if stat_only:
        args.append("--stat")
    args.extend([snap["tree_sha"], cur_tree])
    out = _run_git(repo_root, args).stdout
    return snap, out


def _cleanup_empty_parents(repo_root: Path, rel_path: str) -> None:
    cur = (repo_root / rel_path).parent
    while cur != repo_root and cur.is_dir():
        if cur.name == ".git":
            break
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent


def restore_snapshot(
    repo_root: Path,
    selector: str | None = None,
    *,
    create_safety: bool = True,
) -> dict[str, Any]:
    target = resolve_snapshot(repo_root, selector)
    cur_tree, cur_files = _temp_index_tree(repo_root)

    if cur_tree == target["tree_sha"]:
        return {
            "restored": False,
            "reason": "already-matches",
            "target": target,
            "safety": None,
        }

    safety: dict[str, Any] | None = None
    if create_safety:
        safety = create_snapshot(
            repo_root,
            label="pre-restore safety",
            target=target["short_sha"],
        )

    ls_out = _run_git(
        repo_root, ["ls-tree", "-r", "-z", "--name-only", target["tree_sha"]]
    ).stdout
    snap_files = {f for f in ls_out.split("\0") if f}

    # Remove files created after the target snapshot
    removed_files: list[str] = []
    for extra in sorted(cur_files - snap_files):
        p = repo_root / extra
        if p.is_file() or p.is_symlink():
            p.unlink()
            removed_files.append(extra)
            _cleanup_empty_parents(repo_root, extra)

    # Restore snapshot files cleanly via an isolated temporary index
    tmp_dir = Path(tempfile.mkdtemp(prefix="snap-restore-"))
    tmp_index = tmp_dir / "index"
    try:
        env_extra = {"GIT_INDEX_FILE": str(tmp_index)}
        _run_git(repo_root, ["read-tree", target["tree_sha"]], env_extra=env_extra)
        if snap_files:
            _run_git(repo_root, ["checkout-index", "-a", "-f"], env_extra=env_extra)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "restored": True,
        "target": target,
        "safety": safety,
        "removed_files": removed_files,
        "restored_count": len(snap_files),
    }


def _format_age(created_ts: int) -> str:
    if created_ts <= 0:
        return "now"
    delta = max(0, int(time.time()) - created_ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="snap",
        description="Silent Git shadow checkpoints for AI coding agents.",
    )
    parser.add_argument("-C", "--repo", default=None, help="Path inside git repository")
    sub = parser.add_subparsers(dest="cmd")

    p_save = sub.add_parser("save", help="Create a shadow checkpoint now")
    p_save.add_argument("--label", default="manual", help="Checkpoint label")

    p_list = sub.add_parser("list", help="List recent checkpoints")
    p_list.add_argument("-n", "--limit", type=int, default=20, help="Max items")
    p_list.add_argument("--json", action="store_true", help="JSON output")

    p_diff = sub.add_parser("diff", help="Diff working tree vs checkpoint")
    p_diff.add_argument("id", nargs="?", default="1", help="Index (#1) or SHA prefix")
    p_diff.add_argument("--stat", action="store_true", help="Summary diffstat only")

    sub.add_parser("undo", help="Rewind to the latest checkpoint (#1)")

    p_res = sub.add_parser("restore", help="Rewind to checkpoint index or SHA")
    p_res.add_argument("id", help="Index (#1, #2, ...) or SHA prefix")
    p_res.add_argument("--no-safety", action="store_true", help="Skip pre-restore safety snap")

    p_prune = sub.add_parser("prune", help="Keep only N newest checkpoints")
    p_prune.add_argument("--keep", type=int, default=50, help="Number of checkpoints to keep")

    ns = parser.parse_args(argv)
    if not ns.cmd:
        parser.print_help()
        return 0

    repo_root = find_repo_root(ns.repo)
    if repo_root is None:
        print("snap: not inside a git repository", file=sys.stderr)
        return 2

    try:
        if ns.cmd == "save":
            res = create_snapshot(repo_root, label=ns.label)
            if res["created"]:
                print(f"snap: saved {res['short_sha']} ({res['subject']})")
            else:
                print("snap: working tree unchanged since latest checkpoint")
            return 0

        if ns.cmd == "list":
            items = list_snapshots(repo_root, limit=ns.limit)
            if ns.json:
                print(json.dumps(items, indent=2))
                return 0
            if not items:
                print("No snap checkpoints yet.")
                return 0
            for item in items:
                print(
                    f"#{item['index']:<3} {item['short_sha']}  {_format_age(item['created_ts']):<8}  {item['subject']}"
                )
            return 0

        if ns.cmd == "diff":
            snap, out = diff_snapshot(repo_root, ns.id, stat_only=ns.stat)
            if not out:
                print(f"No changes since #{snap['index']} ({snap['short_sha']}).")
            else:
                print(out.rstrip())
            return 0

        if ns.cmd == "undo":
            res = restore_snapshot(repo_root, "1", create_safety=True)
            t = res["target"]
            if not res["restored"]:
                print(f"Working tree already matches #{t['index']} ({t['short_sha']}).")
            else:
                print(
                    f"Rewound to #{t['index']} {t['short_sha']} ({t['subject']})."
                )
                if res["safety"] and res["safety"].get("created"):
                    print(f"Safety checkpoint saved as #1 ({res['safety']['short_sha']}).")
            return 0

        if ns.cmd == "restore":
            res = restore_snapshot(repo_root, ns.id, create_safety=not ns.no_safety)
            t = res["target"]
            if not res["restored"]:
                print(f"Working tree already matches #{t['index']} ({t['short_sha']}).")
            else:
                print(
                    f"Restored #{t['index']} {t['short_sha']} ({t['subject']})."
                )
                if res["safety"] and res["safety"].get("created"):
                    print(f"Safety checkpoint saved as #1 ({res['safety']['short_sha']}).")
            return 0

        if ns.cmd == "prune":
            removed = prune_snapshots(repo_root, keep=ns.keep)
            print(f"Pruned {removed} old checkpoint(s); kept up to {ns.keep}.")
            return 0
    except Exception as exc:
        print(f"snap error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
