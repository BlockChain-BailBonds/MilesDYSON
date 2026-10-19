"""Validation, durable state, and Markdown declarations for DYSON panels."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

_PATTERN = re.compile(r"^```dyson[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_KINDS = frozenset({"int", "float", "bool", "choice"})


class DysonError(ValueError):
    """Invalid declaration, value, or state format."""


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise DysonError(f"{label} must match {_ID_PATTERN.pattern}")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DysonError(f"{name} must be numeric, not {type(value).__name__}")
    if not math.isfinite(value):
        raise DysonError(f"{name} must be finite")
    return float(value)


def validate_value(control: dict[str, Any], value: Any) -> Any:
    """Validate and normalize a control value without coercing user input."""
    kind = control["type"]
    name = control["key"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise DysonError(f"{name} must be boolean")
        return value
    if kind == "choice":
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise DysonError(f"{name} has a non-scalar choice value")
        if value not in control["options"]:
            raise DysonError(f"{name} must be one of its declared options")
        return value
    numeric = _number(value, name)
    if kind == "int" and not isinstance(value, int):
        raise DysonError(f"{name} must be an integer")
    minimum = control["min"]
    maximum = control["max"]
    step = control["step"]
    if numeric < minimum - 1e-9 or numeric > maximum + 1e-9:
        raise DysonError(f"{name} is outside [{minimum}, {maximum}]")
    steps = (numeric - minimum) / step
    if abs(steps - round(steps)) > 1e-6:
        raise DysonError(f"{name} must align to step {step} from minimum {minimum}")
    return value


def validate_panel(spec: Any) -> dict[str, Any]:
    """Return a deep, normalized copy of the panel schema."""
    if not isinstance(spec, dict):
        raise DysonError("panel must be a JSON object")
    panel_id = _identifier(spec.get("id"), "panel id")
    title = spec.get("title", panel_id)
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise DysonError("panel title must contain 1–120 characters")
    controls = spec.get("controls")
    if not isinstance(controls, list) or not 1 <= len(controls) <= 128:
        raise DysonError("controls must contain between 1 and 128 entries")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in controls:
        if not isinstance(raw, dict):
            raise DysonError("each control must be an object")
        key = _identifier(raw.get("key"), "control key")
        if key in seen:
            raise DysonError(f"duplicate control key: {key}")
        seen.add(key)
        kind = raw.get("type")
        if kind not in _KINDS:
            raise DysonError(f"{key}: type must be one of {sorted(_KINDS)}")
        label = raw.get("label", key)
        if not isinstance(label, str) or not label.strip() or len(label) > 160:
            raise DysonError(f"{key}: invalid label")
        field: dict[str, Any] = {"key": key, "type": kind, "label": label}
        if kind in {"int", "float"}:
            minimum = _number(raw.get("min"), f"{key}.min")
            maximum = _number(raw.get("max"), f"{key}.max")
            step = _number(raw.get("step", 1 if kind == "int" else 0.01), f"{key}.step")
            if not (minimum < maximum and step > 0):
                raise DysonError(f"{key}: require min < max and step > 0")
            if kind == "int" and (not all(float(x).is_integer() for x in (minimum, maximum, step))):
                raise DysonError(f"{key}: integer slider bounds and step must be integral")
            field.update(min=int(minimum) if kind == "int" else minimum,
                         max=int(maximum) if kind == "int" else maximum,
                         step=int(step) if kind == "int" else step)
        elif kind == "choice":
            choices = raw.get("options")
            if not isinstance(choices, list) or not 1 <= len(choices) <= 128:
                raise DysonError(f"{key}: options must be a nonempty list")
            if any(isinstance(x, (list, dict)) for x in choices):
                raise DysonError(f"{key}: options must be scalar")
            if len({json.dumps(x, sort_keys=True) for x in choices}) != len(choices):
                raise DysonError(f"{key}: duplicate choice values")
            field["options"] = choices.copy()
        if "default" not in raw:
            raise DysonError(f"{key}: default is required")
        field["default"] = validate_value(field, raw["default"])
        validated.append(field)
    return {"id": panel_id, "title": title, "controls": validated}


def extract_panels(markdown: str) -> list[dict[str, Any]]:
    """Read JSON schemas from fenced dyson blocks inside Markdown text."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")
    panels = []
    for match in _PATTERN.finditer(markdown):
        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise DysonError(f"invalid DYSON JSON declaration at line {markdown.count(chr(10), 0, match.start())+1}: {exc}") from exc
        panels.append(validate_panel(raw))
    return panels


def default_state_path() -> Path:
    """Support writable Kaggle/Colab/Jupyter working directories."""
    raw = os.environ.get("DYSON_STATE_PATH")
    return Path(raw).expanduser() if raw else Path.cwd() / ".dyson" / "settings.json"


class StateStore:
    """Thread-safe single-process persistent state with atomic disk replacement."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_state_path()
        self._lock = threading.RLock()
        self._specs: dict[str, dict[str, Any]] = {}
        self._values: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DysonError(f"invalid DYSON settings file {self.path}: {exc}") from exc
        if not isinstance(obj, dict) or obj.get("version") != 1 or not isinstance(obj.get("values"), dict):
            raise DysonError("state must contain version=1 and an object named values")
        for panel_id, entries in obj["values"].items():
            _identifier(panel_id, "state panel id")
            if not isinstance(entries, dict):
                raise DysonError("state panel values must be objects")
        self._values = deepcopy(obj["values"])

    def register(self, spec: dict[str, Any]) -> dict[str, Any]:
        validated = validate_panel(spec)
        panel_id = validated["id"]
        with self._lock:
            current = self._specs.get(panel_id)
            if current is not None and current != validated:
                raise DysonError(f"panel id {panel_id} already registered with a different schema")
            self._specs[panel_id] = validated
            existing = self._values.get(panel_id, {})
            current_values: dict[str, Any] = {}
            for item in validated["controls"]:
                value = existing.get(item["key"], item["default"])
                try:
                    current_values[item["key"]] = validate_value(item, value)
                except DysonError:
                    current_values[item["key"]] = item["default"]
            self._values[panel_id] = current_values
            return deepcopy(current_values)

    def _control(self, panel_id: str, key: str) -> dict[str, Any]:
        spec = self._specs.get(panel_id)
        if spec is None:
            raise KeyError(f"unregistered panel {panel_id}; execute its panel cell first")
        for control in spec["controls"]:
            if control["key"] == key:
                return control
        raise KeyError(f"unknown control {panel_id}.{key}")

    def get(self, panel_id: str, key: str) -> Any:
        with self._lock:
            self._control(panel_id, key)
            return deepcopy(self._values[panel_id][key])

    def snapshot(self, panel_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if panel_id is None:
                return deepcopy(self._values)
            if panel_id not in self._specs:
                raise KeyError(f"unregistered panel {panel_id}")
            return deepcopy(self._values[panel_id])

    def set(self, panel_id: str, key: str, value: Any) -> None:
        with self._lock:
            control = self._control(panel_id, key)
            normalized = validate_value(control, value)
            before = self._values[panel_id][key]
            self._values[panel_id][key] = normalized
            try:
                self.save()
            except Exception:
                self._values[panel_id][key] = before
                raise

    def reset(self, panel_id: str) -> dict[str, Any]:
        with self._lock:
            if panel_id not in self._specs:
                raise KeyError(f"unregistered panel {panel_id}")
            previous = self._values[panel_id].copy()
            self._values[panel_id] = {c["key"]: c["default"] for c in self._specs[panel_id]["controls"]}
            try:
                self.save()
            except Exception:
                self._values[panel_id] = previous
                raise
            return self.snapshot(panel_id)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"version": 1, "values": self._values}, indent=2, sort_keys=True,
                              ensure_ascii=False, allow_nan=False) + "\n"
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=".dyson-", suffix=".tmp", delete=False) as handle:
                    temporary = handle.name
                    os.chmod(temporary, 0o600)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)
