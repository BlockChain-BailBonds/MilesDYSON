"""DYSON command line: configure, serve, inspect, and run real inference."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import URLError

from .backend import BackendError, ManagedLlama
from .client import DysonClient
from .config import ConfigError, atomic_json
from .runtime import Runtime
from .server import Server


def parser():
    p = argparse.ArgumentParser(prog="dyson", description="918 Technologies standalone DYSON")
    p.add_argument("--state-dir", default=os.environ.get("DYSON_HOME", str(Path.home() / ".dyson")))
    sub = p.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config", help="show or update settings while the service is stopped")
    config.add_argument("--set", dest="patch", help="JSON object of validated settings")
    serve = sub.add_parser("serve", help="start the local dashboard and API")
    serve.add_argument("--port", type=int, default=9180)
    serve.add_argument("--llama-server", help="absolute path of an installed llama-server executable")
    serve.add_argument("--model-path", help="local GGUF; first shard for split models")
    serve.add_argument("--mmproj", help="optional local multimodal projector")
    serve.add_argument("--backend-port", type=int, default=8080)
    serve.add_argument("--gpu-layers", type=int, default=0)
    serve.add_argument("--context-size", type=int, default=8192)
    serve.add_argument("--startup-timeout", type=float, default=300)
    for command in ("status", "doctor", "chat"):
        client = sub.add_parser(command)
        client.add_argument("--port", type=int, default=9180)
        if command == "chat":
            client.add_argument("prompt")
            client.add_argument("--session", default="cli")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    directory = Path(args.state_dir).expanduser().resolve()
    try:
        if args.command == "config":
            with Runtime(directory) as runtime:
                result = (runtime.configure(json.loads(args.patch), runtime.settings.revision)
                          if args.patch else runtime.settings.snapshot())
                print(json.dumps(result, indent=2))
            return 0
        if args.command != "serve":
            client = DysonClient(port=args.port, token_file=directory / "api-token")
            if args.command == "status":
                result = client.status()
            elif args.command == "doctor":
                result = client.call("health")
            else:
                result = client.chat([{"role": "user", "content": args.prompt}], args.session)
            print(json.dumps(result, indent=2))
            return 0
        if not 1 <= args.port <= 65535:
            raise ConfigError("dashboard port must be between 1 and 65535")
        if bool(args.llama_server) != bool(args.model_path):
            raise ConfigError("--llama-server and --model-path must be supplied together")
        if args.mmproj and not args.llama_server:
            raise ConfigError("--mmproj requires managed llama-server")
        if not 1 <= args.startup_timeout <= 3600:
            raise ConfigError("--startup-timeout must be between 1 and 3600")
        if args.llama_server and args.port == args.backend_port:
            raise ConfigError("dashboard and model backend must use different ports")
        managed = None
        try:
            with Runtime(directory) as runtime:
                if args.llama_server:
                    runtime.configure({"endpoint": f"http://127.0.0.1:{args.backend_port}/v1",
                                       "model": "dyson-local"}, runtime.settings.revision)
                    managed = ManagedLlama(args.llama_server, args.model_path, directory,
                                           port=args.backend_port, mmproj=args.mmproj,
                                           gpu_layers=args.gpu_layers, context_size=args.context_size)
                    print("Loading local GGUF; progress is in llama-server.log", flush=True)
                    managed.wait_ready(runtime.settings.values, args.startup_timeout)
                with Server(("127.0.0.1", args.port), runtime) as server:
                    # Atomic private token file; never write bearer tokens to stdout.
                    atomic_json(directory / "connection.json",
                                {"url": f"http://127.0.0.1:{args.port}", "token_file": "api-token"})
                    token_path = directory / "api-token"
                    descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        os.chmod(token_path, 0o600)
                        handle.write(server.token + "\n")
                    print(f"DYSON ready: http://127.0.0.1:{args.port}", flush=True)
                    print("Press Ctrl+C to stop.", flush=True)
                    try:
                        server.serve_forever(poll_interval=0.25)
                    except KeyboardInterrupt:
                        pass
        finally:
            if managed is not None:
                managed.close()
        return 0
    except (ConfigError, BackendError, OSError, ValueError, URLError) as exc:
        print(f"DYSON: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
