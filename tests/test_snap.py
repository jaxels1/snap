import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import hook_entry  # noqa: E402
import snap  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout


class TestSnap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "Tester")
        _git(self.repo, "config", "user.email", "tester@example.com")
        (self.repo / ".gitignore").write_text("ignored_dir/\n*.tmp\n", encoding="utf-8")
        (self.repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore", "app.py")
        _git(self.repo, "commit", "-m", "initial")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_invisible_in_git_status_log_branch_stash_and_respects_gitignore(self):
        # Create ignored dir + uncommitted tracked edit + untracked file
        ign = self.repo / "ignored_dir"
        ign.mkdir()
        (ign / "big.txt").write_text("do not track\n", encoding="utf-8")
        (self.repo / "app.py").write_text("print('v1-uncommitted')\n", encoding="utf-8")
        (self.repo / "notes.txt").write_text("user note\n", encoding="utf-8")

        status_before = _git(self.repo, "status", "--porcelain")
        log_before = _git(self.repo, "log", "--oneline")

        res = snap.create_snapshot(self.repo, label="manual", tool="edit_file", target="app.py")
        self.assertTrue(res["created"])

        # Git user-visible state must be 100% untouched
        self.assertEqual(_git(self.repo, "status", "--porcelain"), status_before)
        self.assertEqual(_git(self.repo, "log", "--oneline"), log_before)
        self.assertEqual(_git(self.repo, "branch").strip(), "* main")
        self.assertEqual(_git(self.repo, "stash", "list").strip(), "")

        # Dedup check: second snap with identical tree must skip creation
        res2 = snap.create_snapshot(self.repo, label="manual")
        self.assertFalse(res2["created"])
        self.assertEqual(res2["reason"], "unchanged")

        # Ignored files must not be inside snapshot tree
        ls_out = _git(self.repo, "ls-tree", "-r", "--name-only", res["tree_sha"])
        self.assertIn("app.py", ls_out)
        self.assertIn("notes.txt", ls_out)
        self.assertNotIn("ignored_dir/big.txt", ls_out)

    def test_diff_undo_and_undo_is_undoable_with_untracked_files(self):
        (self.repo / "app.py").write_text("print('good-user-edit')\n", encoding="utf-8")
        snap1 = snap.create_snapshot(self.repo, label="before agent")
        self.assertTrue(snap1["created"])

        # Agent wrecks tracked file + creates a brand new file inside subdir
        (self.repo / "app.py").write_text("print('BROKEN')\n", encoding="utf-8")
        new_sub = self.repo / "extra" / "junk.py"
        new_sub.parent.mkdir(parents=True)
        new_sub.write_text("bad = True\n", encoding="utf-8")

        _, diff_txt = snap.diff_snapshot(self.repo, "1")
        self.assertIn("BROKEN", diff_txt)
        self.assertIn("extra/junk.py", diff_txt)

        # Undo restores app.py, deletes extra/junk.py, and leaves ignored files alone
        ign = self.repo / "ignored_dir"
        ign.mkdir()
        (ign / "keep.log").write_text("keep me\n", encoding="utf-8")

        rewind = snap.restore_snapshot(self.repo, "1", create_safety=True)
        self.assertTrue(rewind["restored"])
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "print('good-user-edit')\n")
        self.assertFalse(new_sub.exists())
        self.assertTrue((ign / "keep.log").exists())
        self.assertIsNotNone(rewind["safety"])
        self.assertTrue(rewind["safety"]["created"])

        # Now #1 is `pre-restore safety` — restoring #1 brings back the pre-undo state!
        snaps = snap.list_snapshots(self.repo)
        self.assertIn("pre-restore safety", snaps[0]["subject"])
        redo = snap.restore_snapshot(self.repo, "1", create_safety=False)
        self.assertTrue(redo["restored"])
        self.assertEqual((self.repo / "app.py").read_text(encoding="utf-8"), "print('BROKEN')\n")
        self.assertTrue(new_sub.exists())

    def test_prune_keeps_only_newest_n(self):
        for i in range(5):
            (self.repo / "app.py").write_text(f"print({i})\n", encoding="utf-8")
            snap.create_snapshot(self.repo, label=f"step-{i}")

        self.assertEqual(len(snap.list_snapshots(self.repo, limit=20)), 5)
        deleted = snap.prune_snapshots(self.repo, keep=2)
        self.assertEqual(deleted, 3)
        remaining = snap.list_snapshots(self.repo, limit=20)
        self.assertEqual(len(remaining), 2)
        self.assertIn("step-4", remaining[0]["subject"])
        self.assertIn("step-3", remaining[1]["subject"])


class TestSnapHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.name", "Tester")
        _git(self.repo, "config", "user.email", "tester@example.com")
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo, "add", "app.py")
        _git(self.repo, "commit", "-m", "init")
        self.state_dir = Path(self.tmp.name) / "snap-state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_multi_host_mutating_debounce_and_readonly_skip(self):
        with mock.patch.dict(
            os.environ,
            {"SNAP_STATE_DIR": str(self.state_dir), "SNAP_DEBOUNCE_SECONDS": "10.0"},
        ):
            # Read-only tool must NOT create snapshot
            read_payload = {
                "hookEventName": "PreToolUse",
                "conversationId": "c1",
                "toolCall": {"name": "read_file", "args": {"Path": str(self.repo / "app.py")}},
            }
            self.assertEqual(hook_entry.handle_payload(read_payload)["decision"], "allow")
            self.assertEqual(len(snap.list_snapshots(self.repo)), 0)

            # Read-only shell command must NOT create snapshot
            ro_cmd = {
                "hook_event_name": "PreToolUse",
                "session_id": "cl-1",
                "cwd": str(self.repo),
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
            }
            self.assertEqual(
                hook_entry.handle_payload(ro_cmd)["hookSpecificOutput"]["permissionDecision"],
                "allow",
            )
            self.assertEqual(len(snap.list_snapshots(self.repo)), 0)

            # Mutating Claude Edit creates checkpoint
            cl_edit = {
                "hook_event_name": "PreToolUse",
                "session_id": "cl-1",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(self.repo / "app.py")},
            }
            self.assertEqual(
                hook_entry.handle_payload(cl_edit)["hookSpecificOutput"]["permissionDecision"],
                "allow",
            )
            self.assertEqual(len(snap.list_snapshots(self.repo)), 1)

            # Modify file + trigger Cursor Edit within debounce window -> skipped by debounce
            (self.repo / "app.py").write_text("x = 2\n", encoding="utf-8")
            cu_edit = {
                "hook_event_name": "beforeEdit",
                "cursor_version": "0.50",
                "conversation_id": "cu-1",
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.repo / "app.py")},
            }
            self.assertEqual(hook_entry.handle_payload(cu_edit)["permission"], "allow")
            self.assertEqual(len(snap.list_snapshots(self.repo)), 1)

    def test_fail_open_on_malformed_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO("{broken-json")):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                code = hook_entry.main()
        self.assertEqual(code, 0)
        self.assertIn("allow", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
