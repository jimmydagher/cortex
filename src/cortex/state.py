"""Runtime state in <paths.state>/state.json: the power switch, the brain paths chosen in setup,
and hashed API keys. This is data Cortex changes while it runs, kept apart from config.

Raw API keys are shown once at creation and never stored; only their SHA-256 hash is.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import LayoutConfig, StateConfig
from .errors import NotFoundError, StartupError

STATE_FILE = "state.json"
KEY_PREFIX = "ctx_"
KEY_BYTES = 32
LABEL_MAX = 60


class Power(StrEnum):
    """The brain's switch: off makes every MCP tool answer 'off' for every client."""

    ON = "on"
    OFF = "off"


def now_utc() -> str:
    """The current UTC time as ISO 8601, to the second.

    Returns:
        e.g. `2026-09-26T19:36:35+00:00`.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def hash_key(raw: str) -> str:
    """SHA-256 of an API key; keys are high-entropy, so no salt or stretching is needed.

    Args:
        raw: the full API key.

    Returns:
        The hex digest.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class State:
    """state.json, read once at start and rewritten atomically on every change."""

    def __init__(self, state_dir: Path, config: StateConfig, layout: LayoutConfig) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / STATE_FILE
        self._lock = threading.RLock()
        self._flush_seconds = config.last_used_flush_seconds
        self._last_flush = 0.0
        initial: dict[str, Any] = {
            "power": config.initial_power,
            "power_changed": "",
            "cortex_path": "",
            "synapse_path": layout.synapse,
            "engram_path": layout.engram,
            "protected": [],
            "keys": [],
        }
        try:
            self.data = {**initial, **json.loads(self.path.read_text(encoding="utf-8"))}
        except FileNotFoundError:
            self.data = initial
        except ValueError as error:
            raise StartupError([f"{self.path} is not valid JSON ({error}); fix or remove it"]) from error
        self.data.pop("session_secret", None)  # moved to the cortex-session-key secret
        self.save()

    def save(self) -> None:
        """Write state.json atomically with owner-only permissions."""
        with self._lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):  # Windows development machines don't support POSIX modes
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            self._last_flush = time.monotonic()

    def get(self, key: str) -> Any:
        """Read one state value.

        Args:
            key: a state key such as `cortex_path`.

        Returns:
            The value (a missing key is a bug and raises KeyError).
        """
        return self.data[key]

    def update(self, **values: Any) -> None:
        """Change state values and save.

        Args:
            values: keys and their new values.
        """
        with self._lock:
            self.data.update(values)
            self.save()

    # ---------- power ----------

    @property
    def power(self) -> Power:
        """The current power switch."""
        return Power(self.data["power"])

    def set_power(self, power: Power) -> None:
        """Flip the switch and record when.

        Args:
            power: the new position.
        """
        self.update(power=power.value, power_changed=now_utc())

    # ---------- API keys ----------

    def create_key(self, label: str) -> tuple[dict[str, str], str]:
        """Create an API key for one client.

        Args:
            label: the client's name, shown in the GUI and the activity log.

        Returns:
            The stored record (without the hash) and the raw key, which is never stored.
        """
        raw = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
        record = {
            "id": secrets.token_hex(4),
            "label": label.strip()[:LABEL_MAX],
            "hash": hash_key(raw),
            "hint": raw[:8] + "…" + raw[-4:],
            "created": now_utc(),
            "last_used": "",
        }
        with self._lock:
            self.data["keys"].append(record)
            self.save()
        return {name: value for name, value in record.items() if name != "hash"}, raw

    def revoke_key(self, key_id: str) -> str:
        """Delete a key so its client loses access immediately.

        Args:
            key_id: the key's id.

        Returns:
            The revoked key's label.

        Raises:
            NotFoundError: no key has that id.
        """
        with self._lock:
            for record in self.data["keys"]:
                if record["id"] == key_id:
                    self.data["keys"].remove(record)
                    self.save()
                    return str(record["label"])
        raise NotFoundError(f"no API key with id {key_id}")

    def check_key(self, raw: str) -> dict[str, str] | None:
        """Match a presented bearer token against the stored hashes in constant time.

        Args:
            raw: the token from the Authorization header.

        Returns:
            The key record, or None when it matches no key.
        """
        if not raw.startswith(KEY_PREFIX):
            return None
        digest = hash_key(raw)
        match = None
        for record in self.data["keys"]:
            if hmac.compare_digest(record["hash"], digest):
                match = record
        if match:
            with self._lock:
                match["last_used"] = now_utc()
                if time.monotonic() - self._last_flush > self._flush_seconds:
                    self.save()
        return match

    def public_keys(self) -> list[dict[str, str]]:
        """Key records for the GUI, without hashes.

        Returns:
            One dict per key: id, label, hint, created, last_used.
        """
        return [{name: value for name, value in record.items() if name != "hash"} for record in self.data["keys"]]
