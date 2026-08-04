"""Demo target test suite — isolation-based tests that actually modify cli.py.

Three clearly separated scenarios, each runs in a temp copy of the
demo_target source:

Scenario 1 (BUG_BASELINE):  Original buggy code — tests that FAIL to
    document the Case A crash (KeyError on empty config).

Scenario 2 (CORRECT_FIX):  Apply the correct fix to the copy, then
    verify the fix works and validate_config is preserved.

Scenario 3 (WRONG_FIX):  Apply the wrong fix to the copy, then
    verify the regression is detected by the quality gate.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).parent


def _copy_demo_to(tmp_path: Path) -> Path:
    """Copy cli.py and test_config.py to an isolated temp directory."""
    dest = tmp_path / "demo_isolated"
    dest.mkdir()
    shutil.copy(DEMO_DIR / "cli.py", dest / "cli.py")
    # Copy config samples too
    for cfg in ["valid_config.json", "empty_config.json"]:
        src = DEMO_DIR / cfg
        if src.exists():
            shutil.copy(src, dest / cfg)
    return dest


def _run_cli(isolated: Path, config_file: str, *args) -> subprocess.CompletedProcess:
    """Run the (possibly modified) cli.py in the isolated directory."""
    return subprocess.run(
        [sys.executable, str(isolated / "cli.py"), str(isolated / config_file)] + list(args),
        capture_output=True, text=True, cwd=str(isolated),
    )


def _apply_fix(isolated: Path, old: str, new: str) -> None:
    """Apply a string replacement to cli.py in the isolated directory."""
    cli_path = isolated / "cli.py"
    content = cli_path.read_text(encoding="utf-8")
    assert old in content, f"Expected pattern not found in cli.py:\n{old}"
    content = content.replace(old, new)
    cli_path.write_text(content, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 1 — BUG_BASELINE: original (unfixed) code
# ═══════════════════════════════════════════════════════════════════════════

class TestBugBaseline:
    """Run the ORIGINAL buggy cli.py.  These tests FAIL because the bug
    is real — they demonstrate the Case A evidence."""

    def test_no_projects_crashes_with_keyerror(self, tmp_path):
        """Case A canonical input: config has required_field but no 'projects' key.
        The original code crashes with KeyError because of config['projects']."""
        isolated = _copy_demo_to(tmp_path)
        shutil.copy(DEMO_DIR / "no_projects.json", isolated / "no_projects.json")
        result = _run_cli(isolated, "no_projects.json", "--list")
        assert result.returncode != 0
        assert "KeyError" in result.stderr or "missing config key" in result.stderr.lower()

    def test_valid_config_works_on_original(self, tmp_path):
        isolated = _copy_demo_to(tmp_path)
        shutil.copy(DEMO_DIR / "valid_config.json", isolated / "valid_config.json")
        result = _run_cli(isolated, "valid_config.json", "--list")
        assert result.returncode == 0
        assert "project-alpha" in result.stdout


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 2 — CORRECT_FIX: modifiy cli.py in isolation, verify the fix
# ═══════════════════════════════════════════════════════════════════════════

CORRECT_FIX = (
    'return {\n'
    '        "projects": config["projects"],\n'
    '        "required_field": config["required_field"],\n'
    '    }'
)
CORRECT_FIX_REPLACEMENT = (
    'return {\n'
    '        "projects": config.get("projects", []),\n'
    '        "required_field": config.get("required_field"),\n'
    '    }'
)


class TestCorrectFix:
    """Apply the CORRECT fix to an isolated copy and verify it works."""

    def test_fix_returns_empty_list_on_no_projects(self, tmp_path):
        """Case A expected behavior after correct fix: no 'projects' key
        → defaults to empty list, exit 0 (required_field IS present)."""
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, CORRECT_FIX_REPLACEMENT)
        shutil.copy(DEMO_DIR / "no_projects.json", isolated / "no_projects.json")
        result = _run_cli(isolated, "no_projects.json", "--list")
        assert result.returncode == 0
        # Empty output — no projects configured

    def test_fix_preserves_validate_config(self, tmp_path):
        """The correct fix must NOT weaken validate_config.  Missing
        required_field must still raise ConfigError."""
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, CORRECT_FIX_REPLACEMENT)
        partial = isolated / "partial.json"
        partial.write_text(json.dumps({"projects": ["a", "b"]}))
        result = _run_cli(isolated, "partial.json", "--list")
        assert result.returncode != 0

    def test_valid_config_still_works_after_fix(self, tmp_path):
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, CORRECT_FIX_REPLACEMENT)
        shutil.copy(DEMO_DIR / "valid_config.json", isolated / "valid_config.json")
        result = _run_cli(isolated, "valid_config.json", "--list")
        assert result.returncode == 0
        assert "project-alpha" in result.stdout


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 3 — WRONG_FIX: apply the over-aggressive fix, detect regression
# ═══════════════════════════════════════════════════════════════════════════

WRONG_FIX_REPLACEMENT = (
    'return {\n'
    '        "projects": config.get("projects", []),\n'
    '        "required_field": config.get("required_field", "enabled"),\n'
    '    }'
)


class TestWrongFix:
    """Apply the WRONG fix to an isolated copy and verify the quality
    gate would intercept the regression."""

    def test_wrong_fix_silences_required_field_validation(self, tmp_path):
        """The wrong fix makes empty config work BUT silently skips
        required_field validation — this is the regression the quality
        gate must detect (Case B)."""
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, WRONG_FIX_REPLACEMENT)
        # partial.json: has projects but NO required_field
        partial = isolated / "partial.json"
        partial.write_text(json.dumps({"projects": ["a", "b"]}))
        result = _run_cli(isolated, "partial.json", "--list")
        # The WRONG fix provides a default for required_field,
        # so validation passes.  This is the regression.
        assert result.returncode == 0, (
            "REGRESSION: wrong fix passed validation silently. "
            "The quality gate should have rejected this patch."
        )

    def test_wrong_fix_passes_superficial_case_a_tests(self, tmp_path):
        """The wrong fix DOES pass the Case A baseline tests (empty config
        no longer crashes).  This demonstrates why the quality gate needs
        BOTH the original test suite AND regression-specific tests."""
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, WRONG_FIX_REPLACEMENT)
        shutil.copy(DEMO_DIR / "empty_config.json", isolated / "empty_config.json")
        result = _run_cli(isolated, "empty_config.json", "--list")
        assert result.returncode == 0
        # Superficially looks correct — but test_wrong_fix_silences_*
        # reveals the hidden regression


# ═══════════════════════════════════════════════════════════════════════════
# Quality Gate integration — drives Verification Agent state transitions
# ═══════════════════════════════════════════════════════════════════════════

class TestQualityGate:
    """The quality_gate.py script is what the Verification Agent calls.
    It must exit 0 on the correct fix and exit non-zero on the wrong fix,
    providing the signal for PATCH_REJECTED."""

    def test_quality_gate_passes_on_correct_fix(self, tmp_path):
        """Correct fix: quality gate exits 0 → RELEASE_APPROVAL path."""
        import subprocess
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, CORRECT_FIX_REPLACEMENT)
        shutil.copy(DEMO_DIR / "no_projects.json", isolated / "no_projects.json")
        shutil.copy(DEMO_DIR / "valid_config.json", isolated / "valid_config.json")
        result = subprocess.run(
            [sys.executable, str(DEMO_DIR / "quality_gate.py"), str(isolated)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"Quality gate should PASS on correct fix. "
            f"stderr: {result.stderr.strip()}"
        )

    def test_quality_gate_fails_on_wrong_fix(self, tmp_path):
        """Wrong fix: quality gate exits non-zero → PATCH_REJECTED path.
        This is the Case B trigger that lets Verification Agent reject."""
        import subprocess
        isolated = _copy_demo_to(tmp_path)
        _apply_fix(isolated, CORRECT_FIX, WRONG_FIX_REPLACEMENT)
        shutil.copy(DEMO_DIR / "no_projects.json", isolated / "no_projects.json")
        shutil.copy(DEMO_DIR / "valid_config.json", isolated / "valid_config.json")
        result = subprocess.run(
            [sys.executable, str(DEMO_DIR / "quality_gate.py"), str(isolated)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, (
            f"Quality gate MUST fail on wrong fix (regression). "
            f"Got exit {result.returncode}, stderr: {result.stderr.strip()}"
        )

    def test_quality_gate_fails_on_unfixed_code(self, tmp_path):
        """Original (unfixed) code: quality gate should also fail
        because no_projects.json still crashes with KeyError."""
        import subprocess
        isolated = _copy_demo_to(tmp_path)
        shutil.copy(DEMO_DIR / "no_projects.json", isolated / "no_projects.json")
        shutil.copy(DEMO_DIR / "valid_config.json", isolated / "valid_config.json")
        result = subprocess.run(
            [sys.executable, str(DEMO_DIR / "quality_gate.py"), str(isolated)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0, (
            f"Quality gate should fail on unfixed code (KeyError). "
            f"Got exit {result.returncode}, stderr: {result.stderr.strip()}"
        )
