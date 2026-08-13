#!/usr/bin/env python3
"""Quality gate for the Code Defog demo.

Runs the complete test suite against a (possibly modified) cli.py and
exits 0 if all checks pass or non-zero if the gate fails.

Usage:
    python quality_gate.py <path_to_cli_dir>

Used by the Verification Agent to decide RELEASE_APPROVAL vs PATCH_REJECTED.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(cli_dir: Path, config_name: str, *args: str) -> subprocess.CompletedProcess:
    """Run cli.py in *cli_dir* with the given config and args.

    If *config_name* is an absolute path it is used directly;
    otherwise it is resolved relative to *cli_dir*.
    """
    config_path = Path(config_name)
    if not config_path.is_absolute():
        config_path = cli_dir / config_name
    return subprocess.run(
        [sys.executable, str(cli_dir / "cli.py"), str(config_path)] + list(args),
        capture_output=True, text=True, cwd=str(cli_dir),
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python quality_gate.py <path_to_cli_dir>", file=sys.stderr)
        return 2

    cli_dir = Path(sys.argv[1]).resolve()
    if not (cli_dir / "cli.py").exists():
        print(f"ERROR: cli.py not found in {cli_dir}", file=sys.stderr)
        return 2

    failures: list[str] = []

    # ── Check 1: Case A baseline — no 'projects' must NOT crash ──────────
    # The correct fix should handle missing 'projects' gracefully.
    result = run_cli(cli_dir, "no_projects.json", "--list")
    if result.returncode != 0:
        failures.append(
            f"CHECK 1 FAILED: no_projects.json should exit 0 after fix, "
            f"got {result.returncode}. stderr: {result.stderr.strip()}"
        )

    # ── Check 2: Case B regression — missing 'required_field' MUST fail ──
    # A partial config (projects but no required_field) must still be
    # rejected by validate_config.  The wrong fix would silently pass.
    # Write the test config to a system temp file so we never touch cli_dir.
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as tf:
        json.dump({"projects": ["a", "b"]}, tf)
        tf.flush()
        os.fsync(tf.fileno())
        partial_path = tf.name
    try:
        result = run_cli(cli_dir, partial_path, "--list")
    finally:
        Path(partial_path).unlink(missing_ok=True)
    if result.returncode == 0:
        failures.append(
            "CHECK 2 FAILED (REGRESSION): partial.json with missing "
            "required_field was accepted.  The quality gate must reject "
            "this patch because validate_config was bypassed."
        )

    # ── Check 3: valid config must still work ────────────────────────────
    result = run_cli(cli_dir, "valid_config.json", "--list")
    if result.returncode != 0:
        failures.append(
            f"CHECK 3 FAILED: valid_config.json should exit 0, "
            f"got {result.returncode}. stderr: {result.stderr.strip()}"
        )

    # ── Report ───────────────────────────────────────────────────────────
    if failures:
        print(f"QUALITY GATE FAILED — {len(failures)} check(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1

    print("QUALITY GATE PASSED — all checks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
