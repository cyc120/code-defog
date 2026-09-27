"""Local-diff review hints use code changes, not unsupported AI assertions."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from daemon.change_review import review_local_changes


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


class ChangeReviewTests(unittest.TestCase):
    def test_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(review_local_changes(directory)["status"], "not_git")

    def test_only_changed_function_gets_process_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test")
            source = root / "worker.py"
            source.write_text(
                "def unchanged():\n    return 1\n\n"
                "def run():\n    return 2\n", encoding="utf-8",
            )
            _git(root, "add", "worker.py")
            _git(root, "commit", "-qm", "baseline")
            self.assertEqual(review_local_changes(root)["status"], "no_changes")
            source.write_text(
                "import subprocess\n\n"
                "def unchanged():\n    return 1\n\n"
                "def run():\n    child = subprocess.Popen(['worker'])\n"
                "    return child.communicate(timeout=1)\n", encoding="utf-8",
            )
            result = review_local_changes(root)
            self.assertEqual(result["status"], "ready")
            self.assertEqual([item["function"] for item in result["functions"]], ["run"])
            self.assertEqual([item["kind"] for item in result["hypotheses"]], ["process_tree"])
            self.assertEqual(result["hypotheses"][0]["status"], "unverified")

    def test_untracked_python_file_is_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "-q")
            (root / "fresh.py").write_text(
                "import subprocess\ndef start():\n    return subprocess.Popen(['worker'])\n",
                encoding="utf-8",
            )
            result = review_local_changes(root)
            self.assertEqual(result["functions"][0]["function"], "start")
            self.assertEqual(result["hypotheses"][0]["kind"], "process_tree")


if __name__ == "__main__":
    unittest.main()
