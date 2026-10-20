"""Contract tests exercise the actual HTTP adapter using a local test backend."""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from dyson.backend import BackendError, ManagedLlama
from dyson.cli import main
from dyson.client import DysonClient
from dyson.config import DEFAULTS, ConfigError, ConflictError, Settings, validate
from dyson.equalizer import Equalizer
from dyson.runtime import Runtime
from dyson.server import Server


class ContractBackend(BaseHTTPRequestHandler):
    """Fixture serving the documented wire protocol, not a real LLM."""

    def log_message(self, *args):
        return

    def reply(self, payload):
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.reply({"data": [{"id": "test-contract"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append(payload)
        self.reply({"choices": [{"message": {"role": "assistant", "content": "wire-protocol-test"},
                                  "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}})


@pytest.fixture
def running(tmp_path):
    backend = ThreadingHTTPServer(("127.0.0.1", 0), ContractBackend)
    backend.received = []
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    runtime = Runtime(tmp_path)
    runtime.configure({"endpoint": f"http://127.0.0.1:{backend.server_port}/v1",
                       "model": "test-contract"}, 0)
    server = Server(("127.0.0.1", 0), runtime)
    service = threading.Thread(target=server.serve_forever, daemon=True)
    service.start()
    client = DysonClient(port=server.server_port, token=server.token)
    try:
        yield runtime, server, client, backend
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=5)
        service.join(timeout=5)


def test_real_http_settings_to_payload_persistence_and_feedback(running):
    runtime, server, client, backend = running
    status = client.status()
    client.configure({"temperature": 0.25, "max_tokens": 2048}, status["settings"]["revision"])
    result = client.chat([{"role": "user", "content": "private prompt"}], "adapter")
    assert result["message"]["content"] == "wire-protocol-test"
    assert backend.received[-1]["temperature"] == 0.25
    assert backend.received[-1]["max_tokens"] == 2048
    assert result["usage"]["total_tokens"] == 10
    client.observe(result["request_id"], changed=True)
    assert client.status()["sessions"]["adapter"]["known_outcomes"] == 1
    persisted = Settings(runtime.directory).snapshot()
    assert persisted["values"]["temperature"] == 0.25
    events = (runtime.directory / "events.jsonl").read_text()
    assert "private prompt" not in events
    assert "wire-protocol-test" not in events
    with pytest.raises(BackendError, match="409"):
        client.observe(result["request_id"], changed=True)
    html = urlopen(f"http://127.0.0.1:{server.server_port}/").read().decode()
    assert "__DYSON_TOKEN__" not in html
    assert server.token in html


def test_settings_conflict_does_not_replace_values(running):
    _, _, client, _ = running
    revision = client.status()["settings"]["revision"]
    client.configure({"temperature": 0.3}, revision)
    with pytest.raises(BackendError, match="409"):
        client.configure({"temperature": 0.8}, revision)
    assert client.status()["settings"]["values"]["temperature"] == 0.3


def test_changed_settings_expire_pending_feedback(running):
    _, _, client, _ = running
    result = client.chat([{"role": "user", "content": "x"}])
    settings = client.status()["settings"]
    client.configure({"top_p": 0.9}, settings["revision"])
    with pytest.raises(BackendError, match="409"):
        client.observe(result["request_id"], changed=True)
    assert client.status()["sessions"] == {}


@pytest.mark.parametrize("patch", [
    {"temperature": float("nan")}, {"temperature": True}, {"max_tokens": 1.5},
    {"model": ""}, {"endpoint": "http://example.com:8080/v1"},
    {"endpoint": "http://127.0.0.1:8080/v1?secret=x"},
    {"endpoint": "http://user:password@127.0.0.1:8080/v1"},
    {"min_tokens": 4096}, {"min_observations": 20}, {"unexpected": 0},
])
def test_invalid_config_is_rejected_atomically(tmp_path, patch):
    with Runtime(tmp_path) as runtime:
        before = runtime.settings.snapshot()
        with pytest.raises(ConfigError):
            runtime.configure(patch, 0)
        assert runtime.settings.snapshot() == before
        assert not (tmp_path / "settings.json").exists()


def test_instance_lock_released_and_settings_restored(tmp_path):
    with Runtime(tmp_path) as first:
        first.configure({"seed": 918}, 0)
        with pytest.raises(ConfigError, match="already"):
            Runtime(tmp_path)
    with Runtime(tmp_path) as second:
        assert second.settings.values["seed"] == 918
        assert second.settings.revision == 1


def test_corrupt_state_is_not_silently_replaced(tmp_path):
    (tmp_path / "settings.json").write_text('{"schema":99}')
    with pytest.raises(ConfigError):
        Runtime(tmp_path)
    assert (tmp_path / "settings.json").read_text() == '{"schema":99}'


def observe_sequence(equalizer, values, session="a", latency=1):
    transitions = []
    for changed in values:
        equalizer.response(session, latency)
        transitions.append(equalizer.observe(session, changed))
    return transitions


def test_equalizer_preserves_baseline_unknown_and_session_isolation():
    eq = Equalizer(DEFAULTS)
    baseline = eq.parameters("a")
    observe_sequence(eq, [None] * 8)
    assert eq.state("a").mode == "BASELINE"
    assert len(eq.state("a").outcomes) == 0
    assert eq.parameters("a") == baseline
    transitions = observe_sequence(eq, [True] * 4)
    assert transitions[-1]["after"] == "MOMENTUM"
    assert eq.parameters("a")["max_tokens"] == 768
    assert eq.state("b").mode == "BASELINE"
    assert eq.parameters("b")["max_tokens"] == 1024


def test_equalizer_recovery_cooldown_and_level_reset():
    eq = Equalizer(DEFAULTS)
    observe_sequence(eq, [False] * 4)
    assert eq.state("a").mode == "RECOVERY"
    assert eq.parameters("a")["max_tokens"] == DEFAULTS["max_tokens"]
    observe_sequence(eq, [True, True])
    assert eq.state("a").mode == "RECOVERY"
    observe_sequence(eq, [True] * 8)
    assert eq.state("a").mode == "MOMENTUM"
    eq.observe("a", None, level="new-level")
    assert eq.state("a").mode == "BASELINE"
    assert len(eq.state("a").outcomes) == 0


def test_economy_uses_latency_with_mixed_observed_outcomes():
    eq = Equalizer(DEFAULTS)
    observe_sequence(eq, [True, False, True, False], latency=45)
    assert eq.state("a").mode == "ECONOMY"
    assert eq.parameters("a")["max_tokens"] == 512


def test_equalizer_disabled_leaves_parameters_unmodified():
    eq = Equalizer({**DEFAULTS, "equalizer_enabled": False})
    observe_sequence(eq, [True] * 10)
    assert eq.state("a").mode == "BASELINE"
    assert eq.parameters("a")["max_tokens"] == 1024


def test_api_rejects_auth_host_origin_nonfinite_and_unknown_fields(running):
    _, server, _, _ = running
    base = f"http://127.0.0.1:{server.server_port}"
    checks = [
        (Request(base + "/api/status"), 401),
        (Request(base + "/", headers={"Host": "attacker.invalid"}), 403),
        (Request(base + "/", headers={"Origin": "https://attacker.invalid"}), 403),
        (Request(base + "/api/config", data=b'{"values":{"temperature":NaN},"revision":1}',
                 headers={"X-Dyson-Token": server.token, "Content-Type": "application/json"}), 400),
        (Request(base + "/api/chat", data=b'{"messages":[],"shell":"exec"}',
                 headers={"X-Dyson-Token": server.token, "Content-Type": "application/json"}), 400),
    ]
    for request, code in checks:
        with pytest.raises(HTTPError) as exc:
            urlopen(request)
        assert exc.value.code == code


def test_live_backend_health_and_tools_are_forwarded_without_execution(running):
    _, _, client, backend = running
    assert client.call("health")["backend"]["data"][0]["id"] == "test-contract"
    tools = [{"type": "function", "function": {"name": "inspect",
              "parameters": {"type": "object", "properties": {}}}}]
    client.chat([{"role": "user", "content": "inspect"}], tools=tools)
    assert backend.received[-1]["tools"] == tools


def test_invalid_messages_and_feedback_never_create_evidence(tmp_path):
    with Runtime(tmp_path) as runtime:
        with pytest.raises(ConfigError):
            runtime.chat([{"role": "root", "content": "x"}])
        with pytest.raises(ConflictError):
            runtime.feedback("unknown", True)
        assert runtime.status()["sessions"] == {}


def test_no_concurrent_inference(running):
    runtime, _, _, _ = running
    runtime.inference_lock.acquire()
    try:
        with pytest.raises(ConflictError, match="busy"):
            runtime.chat([{"role": "user", "content": "x"}])
    finally:
        runtime.inference_lock.release()


def test_cli_can_configure_without_notebook_or_backend(tmp_path, capsys):
    assert main(["--state-dir", str(tmp_path), "config", "--set", '{"seed":918}']) == 0
    assert json.loads(capsys.readouterr().out)["values"]["seed"] == 918
    assert main(["--state-dir", str(tmp_path), "serve", "--model-path", "missing.gguf"]) == 2


def test_managed_server_rejects_missing_assets(tmp_path):
    with pytest.raises(ConfigError):
        ManagedLlama(str(tmp_path / "missing-server"), "missing.gguf", tmp_path)


def test_validation_preserves_explicit_model_choice():
    config = validate({**DEFAULTS, "model": "my-local-model"})
    assert config["model"] == "my-local-model"


def test_newer_feedback_supersedes_older_session_feedback(running):
    _, _, client, _ = running
    first = client.chat([{"role": "user", "content": "first"}], "ordered")
    second = client.chat([{"role": "user", "content": "second"}], "ordered")
    client.observe(second["request_id"], changed=True, level="new")
    with pytest.raises(BackendError, match="409"):
        client.observe(first["request_id"], changed=False, level="old")
    assert client.status()["sessions"]["ordered"]["known_outcomes"] == 1


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX executable script")
def test_managed_process_readiness_and_shutdown(tmp_path):
    executable = tmp_path / "contract-server"
    executable.write_text("#!" + sys.executable + "\n" + """
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
args, unused = parser.parse_known_args()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "managed-contract-only"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
""")
    executable.chmod(0o700)
    weights = tmp_path / "contract-only.gguf"
    weights.write_bytes(b"TEST CONTRACT ASSET; NOT MODEL WEIGHTS")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    managed = ManagedLlama(executable, weights, tmp_path, port=port)
    try:
        managed.wait_ready({**DEFAULTS, "endpoint": f"http://127.0.0.1:{port}/v1"}, timeout=10)
        assert managed.process.poll() is None
    finally:
        managed.close()
    assert managed.process.poll() is not None
    assert managed.log.closed


def test_managed_process_rejects_occupied_port(tmp_path):
    weights = tmp_path / "contract-only.gguf"
    weights.write_bytes(b"CONTRACT ONLY")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with pytest.raises(ConfigError, match="already in use"):
            ManagedLlama(sys.executable, weights, tmp_path, port=port)
