"""Focused tests for AI review file previews and per-project quiet windows."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from unittest.mock import patch

from _helpers import start_server
from daemon.ai_review import MAX_TOTAL_BYTES, preview_changed_files
from daemon.store import StateStore


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Code Defog tests"], cwd=repo, check=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    return repo


class AiReviewPreviewTests(unittest.TestCase):
    def test_preview_returns_metadata_and_explains_skipped_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            (repo / "app.py").write_text("PRIVATE_SOURCE_SENTINEL = 2\n", encoding="utf-8")
            (repo / ".env").write_text("PRIVATE_SECRET_SENTINEL=abc\n", encoding="utf-8")

            preview = preview_changed_files(repo)

            self.assertEqual([item["path"] for item in preview["files"]], ["app.py"])
            self.assertEqual(preview["excluded"][0]["path"], ".env")
            serialized = json.dumps(preview)
            self.assertNotIn("PRIVATE_SOURCE_SENTINEL", serialized)
            self.assertNotIn("PRIVATE_SECRET_SENTINEL", serialized)

    def test_web_console_serves_split_assets_and_blocks_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            (root / "index.html").write_text('<link href="/ui/assets/app.css">', encoding="utf-8")
            (assets / "app.css").write_text("body { color: red; }", encoding="utf-8")
            (assets / "app.js").write_text("window.ready = true;", encoding="utf-8")
            store = StateStore(root / "state.db")
            server, base, _token = start_server(store, ui_dir=str(root))
            try:
                with urlopen(base + "/ui") as response:
                    self.assertIn("/ui/assets/app.css", response.read().decode("utf-8"))
                with urlopen(base + "/ui/assets/app.css") as response:
                    self.assertIn("text/css", response.headers["Content-Type"])
                    self.assertEqual(response.read().decode("utf-8"), "body { color: red; }")
                with urlopen(base + "/ui/assets/app.js") as response:
                    self.assertIn("javascript", response.headers["Content-Type"])
                    self.assertEqual(response.read().decode("utf-8"), "window.ready = true;")
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/ui/assets/%2e%2e%2findex.html")
                self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                store.close()

    def test_preview_keeps_individual_candidates_when_their_sum_exceeds_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            paths = []
            for index in range(6):
                path = repo / f"module_{index}.py"
                path.write_text("x" * 30_000, encoding="utf-8")
                paths.append(path.name)

            preview = preview_changed_files(repo)

            self.assertEqual({item["path"] for item in preview["files"]}, set(paths))
            self.assertGreater(preview["candidate_total_bytes"], MAX_TOTAL_BYTES)

    def test_manual_review_endpoint_rejects_over_limit_and_accepts_selected_subset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            paths = []
            for index in range(6):
                path = repo / f"module_{index}.py"
                path.write_text("x" * 30_000, encoding="utf-8")
                paths.append(path.name)
            store = StateStore(Path(directory) / "state.sqlite3")
            store.register_monitored_project({"workspace": str(repo), "kind": "git"})
            server, base, token = start_server(store)
            endpoint = f"{base}/api/projects/{quote(str(repo), safe='')}/code-reviews"

            def post(selected: list[str]) -> tuple[int, dict]:
                request = Request(
                    endpoint,
                    data=json.dumps({"paths": selected}).encode("utf-8"),
                    method="POST",
                    headers={"X-Code-Defog-Token": token, "Content-Type": "application/json"},
                )
                try:
                    with urlopen(request, timeout=3) as response:
                        return response.status, json.loads(response.read())
                except HTTPError as error:
                    return error.code, json.loads(error.read())

            try:
                status, error = post(paths)
                self.assertEqual(status, 409)
                self.assertIn("总量超过", error["error"])

                with patch.object(server, "start_code_review", return_value="run-selected") as start:
                    status, accepted = post(paths[:5])
                self.assertEqual(status, 202)
                self.assertEqual(accepted["run_id"], "run-selected")
                start.assert_called_once_with(str(repo.resolve()), paths[:5], trigger="manual")
            finally:
                server.shutdown()
                server.server_close()
                store.close()


class AutoReviewQuietWindowTests(unittest.TestCase):
    def test_configured_quiet_window_is_persisted_and_used_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database)
            store.register_monitored_project({"workspace": str(repo), "kind": "git"})
            store.set_auto_review_enabled(str(repo), True, "ollama", "signature", 300)
            store.note_auto_review_changes(str(repo), ["app.py"], changed_at=1_000)

            self.assertIsNone(store.claim_due_auto_review(str(repo), now=1_299))
            batch = store.claim_due_auto_review(str(repo), now=1_300)
            self.assertEqual(batch["changed_paths"], ["app.py"])
            self.assertIsNone(store.claim_due_auto_review(str(repo), now=1_600))
            store.close()

            reopened = StateStore(database)
            self.assertEqual(reopened.get_monitored_project(str(repo))["ai_review_quiet_seconds"], 300)
            reopened.close()

    def test_existing_database_migrates_missing_quiet_window_to_three_hour_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _make_repo(Path(directory))
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database)
            store.register_monitored_project({"workspace": str(repo), "kind": "git"})
            store.close()

            connection = sqlite3.connect(database)
            connection.execute("ALTER TABLE monitored_projects DROP COLUMN ai_review_quiet_seconds")
            connection.commit()
            connection.close()

            migrated = StateStore(database)
            project = migrated.get_monitored_project(str(repo))
            self.assertEqual(project["ai_review_quiet_seconds"], 10_800)
            migrated.close()


if __name__ == "__main__":
    unittest.main()
