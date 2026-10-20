"""Small local API adapter usable by ARC, notebooks, or any Python application."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .backend import BackendError, NoRedirect


class DysonClient:
    def __init__(self, port=9180, *, token=None, token_file=None, timeout=3600):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if token is None:
            if token_file is None:
                raise ValueError("provide token or token_file")
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("API token cannot be empty")
        self.base_url = f"http://127.0.0.1:{port}/api"
        self.token, self.timeout = token, timeout

    def call(self, path, payload=None):
        if path not in {"status", "health", "config", "chat", "feedback"}:
            raise ValueError("unknown DYSON API route")
        request = Request(self.base_url + "/" + path,
                          data=None if payload is None else json.dumps(
                              payload, allow_nan=False).encode(),
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(
                    request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            raise BackendError(f"DYSON HTTP {exc.code}: {detail}") from exc

    def chat(self, messages, session_id="default", tools=None):
        payload = {"messages": messages, "session_id": session_id}
        if tools is not None:
            payload["tools"] = tools
        return self.call("chat", payload)

    def observe(self, request_id, changed=None, completed=False, level=None):
        return self.call("feedback", {"request_id": request_id, "changed": changed,
                                     "completed": completed, "level": level})

    def status(self):
        return self.call("status")

    def configure(self, values, revision):
        return self.call("config", {"values": values, "revision": revision})
