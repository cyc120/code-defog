import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import render_report


class WordReportTests(unittest.TestCase):
    def setUp(self):
        self.review = {
            "review": {
                "repository": {"name": "sample-project"},
                "revision": {"head": "abc123"},
                "scope_summary": "Checked the main flow and its data source.",
                "coverage_notes": ["Browser interaction was not run."],
            },
            "checks": [{
                "command": "npm test",
                "status": "passed",
                "exit_code": 0,
                "summary": "12 checks passed.",
            }],
            "findings": [{
                "fingerprint": "sample-bug-001",
                "category": "content_rule_inconsistency",
                "severity": "P2",
                "title": "Displayed count differs from data",
                "status": "confirmed",
                "impact": "Users see a count larger than the number of available items.",
                "expected": "The page count should match the runtime data.",
                "actual": "The page says 10 while the runtime array contains 8.",
                "suggested_action": "Update the page count or add the missing items.",
                "evidence": [
                    {"path": "index.html", "line": 42, "observation": "The page says 10."},
                    {"path": "src/data.ts", "line": 19, "observation": "The runtime array has 8 entries."},
                ],
            }, {
                "fingerprint": "sample-lead-001",
                "category": "unverified_lead",
                "severity": "unrated",
                "title": "Live deployment differs from checkout",
                "status": "unverified",
                "impact": "A user-visible deployment issue has not been established.",
                "suggested_action": "Check the deployed revision and reproduce the workflow.",
                "evidence": [{"url": "https://example.test", "observation": "Markup differs."}],
            }],
        }

    def test_generates_valid_docx_with_all_findings_and_line_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "review.docx"
            render_report.write_docx(self.review, output)
            with ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                root = ET.fromstring(archive.read("word/document.xml"))
            text = "".join(node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
            self.assertIn("sample-project 代码审查报告", text)
            self.assertIn("index.html:42", text)
            self.assertIn("src/data.ts:19", text)
            self.assertIn("contains 8", text)
            self.assertIn("待核实", text)
            self.assertIn("npm test", text)

    def test_cli_defaults_to_docx_next_to_json(self):
        with tempfile.TemporaryDirectory() as temp:
            review_path = Path(temp) / "review.json"
            review_path.write_text(json.dumps(self.review), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "render_report.py"), str(review_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(review_path.with_suffix(".docx").is_file())


if __name__ == "__main__":
    unittest.main()
