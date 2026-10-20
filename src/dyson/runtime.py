"""Standalone runtime, process ownership, feedback provenance, and telemetry."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path

from .backend import LocalBackend
from .config import ConfigError, ConflictError, Settings
from .equalizer import Equalizer

IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class InstanceLock:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = (directory / "runtime.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                self.handle.write(b"0")
                self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise ConfigError("DYSON is already using this state directory; use its API or dashboard") from exc

    def close(self):
        if not self.handle.closed:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise ConfigError("messages must contain 1 to 256 messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
                "system", "user", "assistant", "tool"}:
            raise ConfigError("invalid message role")
        content = message.get("content")
        if content is None and message.get("role") == "assistant" and message.get("tool_calls"):
            continue
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not content:
            raise ConfigError("message content must be text or a nonempty multimodal list")
        for part in content:
            if not isinstance(part, dict):
                raise ConfigError("invalid multimodal content")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                continue
            image = part.get("image_url")
            if (part.get("type") == "image_url" and isinstance(image, dict)
                    and isinstance(image.get("url"), str)
                    and image["url"].startswith(("data:image/", "https://", "http://"))):
                continue
            raise ConfigError("supported content parts are text and image_url")


class Runtime:
    def __init__(self, directory, backend=None):
        self.directory = Path(directory).expanduser().resolve()
        self.owner = InstanceLock(self.directory)
        try:
            self.settings = Settings(self.directory)
            self.equalizer = Equalizer(self.settings.values)
            self.backend = backend or LocalBackend()
            self.lock = threading.RLock()
            self.inference_lock = threading.Lock()
            self.pending = OrderedDict()
            self.events = deque(maxlen=100)
            self.closed = False
        except BaseException:
            self.owner.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            self.owner.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def log(self, event, **fields):
        row = {"time_unix": time.time(), "event": event, **fields}
        with self.lock:
            path = self.directory / "events.jsonl"
            # Rotate bounded telemetry; prompts and responses are never written here.
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                os.replace(path, self.directory / "events.previous.jsonl")
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, allow_nan=False, sort_keys=True) + "\n")
            self.events.append(row)

    def status(self):
        with self.lock:
            return {"version": "0.2.0", "settings": self.settings.snapshot(),
                    "sessions": self.equalizer.snapshot(), "events": list(self.events),
                    "pending_feedback": len(self.pending)}

    def configure(self, patch, revision):
        with self.lock:
            result = self.settings.update(patch, revision)
            self.equalizer = Equalizer(self.settings.values)
            self.pending.clear()
            self.log("settings_updated", revision=result["revision"])
            return result

    def health(self):
        with self.lock:
            settings = dict(self.settings.values)
        return self.backend.health(settings)

    def chat(self, messages, session_id="default", tools=None):
        validate_messages(messages)
        if not isinstance(session_id, str) or not IDENTIFIER.fullmatch(session_id):
            raise ConfigError("invalid session_id")
        if tools is not None and (not isinstance(tools, list) or len(tools) > 128
                                  or any(not isinstance(t, dict) for t in tools)):
            raise ConfigError("tools must be a list of at most 128 tool definitions")
        # One request at a time prevents GPU overload and makes request ordering explicit.
        if not self.inference_lock.acquire(blocking=False):
            raise ConflictError("inference is busy; retry when the current request finishes")
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        try:
            with self.lock:
                settings = dict(self.settings.values)
                revision = self.settings.revision
                params = self.equalizer.parameters(session_id)
                mode = self.equalizer.state(session_id).mode
            response = self.backend.complete(settings, messages, params, tools)
            elapsed = time.monotonic() - started
            usage = response.get("usage")
            # Retain only authentic nonnegative integer usage; unknown stays null.
            usage = {key: value for key, value in usage.items()
                     if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                     and type(value) is int and value >= 0} if isinstance(usage, dict) else None
            with self.lock:
                if revision == self.settings.revision:
                    self.equalizer.response(session_id, elapsed)
                    while len(self.pending) >= 256:
                        self.pending.popitem(last=False)
                    self.pending[request_id] = {"session_id": session_id, "revision": revision}
                self.log("model_response", request_id=request_id, session_id=session_id,
                         revision=revision, elapsed_s=elapsed, parameters=params, mode=mode,
                         usage=usage, finish_reason=response["choices"][0].get("finish_reason"))
            return {"request_id": request_id, "session_id": session_id,
                    "settings_revision": revision, "parameters": params, "mode": mode,
                    "elapsed_s": elapsed, "usage": usage,
                    "message": response["choices"][0]["message"],
                    "finish_reason": response["choices"][0].get("finish_reason")}
        except Exception as exc:
            self.log("model_error", request_id=request_id, session_id=session_id,
                     error_type=type(exc).__name__, elapsed_s=time.monotonic() - started)
            raise
        finally:
            self.inference_lock.release()

    def feedback(self, request_id, changed=None, completed=False, level=None):
        if changed is not None and type(changed) is not bool:
            raise ConfigError("changed must be true, false, or null")
        if type(completed) is not bool:
            raise ConfigError("completed must be boolean")
        if level is not None and (not isinstance(level, str) or not IDENTIFIER.fullmatch(level)):
            raise ConfigError("level must be a short identifier")
        if not isinstance(request_id, str):
            raise ConfigError("request_id must be a string")
        with self.lock:
            record = self.pending.get(request_id)
            if record is None:
                raise ConflictError("request is unknown, expired, already observed, or settings changed")
            transition = self.equalizer.observe(record["session_id"], changed, completed, level)
            # Newer feedback supersedes any older outstanding request in this session.
            # This prevents delayed outcomes from reversing the observed level order.
            for key in list(self.pending):
                item = self.pending.pop(key) if self.pending[key]["session_id"] == record["session_id"] else None
                if key == request_id:
                    break
                if item is not None:
                    self.log("feedback_superseded", request_id=key, session_id=record["session_id"])
            self.log("observed_outcome", request_id=request_id,
                     session_id=record["session_id"], changed=changed, completed=completed,
                     level=level, transition=transition, provenance="CALLER_REPORTED_OUTCOME")
            return {"transition": transition, "sessions": self.equalizer.snapshot()}
