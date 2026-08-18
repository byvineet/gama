"""
actions/terminal.py — Gama Terminal & Coding-Workspace Automation
=====================================================================
Two distinct needs, one module:

  1. "Run this command and tell me what happened" — a command executes
     hidden, we capture stdout/stderr/exit code, and verify success by
     exit code (0 = success) rather than assuming a launched process worked.

  2. "Open a Terminal / VS Code and get it doing something" — a REAL,
     visible terminal or editor window the user can see and keep using,
     for coding-workspace automation (open a project, run its dev server,
     open an integrated terminal, run git commands, etc.)

Safety: a small blocklist stops obviously destructive raw shell commands
(mass delete, disk formatting, fork bombs) from running even if asked —
those still have to go through computer_settings' confirmation-code path
for the specific actions it already models (shutdown/restart/etc).

Author : Vineet Machchal
"""

from __future__ import annotations

from utils.logger import get_logger

import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Optional

from actions.reliability import retry, is_transient_error, wait_for_process
from utils.proc import hidden_kwargs

log = get_logger(__name__)
logger = log  # back-compat alias
_OS = platform.system()

# Command *fragments* that are never allowed to run, regardless of framing.
# Deny-list is a backstop only — security relies on not running raw LLM
# strings with shell=True (see _run below), not on this list alone.
_DANGEROUS_FRAGMENTS = [
    # Unix mass-delete / fork-bomb
    "rm -rf /", "rm -rf /*", "rm -rf ~", ":(){ :|:& };:",
    # Disk-format / zero-wipe
    "format c:", "format /", "mkfs", "dd if=/dev/zero of=/dev/",
    "dd if=/dev/urandom", "> /dev/sda", "> /dev/hda",
    # Windows mass-delete
    "del /s /q c:\\", "del /f /s /q c:\\", "rd /s /q c:\\",
    "rd /s /q %systemroot%", "del /f /q %systemroot%",
    # Partition management (no user-level reason to invoke this)
    "diskpart",
    # Power — those go through computer_settings' confirmation-code path
    "shutdown -h now", "shutdown /s", "shutdown /r",
    # Reverse-shell / exfil patterns
    "nc -e ", "ncat -e ", "/dev/tcp/", "bash -i >& /dev/tcp",
    # Python/perl one-liner exec launchers
    "python -c ", "python3 -c ", "perl -e ", "ruby -e ",
    # Credential harvesting
    "mimikatz", "sekurlsa", "lsadump",
]


def _is_dangerous(command: str) -> Optional[str]:
    low = (command or "").lower()
    # Strip common quoting so "rm  -rf /" etc. still match
    for ch in ('"', "'", "\t"):
        low = low.replace(ch, " ")
    for frag in _DANGEROUS_FRAGMENTS:
        if frag in low:
            return frag
    return None


def _resolve_cwd(cwd: str = "") -> Optional[str]:
    if not cwd:
        return None
    p = Path(cwd).expanduser()
    return str(p) if p.exists() and p.is_dir() else None


def terminal_command(action: str = "run", **kwargs) -> str:
    """Entry point.

    Actions:
      run          - execute a command, capture output, verify exit code.
      open_window  - open a REAL visible terminal (cmd/PowerShell/Terminal)
                     optionally running a command / cd'd into a folder.
      run_in_workspace - open a folder in VS Code AND run a command in an
                     integrated/attached terminal for it (coding-workspace
                     automation, e.g. "open my project and run npm install").
    """
    action = (action or "run").lower().strip()
    if action == "run":
        return _run(
            kwargs.get("command", ""),
            cwd=kwargs.get("cwd", ""),
            timeout=int(kwargs.get("timeout", 30) or 30),
            retry_if_transient=bool(kwargs.get("retry_if_transient", True)),
        )
    if action in ("open_window", "open_terminal"):
        return _open_window(
            kwargs.get("command", ""),
            cwd=kwargs.get("cwd", ""),
            shell=kwargs.get("shell", "auto"),
        )
    if action == "run_in_workspace":
        return _run_in_workspace(
            kwargs.get("path", ""),
            kwargs.get("command", ""),
        )
    return f"Unknown terminal action: {action}. Use: run, open_window, run_in_workspace."


# ---------------------------------------------------------------------------
# Hidden, captured execution — for "run X and tell me the result"
# ---------------------------------------------------------------------------
def _run(command: str, cwd: str = "", timeout: int = 30,
          retry_if_transient: bool = True) -> str:
    command = (command or "").strip()
    if not command:
        return "What command should I run?"

    danger = _is_dangerous(command)
    if danger:
        return (f"Refusing to run this command — it matches a known-destructive "
                f"pattern ('{danger}'). If you really need a power action "
                f"(shutdown/restart), ask for that directly so it goes through "
                f"the confirmation-code flow.")

    workdir = _resolve_cwd(cwd)
    if cwd and workdir is None:
        return f"Working directory not found: {cwd}"

    import shlex
    try:
        cmd_args = shlex.split(command, posix=(_OS != "Windows"))
    except ValueError as exc:
        return f"Could not parse command: {exc}"
    if not cmd_args:
        return "Empty command after parsing."

    def _exec():
        return subprocess.run(
            cmd_args, shell=False, cwd=workdir,
            capture_output=True, text=True, timeout=timeout, **hidden_kwargs(),
        )

    try:
        if retry_if_transient:
            result = retry(
                _exec, attempts=2, delay=1.0,
                exceptions=(subprocess.SubprocessError, OSError),
                should_retry=lambda r: r.returncode != 0 and is_transient_error(
                    Exception(r.stderr or "")
                ),
            )
        else:
            result = _exec()
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"
    except Exception as exc:
        return f"Command failed to start: {exc}"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    body = out
    if err:
        body = f"{body}\n--- STDERR ---\n{err}" if body else err
    if not body:
        body = "(no output)"

    status = "succeeded" if result.returncode == 0 else "failed"
    return f"Command {status} (exit code {result.returncode}): {command}\n\n{body}"


# ---------------------------------------------------------------------------
# Visible terminal window — for "open a terminal and do X"
# ---------------------------------------------------------------------------
def _open_window(command: str = "", cwd: str = "", shell: str = "auto") -> str:
    workdir = _resolve_cwd(cwd) or str(Path.home())
    if cwd and _resolve_cwd(cwd) is None:
        return f"Working directory not found: {cwd}"

    if _OS != "Windows":
        term_cmd = ["x-terminal-emulator"] if shutil_which("x-terminal-emulator") else ["xterm"]
        try:
            subprocess.Popen(term_cmd, cwd=workdir)
            return "Opened a terminal window."
        except Exception as exc:
            return f"Could not open a terminal: {exc}"

    shell = (shell or "auto").lower()
    if shell == "auto":
        shell = "wt" if _which("wt.exe") else "powershell"

    try:
        if shell == "wt":
            popen_args = ["wt.exe", "-d", workdir]
            if command:
                popen_args += ["cmd", "/k", command]
            subprocess.Popen(popen_args, shell=False)
            verified = wait_for_process("windowsterminal.exe", timeout=5.0)
        elif shell == "powershell":
            popen_args = ["powershell.exe", "-NoExit",
                          "-WorkingDirectory", workdir]
            if command:
                popen_args += ["-Command", command]
            subprocess.Popen(popen_args, shell=False)
            verified = wait_for_process("powershell.exe", timeout=5.0)
        else:  # cmd
            init_cmd = f"cd /d {workdir}"
            if command:
                init_cmd += f" && {command}"
            subprocess.Popen(["cmd.exe", "/K", init_cmd], shell=False)
            verified = wait_for_process("cmd.exe", timeout=5.0)
    except Exception as exc:
        return f"Could not open terminal: {exc}"

    tail = " (confirmed open)" if verified else " (could not confirm it opened)"
    if command:
        return f"Opened a terminal in {workdir} and ran: {command}{tail}"
    return f"Opened a terminal in {workdir}{tail}"


def _which(exe: str) -> bool:
    import shutil
    return shutil.which(exe) is not None


def shutil_which(exe: str) -> bool:
    return _which(exe)


# ---------------------------------------------------------------------------
# Coding workspace automation — open a project in VS Code + run a command
# ---------------------------------------------------------------------------
def _run_in_workspace(path: str, command: str = "") -> str:
    path = (path or "").strip()
    if not path:
        return "Which folder/project should I open?"
    p = Path(path).expanduser()
    if not p.exists() or not p.is_dir():
        return f"Project folder not found: {p}"

    from actions.reliability import expected_process_name

    steps = []
    # 1. Open the folder in VS Code.
    try:
        code_exe = "code.cmd" if _which("code.cmd") else "code"
        if _which(code_exe):
            subprocess.Popen([code_exe, str(p)], shell=False)
            opened = wait_for_process(expected_process_name("vscode") or "code.exe", timeout=8.0)
            steps.append(f"Opened '{p}' in VS Code" + (" (verified)." if opened else " (could not confirm)."))
        else:
            steps.append("VS Code CLI ('code') not found on PATH — skipped opening the editor.")
    except Exception as exc:
        steps.append(f"Could not open VS Code: {exc}")

    # 2. Run the requested command in a terminal cd'd into the project.
    if command:
        result = _open_window(command=command, cwd=str(p))
        steps.append(result)

    return "\n".join(steps)


__all__ = ["terminal_command"]
