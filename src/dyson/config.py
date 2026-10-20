"""Validated configuration and atomic, private local storage."""
from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "endpoint": "http://127.0.0.1:8080/v1",
    "model": "dyson-local",
    "temperature": 0.65,
    "top_p": 0.95,
    "max_tokens": 1024,
    "seed": -1,
    "timeout_s": 180.0,
    "equalizer_enabled": True,
    "evidence_window": 12,
    "min_observations": 4,
    "cooldown_requests": 3,
    "stall_streak": 3,
    "latency_target_s": 30.0,
    "min_tokens": 128,
    "progress_gain": 0.75,
    "economy_gain": 0.5,
}
RANGES = {
    "temperature": (0.0, 2.0), "top_p": (0.01, 1.0),
    "max_tokens": (1, 131072), "seed": (-1, 2147483647),
    "timeout_s": (1.0, 3600.0), "evidence_window": (3, 256),
    "min_observations": (3, 256), "cooldown_requests": (1, 100),
    "stall_streak": (2, 100), "latency_target_s": (0.1, 3600.0),
    "min_tokens": (1, 131072), "progress_gain": (0.1, 1.0),
    "economy_gain": (0.1, 1.0),
}
INTEGER_KEYS = {"max_tokens", "seed", "evidence_window", "min_observations",
                "cooldown_requests", "stall_streak", "min_tokens"}


class ConfigError(ValueError):
    """Invalid input; nothing was persisted."""


class ConflictError(ConfigError):
    """The requested revision is no longer current."""


def validate_endpoint(value):
    if not isinstance(value, str):
        raise ConfigError("endpoint must be a string")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ConfigError("invalid endpoint") from exc
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment
            or port is None or not 1 <= port <= 65535
            or url.path.rstrip("/") != "/v1"):
        raise ConfigError("endpoint must be a loopback HTTP URL with explicit port and /v1")
    return value.rstrip("/")


def validate(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ConfigError("configuration has missing or unknown fields")
    out = deepcopy(values)
    out["endpoint"] = validate_endpoint(out["endpoint"])
    model = out["model"]
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ConfigError("model must be a nonempty string of at most 256 characters")
    if type(out["equalizer_enabled"]) is not bool:
        raise ConfigError("equalizer_enabled must be boolean")
    for key, (low, high) in RANGES.items():
        value = out[key]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ConfigError(f"{key} must be finite and numeric")
        if key in INTEGER_KEYS and type(value) is not int:
            raise ConfigError(f"{key} must be an integer")
        if not low <= value <= high:
            raise ConfigError(f"{key} must be between {low} and {high}")
    if out["min_observations"] > out["evidence_window"]:
        raise ConfigError("min_observations must not exceed evidence_window")
    if out["min_tokens"] > out["max_tokens"]:
        raise ConfigError("min_tokens must not exceed max_tokens")
    return out


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".dyson-", delete=False) as handle:
            name = handle.name
            os.chmod(name, 0o600)
            json.dump(data, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


class Settings:
    """Single-writer store; Runtime owns the process and thread locks."""

    def __init__(self, directory):
        self.path = Path(directory) / "settings.json"
        self.revision = 0
        self.values = deepcopy(DEFAULTS)
        if self.path.exists():
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(obj, dict) or obj.get("schema") != 1
                    or type(obj.get("revision")) is not int or obj["revision"] < 0):
                raise ConfigError("invalid settings schema or revision")
            self.values = validate(obj.get("values"))
            self.revision = obj["revision"]

    def snapshot(self):
        return {"schema": 1, "revision": self.revision, "values": deepcopy(self.values)}

    def update(self, patch, expected_revision):
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise ConflictError("settings changed; reload before applying")
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise ConfigError("settings patch contains unknown fields")
        values = validate({**self.values, **patch})
        next_revision = self.revision + 1
        atomic_json(self.path, {"schema": 1, "revision": next_revision, "values": values})
        self.values, self.revision = values, next_revision
        return self.snapshot()
