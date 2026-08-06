#!/usr/bin/env python3
"""Start the Code CCTV localhost service."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime.agentteams_preflight import inspect_agentteams_preflight
from agent_runtime.orchestrator import Orchestrator
from agent_runtime.teams_adapter import AgentScopeExecutionAdapter

from . import paths
from .server import CodeCCTVServer
from .service_discovery import LocalServiceDiscoveryAgent
from .store import StateStore

UI_DIR = Path(__file__).resolve().parent.parent / "web"


def _run_retrospective(store: StateStore, case_id: str) -> None:
    """Generate a retrospective off the hot path, best-effort."""
    from retrospective.retrospective import generate_retrospective

    try:
        generate_retrospective(store, case_id)
    except Exception:
        # Retrospective must never break the transition that triggered it.
        pass


DEFAULT_CONFIG = paths.config_path()
DEFAULT_STATE = paths.state_path()


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Code CCTV local background service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="Use 0 to select a free local port.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--runtime-mode", choices=("mock", "agentscope", "agentteams", "production"),
        default=os.environ.get("CODE_CCTV_RUNTIME_MODE", "mock"),
        help=(
            "Use agentscope for the local AgentScope runtime. agentteams is fail-closed "
            "until an external AgentTeams workflow bridge is configured. "
            "production is a legacy alias for agentscope."
        ),
    )
    parser.add_argument(
        "--agentteams-preflight", action="store_true",
        help="Inspect local AgentTeams prerequisites without starting a service or deployment.",
    )
    parser.add_argument(
        "--approval-key", default=os.environ.get("CODE_CCTV_APPROVAL_KEY", ""),
        help="Independent human approval key; prefer CODE_CCTV_APPROVAL_KEY over shell history.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.agentteams_preflight:
        report = inspect_agentteams_preflight()
        print(report.format_text(), file=sys.stderr)
        if not report.ready:
            raise SystemExit(2)
        return

    if args.runtime_mode == "agentteams":
        report = inspect_agentteams_preflight()
        print(report.format_text(), file=sys.stderr)
        if not report.ready:
            raise SystemExit(2)
        print(
            "AgentTeams workflow bridge: BLOCKED\n"
            "- [missing] control-plane/workflow bridge: no AgentTeams Team/Task/Handoff bridge is "
            "configured\n"
            "  Remedy: Configure the external AgentTeams control plane and workflow bridge before "
            "requesting agentteams mode. Code CCTV will not substitute AgentScope.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    token = secrets.token_urlsafe(32)
    instance_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    store = StateStore(args.state)
    teams = AgentScopeExecutionAdapter(store)
    if args.runtime_mode in ("agentscope", "production"):
        teams.set_mode(args.runtime_mode)
    orchestrator = Orchestrator(store, teams)
    discovery_agent = LocalServiceDiscoveryAgent(paths.service_registry_dir(), paths.config_path())
    approval_secret = args.approval_key or secrets.token_urlsafe(32)
    if not args.approval_key:
        print(
            "Code CCTV human approval key (keep private; not stored in service.json): "
            f"{approval_secret}",
            file=sys.stderr,
        )

    # Async retrospective on Case close — runs on a daemon thread so the
    # transition that triggered it is never blocked.
    store.retrospective_hook = lambda case_id: threading.Thread(
        target=_run_retrospective, args=(store, case_id), daemon=True,
    ).start()

    server = CodeCCTVServer(
        (args.host, args.port), token, store, orchestrator,
        ui_dir=str(UI_DIR), discovery_agent=discovery_agent, instance_id=instance_id,
        approval_secret=approval_secret, runtime_mode=teams.mode,
    )
    address, port = server.server_address
    descriptor_registered = False
    try:
        if discovery_agent.is_loopback_host(address):
            discovery_agent.register({
                "instance_id": instance_id,
                "display_name": f"Code CCTV DevLoop · {instance_id[:8]}",
                "host": address,
                "port": port,
                "pid": os.getpid(),
                "started_at": started_at,
                "updated_at": started_at,
            })
            descriptor_registered = True
        write_json(
            args.config.expanduser().resolve(),
            {
                "host": address,
                "port": port,
                "token": token,
                "state_path": str(args.state.expanduser().resolve()),
                "pid": os.getpid(),
                "instance_id": instance_id,
                "updated_at": started_at,
            },
        )

        stopping = threading.Event()

        def stop(*_signals: object) -> None:
            if stopping.is_set():
                return
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=0.25)
    finally:
        if descriptor_registered:
            discovery_agent.unregister(instance_id)
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
