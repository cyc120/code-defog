from __future__ import annotations

import tempfile
import sys
from unittest.mock import patch
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import event_client  # noqa: E402
import update_worklog  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
