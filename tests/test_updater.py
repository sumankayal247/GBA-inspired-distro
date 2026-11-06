#!/usr/bin/env python3
"""
Tests for src/gba_updater.py -- the anonymous in-shell self-update (D-055).

Everything runs against throwaway local git repositories served over
file:// so no test touches the network or the real install directory.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import gba_updater  # noqa: E402

_GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_SYSTEM=os.devnull,
    GIT_AUTHOR_NAME="tester", GIT_AUTHOR_EMAIL="tester@example.com",
    GIT_COMMITTER_NAME="tester", GIT_COMMITTER_EMAIL="tester@example.com",
)


def _git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), env=_GIT_ENV,
                          check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)


def _make_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, cwd=path)
    return path


def _commit(repo: Path, name: str, text: str = "x") -> str:
    (repo / name).write_text(text)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", name, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def _write_installer(repo: Path, body: str) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "install.sh").write_text(body)
    (scripts / "install.sh").chmod(0o755)


GOOD_INSTALLER = """#!/bin/sh
echo "installing to $GBA_INSTALL_DIR"
mkdir -p "$GBA_INSTALL_DIR"
echo applied > "$GBA_INSTALL_DIR/marker"
exit 0
"""

BAD_INSTALLER = """#!/bin/sh
echo boom
exit 3
"""


class TestNetworkOk(unittest.TestCase):
    def test_open_port_is_reachable(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.assertTrue(gba_updater.network_ok("127.0.0.1", port, timeout=2.0))

    def test_closed_port_is_not(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        self.assertFalse(gba_updater.network_ok("127.0.0.1", port, timeout=0.5))


class TestInstalledVersion(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_version_file_wins(self):
        (self.dir / "VERSION").write_text("GBA OS\ncommit abc1234deadb\ninstalled x\n")
        self.assertEqual(gba_updater.installed_version(str(self.dir)), "abc1234deadb")

    def test_unknown_commit_is_none(self):
        (self.dir / "VERSION").write_text("GBA OS\ncommit unknown\n")
        self.assertIsNone(gba_updater.installed_version(str(self.dir)))

    def test_git_checkout_falls_back(self):
        sha = _commit(_make_repo(self.dir / "repo"), "one")
        found = gba_updater.installed_version(str(self.dir / "repo"))
        self.assertTrue(sha.startswith(found))

    def test_missing_dir(self):
        self.assertIsNone(gba_updater.installed_version(str(self.dir / "nope")))


class TestCheckForUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = _make_repo(self.tmp / "origin")
        self.first = _commit(self.origin, "first")
        _write_installer(self.origin, GOOD_INSTALLER)
        self.second = _commit(self.origin, "second")
        self.url = f"file://{self.origin}"
        self.work = self.tmp / "work"
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_up_to_date_needs_no_clone(self):
        report = gba_updater.check_for_updates(str(self.work), self.second,
                                               repo_url=self.url)
        self.assertTrue(report.ok)
        self.assertTrue(report.up_to_date)
        self.assertEqual(report.behind, 0)
        self.assertFalse(self.work.exists(), "up-to-date check must not clone")

    def test_behind_lists_new_commits(self):
        report = gba_updater.check_for_updates(str(self.work), self.first,
                                               repo_url=self.url)
        self.assertTrue(report.ok)
        self.assertFalse(report.up_to_date)
        self.assertEqual(report.behind, 1)
        self.assertEqual(report.remote_sha, self.second)
        self.assertTrue(any("second" in line for line in report.subjects),
                        report.subjects)
        self.assertTrue((self.work / ".git").is_dir())

    def test_reused_work_dir_fetches(self):
        gba_updater.check_for_updates(str(self.work), self.first, repo_url=self.url)
        third = _commit(self.origin, "third")
        report = gba_updater.check_for_updates(str(self.work), self.first,
                                               repo_url=self.url)
        self.assertTrue(report.ok)
        self.assertEqual(report.behind, 2)
        self.assertEqual(report.remote_sha, third)

    def test_unknown_local_version_still_offers_update(self):
        report = gba_updater.check_for_updates(str(self.work), None,
                                               repo_url=self.url)
        self.assertTrue(report.ok)
        self.assertFalse(report.up_to_date)
        self.assertIsNone(report.behind)
        self.assertTrue(report.subjects)

    def test_unreachable_repo_is_an_error(self):
        report = gba_updater.check_for_updates(str(self.work), self.first,
                                               repo_url=f"file://{self.tmp}/nope")
        self.assertFalse(report.ok)
        self.assertTrue(report.error)


class TestApplyUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = _make_repo(self.tmp / "origin")
        _write_installer(self.origin, GOOD_INSTALLER)
        _commit(self.origin, "installer")
        self.url = f"file://{self.origin}"
        self.work = self.tmp / "work"
        self.install = self.tmp / "install"
        self.install.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_apply_runs_the_repo_installer(self):
        ok, tail = gba_updater.apply_update(str(self.work), str(self.install),
                                            repo_url=self.url, use_sudo=False)
        self.assertTrue(ok, tail)
        self.assertTrue((self.install / "marker").exists())
        self.assertIn("installing to", tail)

    def test_apply_propagates_installer_failure(self):
        _write_installer(self.origin, BAD_INSTALLER)
        _commit(self.origin, "broken installer")
        ok, tail = gba_updater.apply_update(str(self.work), str(self.install),
                                            repo_url=self.url, use_sudo=False)
        self.assertFalse(ok)
        self.assertIn("boom", tail)
        self.assertFalse((self.install / "marker").exists())

    def test_apply_without_installer_script_fails(self):
        origin = _make_repo(self.tmp / "origin2")
        _commit(origin, "no installer")
        ok, tail = gba_updater.apply_update(str(self.tmp / "work2"), str(self.install),
                                            repo_url=f"file://{origin}", use_sudo=False)
        self.assertFalse(ok)
        self.assertIn("install.sh", tail)


if __name__ == "__main__":
    unittest.main()
