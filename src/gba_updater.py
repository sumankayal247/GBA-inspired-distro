#!/usr/bin/env python3
"""
In-shell self-update for GBA OS (SETTINGS -> System -> Check for updates).

The flow is deliberately anonymous and read-only on the network side:

1. ``network_ok`` proves github.com:443 is reachable (no login, no token --
   the repository is public, so a plain HTTPS clone is all it takes).
2. ``check_for_updates`` compares the commit recorded in the install's
   ``VERSION`` file with ``origin/<branch>`` via ``git ls-remote`` and, when
   they differ, shallow-clones the new history so the shell can show *what*
   changed.
3. ``apply_update`` resets that working clone to the new commit and runs the
   repository's own ``scripts/install.sh --apply --repair`` from it, which
   re-copies every GBA OS file in place while leaving ``/data`` (saves,
   settings, logs) and ``/games`` untouched by contract.

Security notes (D-055):

- Nothing is ever pushed; the only write is to the install directory, done by
  the shipped installer, elevated with ``sudo -n`` when the shell itself is
  unprivileged.  If passwordless sudo is not configured the apply fails with
  the installer's output instead of prompting for a password on a console UI.
- ``GIT_TERMINAL_PROMPT=0`` guarantees no git command can block on a
  credential prompt behind the modal.
- The working clone lives under the data dir (``<data>/update/work``) and is
  disposable: every apply hard-resets it to ``origin/<branch>``.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from common import get_data_dir

DEFAULT_REPO_URL = "https://github.com/sumankayal247/GBA-inspired-distro.git"
DEFAULT_BRANCH = "main"
DEFAULT_HOST = "github.com"
DEFAULT_PORT = 443

_GIT_ENV_EXTRA = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


@dataclass
class UpdateReport:
    """Outcome of a check: either up-to-date, behind, or an error string."""

    local_sha: Optional[str] = None
    remote_sha: Optional[str] = None
    behind: Optional[int] = None
    subjects: List[str] = field(default_factory=list)
    up_to_date: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def default_install_dir() -> str:
    return os.environ.get("GBA_INSTALL_DIR", "/opt/gba-distro")


def default_work_dir() -> str:
    return os.path.join(get_data_dir(), "update", "work")


def network_ok(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
               timeout: float = 4.0) -> bool:
    """True when a TCP connection to `host`:`port` can be established."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _git(args: List[str], cwd: Optional[str] = None,
         timeout: float = 60.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(_GIT_ENV_EXTRA)
    # Inject safe.directory=* to bypass git's ownership check. The updater
    # may run as root while /data/update/work is owned by the user account,
    # which triggers git's "dubious ownership" error. This is safe because
    # we only operate on our own repository.
    return subprocess.run(["git", "-c", "safe.directory=*"] + args, cwd=cwd,
                          env=env, timeout=timeout, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


def _tail(text: str, lines: int = 12) -> str:
    kept = [ln for ln in (text or "").splitlines() if ln.strip()][-lines:]
    return "\n".join(kept)


def installed_version(install_dir: str) -> Optional[str]:
    """Commit sha of the running install, from VERSION or a local .git."""
    version_file = os.path.join(install_dir, "VERSION")
    try:
        with open(version_file, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("commit "):
                    sha = line.split()[1].strip()
                    if sha and sha != "unknown":
                        return sha
    except OSError:
        pass
    if os.path.isdir(os.path.join(install_dir, ".git")):
        probe = _git(["rev-parse", "--short=12", "HEAD"], cwd=install_dir, timeout=15)
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.strip()
    return None


def _same_commit(left: Optional[str], right: Optional[str]) -> bool:
    return bool(left) and bool(right) and (
        right.startswith(left) or left.startswith(right))


def _ensure_clone(work_dir: str, repo_url: str, branch: str, depth: int,
                  timeout: float) -> Optional[str]:
    """Returns an error string, or None when work_dir sits on origin/branch."""
    if os.path.isdir(os.path.join(work_dir, ".git")):
        fetch = _git(["fetch", "--depth", str(depth), "origin", branch],
                     cwd=work_dir, timeout=timeout)
        if fetch.returncode != 0:
            return f"git fetch failed: {_tail(fetch.stderr, 4)}"
    else:
        parent = os.path.dirname(work_dir)
        os.makedirs(parent, exist_ok=True)
        clone = _git(["clone", "--depth", str(depth), "--branch", branch,
                      repo_url, work_dir], timeout=timeout)
        if clone.returncode != 0:
            return f"git clone failed: {_tail(clone.stderr, 4)}"
    return None


def check_for_updates(work_dir: str, local_sha: Optional[str],
                      repo_url: str = DEFAULT_REPO_URL,
                      branch: str = DEFAULT_BRANCH, depth: int = 50,
                      timeout: float = 120.0) -> UpdateReport:
    """Compare the install against origin/branch; list what is new."""
    if shutil.which("git") is None:
        return UpdateReport(local_sha=local_sha,
                            error="git is not installed on this system.")

    probe = _git(["ls-remote", repo_url, f"refs/heads/{branch}"], timeout=min(timeout, 60.0))
    if probe.returncode != 0 or not probe.stdout.strip():
        return UpdateReport(local_sha=local_sha,
                            error=f"Cannot read {repo_url} ({branch}): "
                                  f"{_tail(probe.stderr, 3) or 'no such branch'}")
    remote_sha = probe.stdout.split()[0]

    if _same_commit(local_sha, remote_sha):
        return UpdateReport(local_sha=local_sha, remote_sha=remote_sha,
                            behind=0, up_to_date=True)

    report = UpdateReport(local_sha=local_sha, remote_sha=remote_sha)
    error = _ensure_clone(work_dir, repo_url, branch, depth, timeout)
    if error:
        report.error = error
        return report

    subjects: List[str] = []
    if local_sha:
        log = _git(["log", "--oneline", f"{local_sha}..origin/{branch}"],
                   cwd=work_dir, timeout=30)
        if log.returncode == 0:
            subjects = log.stdout.splitlines()
        count = _git(["rev-list", "--count", f"{local_sha}..origin/{branch}"],
                     cwd=work_dir, timeout=30)
        if count.returncode == 0 and count.stdout.strip().isdigit():
            report.behind = int(count.stdout.strip())
    if not subjects:
        log = _git(["log", "--oneline", "-n", "5", f"origin/{branch}"],
                   cwd=work_dir, timeout=30)
        if log.returncode == 0:
            subjects = log.stdout.splitlines()
    report.subjects = subjects[:8]
    return report


def apply_update(work_dir: str, install_dir: str,
                 repo_url: str = DEFAULT_REPO_URL, branch: str = DEFAULT_BRANCH,
                 use_sudo: Optional[bool] = None, depth: int = 50,
                 timeout: float = 900.0) -> Tuple[bool, str]:
    """Reset the working clone to origin/branch and run its installer.

    Returns (ok, output_tail).  The installer (``scripts/install.sh --apply
    --repair``) is the single code path that touches system files, so an
    in-shell update can never drift from a manual one.
    """
    if shutil.which("git") is None:
        return False, "git is not installed on this system."

    error = _ensure_clone(work_dir, repo_url, branch, depth, timeout)
    if error:
        return False, error
    reset = _git(["reset", "--hard", f"origin/{branch}"], cwd=work_dir, timeout=60)
    if reset.returncode != 0:
        return False, f"git reset failed: {_tail(reset.stderr, 4)}"

    script = os.path.join(work_dir, "scripts", "install.sh")
    if not os.path.isfile(script):
        return False, f"Update checkout has no scripts/install.sh ({script})."
    os.chmod(script, 0o755)

    if use_sudo is None:
        use_sudo = os.geteuid() != 0 and shutil.which("sudo") is not None
    cmd = (["sudo", "-n"] if use_sudo else []) + [script, "--apply", "--repair"]
    env = dict(os.environ)
    env["GBA_INSTALL_DIR"] = install_dir
    try:
        run = subprocess.run(cmd, cwd=work_dir, env=env, timeout=timeout,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
    except subprocess.TimeoutExpired:
        return False, "The installer timed out; re-run Check for updates."
    except OSError as exc:
        return False, f"Could not run the installer: {exc}"
    tail = _tail(run.stdout, 12)
    if run.returncode != 0:
        return False, tail or f"installer exited with code {run.returncode}"
    return True, tail or "Update applied."
