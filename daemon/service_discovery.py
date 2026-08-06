"""Tokenless, loopback-only discovery for local Code CCTV services.

The dashboard cannot inspect local files or running processes from a browser.
This component gives the local dashboard a bounded registry to inspect instead:
each daemon writes one public descriptor after it binds, and discovery verifies
the descriptor against the daemon's public health endpoint before showing it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


DESCRIPTOR_SCHEMA_VERSION = 1
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_DESCRIPTORS = 32
MAX_PROBE_WORKERS = 8
INSTANCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")


def _endpoint_url(host: str, port: int, path: str = "") -> str:
    address = f"[{host}]" if ":" in host else host
    return f"http://{address}:{port}{path}"


class LocalServiceDiscoveryAgent:
    """Discover registered Code CCTV daemons without handling credentials."""

    def __init__(
        self,
        registry_dir: Path,
        legacy_config_path: Path | None = None,
        probe_timeout: float = 0.25,
    ) -> None:
        self.registry_dir = registry_dir.expanduser()
        self.legacy_config_path = legacy_config_path.expanduser() if legacy_config_path else None
        self.probe_timeout = max(0.01, float(probe_timeout))

    @staticmethod
    def is_loopback_host(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return ipaddress.ip_address(value.strip()).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _valid_port(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            port = int(value)
        except (TypeError, ValueError):
            return None
        return port if 1 <= port <= 65535 else None

    @staticmethod
    def _clean_label(value: object, host: str, port: int) -> str:
        if isinstance(value, str):
            label = " ".join(value.split())[:80]
            if label:
                return label
        return f"Code CCTV · {host}:{port}"

    @staticmethod
    def _valid_instance_id(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        return candidate if INSTANCE_ID_PATTERN.fullmatch(candidate) else None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file() or path.stat().st_size > MAX_DESCRIPTOR_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _candidate_from_registry(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("schema_version") != DESCRIPTOR_SCHEMA_VERSION:
            return None
        instance_id = self._valid_instance_id(payload.get("instance_id"))
        host = payload.get("host")
        port = self._valid_port(payload.get("port"))
        if not instance_id or not self.is_loopback_host(host) or port is None:
            return None
        host = str(host).strip()
        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            pid = None
        updated_at = payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else ""
        return {
            "id": instance_id,
            "label": self._clean_label(payload.get("display_name"), host, port),
            "host": host,
            "port": port,
            "ui_url": _endpoint_url(host, port, "/ui"),
            "status": "offline",
            "source": "registry",
            "pid": pid,
            "updated_at": updated_at,
            "_expected_instance_id": instance_id,
        }

    def _candidate_from_legacy_config(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        host = payload.get("host")
        port = self._valid_port(payload.get("port"))
        if not self.is_loopback_host(host) or port is None:
            return None
        host = str(host).strip()
        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            pid = None
        updated_at = payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else ""
        return {
            "id": f"legacy-{host.replace(':', '-')}-{port}",
            "label": self._clean_label(payload.get("display_name"), host, port),
            "host": host,
            "port": port,
            "ui_url": _endpoint_url(host, port, "/ui"),
            "status": "offline",
            "source": "legacy_config",
            "pid": pid,
            "updated_at": updated_at,
            "_expected_instance_id": None,
        }

    def _collect_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        try:
            files = sorted(self.registry_dir.glob("*.json"))[:MAX_DESCRIPTORS]
        except OSError:
            files = []
        for path in files:
            payload = self._read_json(path)
            if payload is None:
                continue
            candidate = self._candidate_from_registry(payload)
            if candidate is not None:
                candidates.append(candidate)
        if self.legacy_config_path is not None:
            payload = self._read_json(self.legacy_config_path)
            if payload is not None:
                legacy = self._candidate_from_legacy_config(payload)
                if legacy is not None:
                    candidates.append(legacy)

        # A registered descriptor has identity data and wins over the legacy
        # single-service config when both point to the same daemon.
        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for candidate in candidates:
            key = (str(candidate["host"]), int(candidate["port"]))
            current = unique.get(key)
            if current is None or current["source"] == "legacy_config":
                unique[key] = candidate
        return list(unique.values())

    def _probe(self, candidate: dict[str, Any]) -> str:
        try:
            request = Request(
                _endpoint_url(str(candidate["host"]), int(candidate["port"]), "/health"),
                headers={"Accept": "application/json"},
            )
            with urlopen(request, timeout=self.probe_timeout) as response:
                if response.status != 200:
                    return "offline"
                payload = json.loads(response.read(MAX_DESCRIPTOR_BYTES).decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return "offline"
        if not isinstance(payload, dict) or payload.get("ok") is not True or payload.get("service") != "code-cctv":
            return "offline"
        expected_instance_id = candidate["_expected_instance_id"]
        if expected_instance_id is not None:
            return "ready" if payload.get("instance_id") == expected_instance_id and payload.get("ui") is True else "offline"
        return "legacy" if payload.get("ui") is True else "offline"

    def discover(self) -> list[dict[str, Any]]:
        candidates = self._collect_candidates()
        if candidates:
            with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(candidates))) as pool:
                futures = {pool.submit(self._probe, candidate): candidate for candidate in candidates}
                for future in as_completed(futures):
                    candidate = futures[future]
                    try:
                        candidate["status"] = future.result()
                    except Exception:
                        candidate["status"] = "offline"
        for candidate in candidates:
            candidate.pop("_expected_instance_id", None)
        return sorted(
            candidates,
            key=lambda item: (
                0 if item["status"] == "ready" else 1 if item["status"] == "legacy" else 2,
                str(item["updated_at"]),
                str(item["label"]),
            ),
            reverse=False,
        )

    def register(self, descriptor: dict[str, Any]) -> Path:
        """Atomically publish a public descriptor for one running daemon."""
        candidate = self._candidate_from_registry({
            **descriptor,
            "schema_version": descriptor.get("schema_version", DESCRIPTOR_SCHEMA_VERSION),
        })
        if candidate is None:
            raise ValueError("invalid local service descriptor")
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": DESCRIPTOR_SCHEMA_VERSION,
            "instance_id": candidate["id"],
            "display_name": candidate["label"],
            "host": candidate["host"],
            "port": candidate["port"],
            "pid": candidate["pid"],
            "started_at": descriptor.get("started_at") if isinstance(descriptor.get("started_at"), str) else "",
            "updated_at": candidate["updated_at"],
        }
        target = self.registry_dir / f"{candidate['id']}.json"
        descriptor_handle, temporary_name = tempfile.mkstemp(
            prefix=f".{candidate['id']}.", suffix=".tmp", dir=self.registry_dir,
        )
        try:
            with os.fdopen(descriptor_handle, "w", encoding="utf-8", newline="") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        return target

    def unregister(self, instance_id: str) -> None:
        valid = self._valid_instance_id(instance_id)
        if valid is None:
            return
        try:
            (self.registry_dir / f"{valid}.json").unlink()
        except FileNotFoundError:
            return
