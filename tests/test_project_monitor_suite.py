"""Tests for enterprise milestone 1: repo identity, project discovery,
monitored-project registry, and the ProjectMonitor service.

All tests are offline and touch only temp directories — never real repos or
the network.  Git helpers create throwaway repos under temp dirs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.project_discovery import LocalProjectDiscoveryAgent
from daemon.project_monitor import ProjectMonitor
from daemon.repo_identity import canonical_repo_identity, clear_caches, resolve_base_commit
from daemon.store import StateStore


def _make_git_repo(base: Path, name: str = "proj") -> Path:
    """Create a throwaway git repo under *base* with one committed file."""
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   capture_output=True)
    return repo


class RepoIdentityTests(unittest.TestCase):
    def test_git_repo_identity_and_base_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_git_repo(Path(directory))
            clear_caches()
            ident = canonical_repo_identity(str(repo))
            self.assertTrue(ident["is_git"])
            self.assertEqual(ident["abs_path"], str(repo.resolve()))
            self.assertIn(str(repo.resolve()), ident["canonical_ref"])
            commit = resolve_base_commit(str(repo))
            self.assertIsNotNone(commit)
            self.assertEqual(len(commit or ""), 40)

    def test_non_git_dir_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            ident = canonical_repo_identity(str(d))
            self.assertFalse(ident["is_git"])
            self.assertEqual(ident["canonical_ref"], str(d.resolve()))

    def test_raw_missing_path_identity(self) -> None:
        ident = canonical_repo_identity("/nonexistent/xyz")
        self.assertFalse(ident["is_git"])
        self.assertEqual(ident["canonical_ref"], "/nonexistent/xyz")

    def test_symlink_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = _make_git_repo(base, "real")
            link = base / "link"
            link.symlink_to(real, target_is_directory=True)
            ident = canonical_repo_identity(str(link))
            self.assertTrue(ident["is_git"])
            self.assertEqual(ident["abs_path"], str(real.resolve()))


class ProjectDiscoveryTests(unittest.TestCase):
    def test_discovers_git_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            r1 = _make_git_repo(base, "repo1")
            _make_git_repo(base, "repo2")
            (base / "nota" / "repo").mkdir(parents=True)
            agent = LocalProjectDiscoveryAgent(roots=(str(base),), max_depth=3,
                                               max_candidates=20, max_process_sample=5)
            git = agent.discover_git_projects()
            paths = {Path(g["path"]).name for g in git}
            self.assertIn("repo1", paths)
            self.assertIn("repo2", paths)
            # every candidate carries git metadata
            for g in git:
                self.assertIn("branch", g)
                self.assertIn("last_commit", g)
                self.assertEqual(g["kind"], "git")

    def test_discovery_skips_junk_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = _make_git_repo(base, "ok")
            (base / "node_modules" / "fake" / ".git").mkdir(parents=True)
            agent = LocalProjectDiscoveryAgent(roots=(str(base),), max_depth=5,
                                               max_candidates=20, max_process_sample=5)
            git = agent.discover_git_projects()
            paths = {g["path"] for g in git}
            self.assertIn(str(repo.resolve()), paths)
            self.assertFalse(any("node_modules" in p for p in paths))

    def test_process_discovery_privacy(self) -> None:
        """discover_processes must not raise and should be bounded; never leaks argv."""
        agent = LocalProjectDiscoveryAgent(max_process_sample=10)
        procs = agent.discover_processes()
        for p in procs:
            self.assertNotIn("argv", p)
            self.assertNotIn("env", p)
            self.assertIn("cwd", p)
            self.assertIn("pid", p)


class ProjectRegistryTests(unittest.TestCase):
    def test_register_list_unregister(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            p = store.register_monitored_project({"workspace": directory})
            self.assertEqual(p["workspace"], str(Path(directory).resolve()))
            self.assertEqual(p["kind"], "process")  # no .git
            self.assertEqual(len(store.list_monitored_projects()), 1)
            self.assertTrue(store.unregister_monitored_project(directory))
            self.assertEqual(len(store.list_monitored_projects()), 0)
            store.close()

    def test_register_git_project_resolves_base_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = _make_git_repo(base, "proj")
            store = StateStore(base / "s.sqlite3")
            p = store.register_monitored_project({"workspace": str(repo)})
            self.assertEqual(p["kind"], "git")
            self.assertIsNotNone(p["base_commit"])
            self.assertTrue(p["canonical_ref"])
            store.close()

    def test_register_requires_existing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            with self.assertRaises(ValueError):
                store.register_monitored_project({"workspace": "/nonexistent/xyz"})
            store.close()

    def test_status_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "s.sqlite3")
            store.register_monitored_project({"workspace": directory})
            store.update_monitored_project_status(directory, "watching")
            self.assertEqual(store.get_monitored_project(directory)["status"], "watching")
            store.close()


class ProjectMonitorTests(unittest.TestCase):
    def test_file_change_detection_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = _make_git_repo(base, "proj")
            store = StateStore(base / "s.sqlite3")
            store.register_monitored_project({"workspace": str(repo)})
            events = []

            def capture(payload):
                events.append(payload)
                return True
            mon = ProjectMonitor(store, post_event=capture, poll_interval=1.0,
                                 git_poll_interval=999)
            mon.start_project(str(repo))
            time.sleep(2)  # baseline
            (repo / "a.txt").write_text("changed\n", encoding="utf-8")
            time.sleep(3)  # poll
            mon.stop()
            self.assertTrue(any(e.get("event_type") == "file_change" for e in events),
                            f"expected file_change event, got {events}")
            self.assertEqual(store.get_monitored_project(str(repo))["status"], "watching")
            store.close()

    def test_store_ingest_path_when_no_post_event(self) -> None:
        """Default delivery writes straight to store.ingest (no HTTP loopback)."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = _make_git_repo(base, "proj")
            store = StateStore(base / "s.sqlite3")
            store.register_monitored_project({"workspace": str(repo)})
            mon = ProjectMonitor(store, poll_interval=1.0, git_poll_interval=999)
            mon.start_project(str(repo))
            time.sleep(2)
            (repo / "a.txt").write_text("changed2\n", encoding="utf-8")
            time.sleep(3)
            mon.stop()
            state = store.state()
            projects = state.get("projects", [])
            self.assertTrue(any(p["workspace"] == str(repo.resolve()) for p in projects),
                            "expected project row in state after ingest")
            store.close()


if __name__ == "__main__":
    unittest.main()
