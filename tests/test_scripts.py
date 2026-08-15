from __future__ import annotations

import tempfile
import sys
from unittest.mock import patch
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import event_client  # noqa: E402
import scan_code_map  # noqa: E402
import update_worklog  # noqa: E402
import watch_worklog  # noqa: E402


class WorklogTests(unittest.TestCase):
    def test_conversation_id_prefers_codex_thread(self) -> None:
        with patch.dict(
            update_worklog.os.environ,
            {"CODEX_THREAD_ID": "thread-current", "CODEX_SESSION_ID": "session-fallback"},
            clear=False,
        ):
            self.assertEqual(update_worklog.conversation_id(), "thread-current")

    def test_conversation_id_has_default_for_manual_runs(self) -> None:
        with patch.dict(
            update_worklog.os.environ,
            {"CODEX_THREAD_ID": "", "CODEX_CONVERSATION_ID": "", "CODEX_SESSION_ID": ""},
            clear=False,
        ):
            self.assertEqual(update_worklog.conversation_id(), "default")

    def test_escaped_pipe_and_backslash_round_trip(self) -> None:
        value = r"C:\temp|source.py"
        encoded = update_worklog.escape_cell(value)
        self.assertEqual(update_worklog.split_escaped_values(encoded), [value])

    def test_table_parser_preserves_escaped_cell(self) -> None:
        lines = [
            "## 模块图谱",
            "",
            "| 模块 | 相关代码 | 职责 | 依赖 | 风险 | 怎么核对 |",
            "| --- | --- | --- | --- | --- | --- |",
            r"| 模块 | C:\temp\|src.py | 读取代码 | 暂无 | 暂无 | 打开文件 |",
            "",
            "## 流程图",
        ]
        rows = update_worklog.parse_table(lines, ["模块图谱"], 6)
        self.assertEqual(rows[0][1], r"C:\temp|src.py")

    def test_atomic_write_replaces_content_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AI_WORKLOG.md"
            path.write_text("旧内容", encoding="utf-8")
            update_worklog.atomic_write_text(path, "新内容")
            self.assertEqual(path.read_text(encoding="utf-8"), "新内容")
            self.assertEqual(list(path.parent.glob(".AI_WORKLOG.md.*.tmp")), [])

    def test_parse_table_keeps_malformed_rows(self) -> None:
        lines = [
            "## 涉及文件",
            "",
            "| 文件 | 用途 | 状态 |",
            "| --- | --- | --- |",
            "| a | 短行 |",
            "| b | 多|格 | 状态 | 额外 |",
            "",
            "## 流程图",
        ]
        rows = update_worklog.parse_table(lines, ["涉及文件"], 3)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ["a", "短行", ""])  # short row padded
        self.assertEqual(rows[1][:2], ["b", "多"])
        self.assertEqual(rows[1][2], "格|状态|额外")  # surplus folded into last cell

    def test_hand_edited_row_survives_full_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AI_WORKLOG.md"
            existing = (
                update_worklog.START
                + "\n# Code Defog\n\n最后更新：2026-08-02 10:00:00 CST\n状态：侦察中\n"
                "当前关注：准备查看项目上下文。\n\n## 涉及文件\n\n| 文件 | 用途 | 状态 |\n"
                "| --- | --- | --- |\n| src/normal.ts | 正常 | 修改中 |\n"
                "| src/hand_edited.ts | 手写行（缺状态列） |\n\n## 流程图\n\n- 收到目标\n"
                + update_worklog.END
            )
            path.write_text(existing, encoding="utf-8")
            worklog = update_worklog.load_worklog(path, "zh")
            worklog.files.append(update_worklog.split_row("src/new.ts|新文件|新增", 3))
            rendered = update_worklog.render_worklog(worklog, "2026-08-03 12:00:00 CST", "zh")
            updated = update_worklog.replace_section(path.read_text(encoding="utf-8"), rendered)
            self.assertIn("src/hand_edited.ts", updated)
            self.assertIn("src/normal.ts", updated)
            self.assertIn("src/new.ts", updated)

    def test_legacy_code_cctv_markers_migrate_to_code_defog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AI_WORKLOG.md"
            path.write_text(
                "<!-- code-cctv:start -->\n# Code CCTV\n\n状态：侦察中\n"
                "<!-- code-cctv:end -->\n",
                encoding="utf-8",
            )
            worklog = update_worklog.load_worklog(path, "zh")
            rendered = update_worklog.render_worklog(worklog, "2026-08-03 12:00:00 CST", "zh")
            updated = update_worklog.replace_section(path.read_text(encoding="utf-8"), rendered)
            self.assertIn(update_worklog.START, updated)
            self.assertNotIn(update_worklog.LEGACY_CODE_CCTV_START, updated)
            self.assertIn("# Code Defog", updated)

    def test_single_line_collapses_newlines(self) -> None:
        self.assertEqual(update_worklog.single_line("a\n\nb\nc"), "a b c")

    def test_render_flow_sanitizes_newline_in_status(self) -> None:
        rendered = update_worklog.render_flow("验证中\n## 注入", "zh")
        self.assertNotIn("\n## 注入", rendered)
        self.assertIn("验证中 ## 注入", rendered)

    def test_render_worklog_sanitizes_newline_in_status_and_focus(self) -> None:
        worklog = update_worklog.Worklog(status="侦察中\n\n## 注入", focus="专注\n恶意行", final_summary="待完成。")
        rendered = update_worklog.render_worklog(worklog, "t", "zh")
        self.assertNotIn("\n## 注入", rendered)
        self.assertIn("状态：侦察中", rendered)
        self.assertNotIn("\n恶意行", rendered)

    def test_event_client_bad_port_returns_false(self) -> None:
        with patch.object(
            event_client, "load_config", return_value={"host": "127.0.0.1", "port": "not-a-number", "token": "t"}
        ):
            with patch.object(event_client, "urlopen", side_effect=AssertionError("urlopen must not run")):
                self.assertFalse(event_client.post_event({"workspace": "/tmp/x"}))

    def test_event_client_daemon_down_returns_false(self) -> None:
        with patch.object(
            event_client, "load_config", return_value={"host": "127.0.0.1", "port": 1, "token": "t"}
        ):
            with patch.object(event_client, "urlopen", side_effect=OSError("connection refused")):
                self.assertFalse(event_client.post_event({"workspace": "/tmp/x"}))


class WatchWorklogEngineTests(unittest.TestCase):
    """The change-detection engine used by daemon/project_monitor — it had
    zero tests despite being production code."""

    def _worktree(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "a.txt").write_text("one", encoding="utf-8")
        (root / "b.txt").write_text("two", encoding="utf-8")
        return directory, root

    def test_snapshot_and_diff_detect_add_modify_delete(self) -> None:
        directory, root = self._worktree()
        try:
            state_file = Path(directory.name) / "state.json"
            first = watch_worklog.snapshot(root, "AI_WORKLOG.md", state_file)
            self.assertEqual(set(first), {"a.txt", "b.txt"})

            # add + modify + delete
            (root / "c.txt").write_text("three", encoding="utf-8")
            (root / "a.txt").write_text("one-v2", encoding="utf-8")
            (root / "b.txt").unlink()
            second = watch_worklog.snapshot(root, "AI_WORKLOG.md", state_file)
            changes = watch_worklog.diff_snapshots(first, second)
            states = {c.path: c.state for c in changes}
            self.assertEqual(states["c.txt"], "已新增")
            self.assertEqual(states["a.txt"], "已修改")
            self.assertEqual(states["b.txt"], "已删除")
        finally:
            directory.cleanup()

    def test_size_and_mtime_preserving_rewrite_is_detected(self) -> None:
        """Same-size rewrite with restored mtime must still register via
        ctime/inode (previously invisible with mtime+size only)."""
        import os as _os
        import time as _time
        directory, root = self._worktree()
        try:
            state_file = Path(directory.name) / "state.json"
            target = root / "a.txt"
            before = target.stat()
            first = watch_worklog.snapshot(root, "AI_WORKLOG.md", state_file)
            # Same bytes, same size; restore the mtime to hide the rewrite.
            _os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            target.write_text("one", encoding="utf-8")
            _os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = watch_worklog.snapshot(root, "AI_WORKLOG.md", state_file)
            changes = watch_worklog.diff_snapshots(first, second)
            # ctime_ns always advances on write even when mtime is restored.
            self.assertIn("a.txt", [c.path for c in changes])
        finally:
            directory.cleanup()

    def test_save_state_is_atomic_and_readable(self) -> None:
        directory, root = self._worktree()
        try:
            state_file = Path(directory.name) / "state.json"
            snapshot = watch_worklog.snapshot(root, "AI_WORKLOG.md", state_file)
            watch_worklog.save_state(state_file, root, snapshot)
            loaded = watch_worklog.load_state(state_file)
            self.assertEqual(loaded, snapshot)
            # No temp litter left behind.
            self.assertEqual([p.name for p in Path(directory.name).glob("*.tmp")], [])
        finally:
            directory.cleanup()

    def test_run_once_keeps_snapshot_when_update_fails(self) -> None:
        """A failed worklog update must NOT advance the snapshot: the change
        has to be retried, not lost (watch_worklog.py:246 regression)."""
        directory, root = self._worktree()
        try:
            state_file = Path(directory.name) / "state.json"
            from argparse import Namespace
            args = Namespace(
                workspace=str(root), once=True, quiet=True,
                file=watch_worklog.DEFAULT_WORKLOG,
                language=watch_worklog.DEFAULT_LANGUAGE,
                interval=watch_worklog.DEFAULT_INTERVAL,
                no_start_note=True,
                max_files_per_note=watch_worklog.DEFAULT_MAX_FILES_PER_NOTE,
                state_file=None,
            )
            # Baseline first.
            watch_worklog.run_once(args, root, state_file)
            (root / "a.txt").write_text("one-v2", encoding="utf-8")
            with patch.object(watch_worklog, "update_worklog_safe", return_value=False):
                watch_worklog.run_once(args, root, state_file)
            # Snapshot must still describe the ORIGINAL tree.
            state = watch_worklog.load_state(state_file)
            self.assertEqual(state["a.txt"]["size"], 3, "a.txt is now 5 bytes but snapshot advanced")
        finally:
            directory.cleanup()


class ScanCodeMapTests(unittest.TestCase):
    def test_workspace_under_skip_named_dir_is_not_skipped(self) -> None:
        """SKIP_DIRS matches relative path parts only; a workspace itself
        living under build/ or dist/ must still be scanned."""
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build"
            build.mkdir()
            (build / "app.py").write_text("def f():\n    pass\n", encoding="utf-8")
            (build / "node_modules").mkdir()
            (build / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
            files = scan_code_map.iter_files([str(build)], {".py"})
            names = [f.name for f in files]
            self.assertIn("app.py", names)
            self.assertNotIn("pkg.js", names)


if __name__ == "__main__":
    unittest.main()
