"""Local chat-completions backend and optional owned llama-server process."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import ConfigError, validate_endpoint


class BackendError(RuntimeError):
    """Backend could not complete a real request."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendError("backend redirects are disabled")


def request_json(url, payload=None, timeout=180):
    request = Request(url, data=None if payload is None else
                      json.dumps(payload, allow_nan=False).encode(),
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise BackendError("backend response exceeds 8 MiB")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise BackendError("backend response must be a JSON object")
        return result
    except HTTPError as exc:
        raise BackendError(f"backend returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, UnicodeError, ValueError) as exc:
        raise BackendError(f"backend request failed: {type(exc).__name__}") from exc


class LocalBackend:
    def health(self, settings):
        endpoint = validate_endpoint(settings["endpoint"])
        return request_json(endpoint + "/models", timeout=min(10, settings["timeout_s"]))

    def complete(self, settings, messages, parameters, tools=None):
        payload = {"model": settings["model"], "messages": messages,
                   "stream": False, **parameters}
        if tools:
            payload["tools"] = tools
        result = request_json(validate_endpoint(settings["endpoint"]) + "/chat/completions",
                              payload, settings["timeout_s"])
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise BackendError("backend returned no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise BackendError("backend returned no assistant message")
        if not isinstance(message.get("content"), str) and not message.get("tool_calls"):
            raise BackendError("backend returned neither text nor tool calls")
        return result


class ManagedLlama:
    """Starts a supplied executable and local GGUF; never downloads weights."""

    def __init__(self, executable, model, directory, *, port=8080, mmproj=None,
                 gpu_layers=0, context_size=8192):
        if not 1 <= port <= 65535 or context_size < 512 or gpu_layers < -1:
            raise ConfigError("invalid backend port, context size, or GPU layers")
        binary = Path(executable).expanduser().resolve()
        weights = Path(model).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ConfigError("llama-server must be an executable file")
        if not weights.is_file():
            raise ConfigError("local GGUF model file not found")
        # Refuse to attach accidentally to an unrelated server on the selected port.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise ConfigError("backend port is already in use") from exc
        args = [str(binary), "-m", str(weights), "--host", "127.0.0.1", "--port", str(port),
                "--alias", "dyson-local", "-c", str(context_size), "-ngl", str(gpu_layers)]
        if mmproj:
            projector = Path(mmproj).expanduser().resolve()
            if not projector.is_file():
                raise ConfigError("local projector file not found")
            args.extend(["--mmproj", str(projector)])
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "llama-server.log").open("ab")
        os.chmod(self.directory / "llama-server.log", 0o600)
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                            stdout=self.log, stderr=subprocess.STDOUT)
        except BaseException:
            self.log.close()
            raise

    def wait_ready(self, settings, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise BackendError("llama-server exited; inspect llama-server.log")
            try:
                LocalBackend().health(settings)
                return
            except BackendError:
                time.sleep(0.25)
        raise BackendError("model startup timed out; inspect llama-server.log")

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
        finally:
            self.log.close()
