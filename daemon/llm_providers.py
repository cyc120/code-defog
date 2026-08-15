"""Local, provider-neutral configuration for JSON-capable chat models.

The application runs as a localhost control plane, so provider credentials are
kept outside the repository and SQLite audit store.  The on-disk fallback is
restricted to the current user (directory ``0700``, file ``0600``); HTTP
responses expose only configuration status, never the secret itself.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from urllib.parse import urlparse

from agent_runtime.envfile import get_key

from . import paths


# ``hosts`` names the only endpoints a connection test may target while
# reusing a *stored or environment* API key.  A base_url override to any
# other host must supply its own explicit ``api_key``, otherwise a
# service-token holder could point a saved key at an arbitrary HTTPS host
# and exfiltrate it.  ``custom`` has no trusted host: its saved key may only
# be reused after the operator explicitly persists a base_url for it.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "json_mode": True,
        "legacy_env": "DEEPSEEK_API_KEY",
        "hosts": ("api.deepseek.com",),
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "json_mode": True,
        "hosts": ("api.openai.com",),
    },
    "ollama": {
        "name": "Ollama（本机）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "llama3.2",
        "json_mode": False,
        "hosts": ("127.0.0.1", "localhost", "::1"),
    },
    "custom": {
        "name": "自定义 OpenAI 兼容服务",
        "base_url": "https://example.invalid/v1",
        "model": "your-model",
        "json_mode": False,
        "hosts": (),
    },
}

_MAX_BASE_URL = 512
_MAX_MODEL = 160
_MAX_API_KEY = 2048


def _default_state() -> dict[str, Any]:
    return {"version": 1, "active_provider": "deepseek", "providers": {}}


def _preset_hosts(provider_id: str) -> set[str]:
    """Return the trusted endpoint hosts for a preset provider.

    These are the only hosts a connection test may target when it reuses a
    stored/environment key.  ``custom`` returns an empty set: it has no
    trusted host, so a saved custom key is never silently reused against an
    overridden base_url.
    """
    hosts = PROVIDER_PRESETS.get(provider_id, {}).get("hosts", ())
    return {str(host).strip().lower() for host in hosts if host}


def _legacy_deepseek_key() -> str:
    """Read the legacy project setting only when no saved key exists."""
    return get_key("DEEPSEEK_API_KEY", "")


class LLMProviderStore:
    """Own the local provider configuration and confidential API keys.

    The store deliberately has no dependency on ``StateStore``.  This keeps
    secrets out of the auditable SQLite database and makes all public views
    safe to send to the browser or via SSE.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        legacy_key_loader: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else paths.llm_provider_config_path()
        self._legacy_key_loader = legacy_key_loader or _legacy_deepseek_key
        self._lock = Lock()
        self._state = self._read_state()

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _default_state()
        if not isinstance(payload, dict):
            return _default_state()
        state = _default_state()
        active = payload.get("active_provider")
        if isinstance(active, str) and active in PROVIDER_PRESETS:
            state["active_provider"] = active
        providers = payload.get("providers")
        if isinstance(providers, dict):
            state["providers"] = {
                provider_id: value
                for provider_id, value in providers.items()
                if provider_id in PROVIDER_PRESETS and isinstance(value, dict)
            }
        return state

    def _write_state_locked(self) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _provider_id(value: Any) -> str:
        provider_id = value.strip().lower() if isinstance(value, str) else ""
        if provider_id not in PROVIDER_PRESETS:
            raise ValueError("unsupported provider_id")
        return provider_id

    @staticmethod
    def _base_url(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("base_url is required")
        base_url = value.strip().rstrip("/")
        if not base_url or len(base_url) > _MAX_BASE_URL:
            raise ValueError("base_url must be 1-512 characters")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("http base_url is only allowed for a local provider")
        return base_url

    @staticmethod
    def _model(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("model is required")
        model = " ".join(value.split())
        if not model or len(model) > _MAX_MODEL:
            raise ValueError("model must be 1-160 characters")
        return model

    @staticmethod
    def _api_key(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("api_key must be a string")
        key = value.strip()
        if not key or len(key) > _MAX_API_KEY:
            raise ValueError("api_key must be 1-2048 characters")
        return key

    def _stored_settings_locked(self, provider_id: str) -> dict[str, Any]:
        stored = self._state["providers"].get(provider_id)
        return dict(stored) if isinstance(stored, dict) else {}

    def _resolve_locked(
        self,
        provider_id: str,
        *,
        base_url: Any = None,
        model: Any = None,
        api_key: Any = None,
        api_key_supplied: bool = False,
    ) -> dict[str, Any]:
        preset = PROVIDER_PRESETS[provider_id]
        stored = self._stored_settings_locked(provider_id)
        resolved_base = self._base_url(
            base_url if base_url is not None else stored.get("base_url", preset["base_url"])
        )
        resolved_model = self._model(
            model if model is not None else stored.get("model", preset["model"])
        )
        stored_key = stored.get("api_key") if isinstance(stored.get("api_key"), str) else ""
        if api_key_supplied:
            resolved_key = self._api_key(api_key)
            key_source = "request"
        elif stored_key:
            resolved_key = stored_key
            key_source = "local"
        elif provider_id == "deepseek":
            resolved_key = str(self._legacy_key_loader() or "").strip()
            key_source = "environment" if resolved_key else "none"
        else:
            resolved_key = ""
            key_source = "none"
        return {
            "id": provider_id,
            "name": preset["name"],
            "base_url": resolved_base,
            "model": resolved_model,
            "json_mode": bool(preset.get("json_mode")),
            "api_key": resolved_key,
            "key_source": key_source,
        }

    @staticmethod
    def _public_selection(selection: dict[str, Any], active: bool) -> dict[str, Any]:
        source_labels = {
            "local": "本地受限配置",
            "environment": "环境变量兼容回退",
            "request": "本次输入",
            "none": "未配置",
        }
        return {
            "id": selection["id"],
            "name": selection["name"],
            "base_url": selection["base_url"],
            "model": selection["model"],
            "active": active,
            "configured": bool(selection["api_key"]),
            "key_source": source_labels.get(selection["key_source"], "未配置"),
        }

    def public_config(self) -> dict[str, Any]:
        """Return browser-safe provider state with no raw credentials."""
        with self._lock:
            active = self._state["active_provider"]
            providers = [
                self._public_selection(self._resolve_locked(provider_id), provider_id == active)
                for provider_id in PROVIDER_PRESETS
            ]
        return {"active_provider": active, "providers": providers}

    def resolve_active(self) -> dict[str, Any]:
        with self._lock:
            return self._resolve_locked(self._state["active_provider"])

    def resolve_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve a request-only candidate for connection testing.

        ``api_key`` from a test request is never written to disk.  When it is
        omitted the saved key (or legacy DeepSeek environment fallback) is
        used instead — but only against the provider's own preset host.  A
        base_url override to an arbitrary host requires an explicit key, so a
        service-token holder cannot point a stored secret at an attacker's
        endpoint.
        """
        provider_id = self._provider_id(payload.get("provider_id"))
        api_key_supplied = "api_key" in payload and str(payload.get("api_key") or "").strip() != ""
        if not api_key_supplied:
            with self._lock:
                stored = self._stored_settings_locked(provider_id)
                base_url = self._base_url(
                    payload.get("base_url")
                    if "base_url" in payload
                    else stored.get("base_url", PROVIDER_PRESETS[provider_id]["base_url"])
                )
                saved_host = (
                    (urlparse(stored["base_url"]).hostname or "").lower()
                    if stored.get("base_url") else ""
                )
            host = (urlparse(base_url).hostname or "").lower()
            allowed = _preset_hosts(provider_id)
            if saved_host:
                allowed = allowed | {saved_host}
            if host not in allowed:
                raise ValueError(
                    "connection test may reuse a stored key only against the "
                    "provider's own endpoint; supply an explicit api_key to "
                    "test a custom base_url"
                )
        with self._lock:
            return self._resolve_locked(
                provider_id,
                base_url=payload.get("base_url") if "base_url" in payload else None,
                model=payload.get("model") if "model" in payload else None,
                api_key=payload.get("api_key"),
                api_key_supplied=api_key_supplied,
            )

    @staticmethod
    def _host_of(base_url: str) -> str:
        try:
            parsed = urlparse(base_url)
        except ValueError:
            return ""
        return (parsed.hostname or "").strip().lower()

    def save_and_activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a provider selection.  Empty key input keeps the old key.

        Mirror of the connection-test guard (``resolve_candidate``): a stored
        key is never silently reused against a host outside the provider's
        preset hosts.  Repointing the endpoint to a foreign host therefore
        requires an explicit ``api_key`` for that host."""
        provider_id = self._provider_id(payload.get("provider_id"))
        clear_key = payload.get("clear_key") is True
        with self._lock:
            current = self._resolve_locked(provider_id)
            base_url = self._base_url(payload.get("base_url", current["base_url"]))
            model = self._model(payload.get("model", current["model"]))
            saved = self._stored_settings_locked(provider_id)
            api_key_supplied = bool(str(payload.get("api_key") or "").strip())
            new_host = self._host_of(base_url)
            old_host = self._host_of(current["base_url"])
            if (not api_key_supplied and not clear_key
                    and new_host != old_host
                    and new_host not in _preset_hosts(provider_id)):
                raise ValueError(
                    "changing the endpoint host requires an explicit api_key "
                    "for the new host; stored keys are never silently reused "
                    "outside the provider's trusted hosts",
                )
            saved["base_url"] = base_url
            saved["model"] = model
            if "api_key" in payload and str(payload.get("api_key") or "").strip():
                saved["api_key"] = self._api_key(payload["api_key"])
            elif clear_key:
                saved.pop("api_key", None)
            self._state["providers"][provider_id] = saved
            self._state["active_provider"] = provider_id
            self._write_state_locked()
        return self.public_config()
