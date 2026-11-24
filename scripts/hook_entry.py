#!/usr/bin/env python3
"""
snap hook_entry: Multi-host PreToolUse hook (Antigravity, Claude Code, Cursor,
Gemini CLI) that silently records a turn-debounced shadow checkpoint before
mutating edits or commands. Strictly fail-open on errors.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from snap import create_snapshot, find_repo_root  # noqa: E402

FILE_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
}
SHELL_TOOLS = {"run_command", "Bash", "Shell", "run_shell_command", "exec_command"}

READONLY_SHELL_RE = re.compile(
    r"^\s*(?:git\s+(?:status|log|diff|show|branch|rev-parse)\b|ls\b|dir\b|pwd\b|"
    r"cat\b|head\b|tail\b|rg\b|grep\b|find\b|which\b|where\b|echo\b|python3?\s+-m\s+unittest\b|pytest\b)",
    re.I,
)


def detect_host(payload: dict[str, Any]) -> str:
    if "toolCall" in payload or "hookEventName" in payload:
        return "antigravity"
    hook_evt = str(payload.get("hook_event_name") or "")
    if "cursor_version" in payload or hook_evt.startswith("before") or "workspace_roots" in payload:
        return "cursor"
    if "tool_input" in payload and "tool_name" in payload:
        tool_name = str(payload.get("tool_name") or "")
        if payload.get("host") == "gemini" or tool_name in ("run_shell_command", "write_file"):
            return "gemini"
        return "claude"
    return "antigravity"


def extract_tool(payload: dict[str, Any], host: str) -> tuple[str, dict[str, Any], str]:
    if host == "antigravity":
        tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        tool_name = str(tc.get("name") or payload.get("toolName") or "")
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        conv_id = str(payload.get("conversationId") or "antigravity-default")
        return tool_name, args, conv_id

    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    conv_id = str(payload.get("session_id") or payload.get("conversation_id") or f"{host}-default")
    return tool_name, args, conv_id


def format_allow(host: str) -> dict[str, Any]:
    if host == "cursor":
        return {"permission": "allow"}
    if host == "claude":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
    return {"decision": "allow"}


def _debounce_path() -> Path:
    base = Path(os.environ.get("SNAP_STATE_DIR") or (Path(tempfile.gettempdir()) / "snap-state"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "debounce.json"


def _should_debounce(repo_key: str, window_s: float) -> bool:
    if window_s <= 0:
        return False
    db_file = _debounce_path()
    now = time.time()
    try:
        data = json.loads(db_file.read_text(encoding="utf-8")) if db_file.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    last = float(data.get(repo_key, 0.0) or 0.0)
    if now - last < window_s:
        return True

    data[repo_key] = now
    try:
        tmp = db_file.with_name(f"debounce.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, db_file)
    except OSError:
        pass
    return False


def handle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    host = detect_host(payload)
    tool_name, args, conv_id = extract_tool(payload, host)
    norm_tool = tool_name.strip()

    is_file_mut = norm_tool in FILE_WRITE_TOOLS
    is_shell = norm_tool in SHELL_TOOLS
    if not (is_file_mut or is_shell):
        return format_allow(host)

    target_path = ""
    if is_file_mut:
        target_path = str(
            args.get("TargetFile")
            or args.get("Path")
            or args.get("file_path")
            or args.get("path")
            or ""
        )
    elif is_shell:
        cmd_line = str(
            args.get("CommandLine")
            or args.get("command")
            or args.get("cmd")
            or args.get("Command")
            or ""
        ).strip()
        if not cmd_line or READONLY_SHELL_RE.match(cmd_line):
            return format_allow(host)
        target_path = cmd_line.split()[0]

    cwd = str(payload.get("cwd") or args.get("workdir") or "")
    probe = Path(target_path) if target_path and os.path.isabs(target_path) else Path(cwd or os.getcwd())
    repo_root = find_repo_root(probe)
    if repo_root is None:
        return format_allow(host)

    debounce_s = float(os.environ.get("SNAP_DEBOUNCE_SECONDS", "5.0") or "5.0")
    if _should_debounce(str(repo_root), debounce_s):
        return format_allow(host)

    create_snapshot(
        repo_root,
        label="auto",
        tool=norm_tool,
        target=target_path,
        session_id=conv_id,
    )
    return format_allow(host)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print('{"decision": "allow"}')
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print('{"decision": "allow"}')
            return 0
        print(json.dumps(handle_payload(payload)))
        return 0
    except Exception:
        print('{"decision": "allow"}')
        return 0


if __name__ == "__main__":
    sys.exit(main())
