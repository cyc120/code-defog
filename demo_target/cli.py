# Demo Target — sample buggy Python CLI for DevLoop case demonstrations.
#
# This is a deliberately buggy tool used to generate the defect scenarios
# described in the project framework (Section 3.3):
#
#   Case A: empty config → KeyError crash
#   Case B: over-aggressive fix silences validation errors
#
# DO NOT use this code as a reference for production patterns.

import json
import sys
from pathlib import Path


def load_config(path: str) -> dict:
    """Load a JSON config file.

    BUG (Case A): uses direct key access instead of .get() with defaults.
    """
    with open(path) as f:
        config = json.load(f)
    # Bug: KeyError when 'projects' key is missing
    return {
        "projects": config["projects"],
        "required_field": config["required_field"],
    }


def list_projects(config: dict) -> list[str]:
    return config.get("projects", [])


def validate_config(config: dict) -> None:
    """Validate that required fields are present.

    This must raise ConfigError when required_field is missing.
    """
    if "required_field" not in config or not config["required_field"]:
        raise ConfigError("required_field is missing or empty")


class ConfigError(Exception):
    pass


def main():
    if len(sys.argv) < 2:
        print("Usage: python cli.py <config.json> [--list]", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]
    cmd = sys.argv[2] if len(sys.argv) > 2 else "--list"

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        # This is the Case A bug — uncaught KeyError becomes a crash
        print(f"Error: missing config key: {e}", file=sys.stderr)
        sys.exit(1)

    if cmd == "--list":
        # Case B: validate_config should be called before listing,
        # but a bad fix might skip validation
        validate_config(config)
        projects = list_projects(config)
        for p in projects:
            print(p)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
