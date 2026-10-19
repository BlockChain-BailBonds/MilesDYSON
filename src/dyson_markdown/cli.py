"""Command-line workflow for converting notebook Markdown to live widgets."""

from __future__ import annotations

import argparse
import json

from .notebook import compile_notebook, validate_notebook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dyson-markdown")
    sub = parser.add_subparsers(dest="command", required=True)
    compile_cmd = sub.add_parser("compile", help="Inject live panel code cells under Markdown declarations")
    compile_cmd.add_argument("input", help="Source .ipynb path")
    compile_cmd.add_argument("--output", "-o", required=True, help="Output .ipynb path")
    check_cmd = sub.add_parser("validate", help="Validate DYSON declarations and notebook structure")
    check_cmd.add_argument("input", help="Notebook .ipynb path")
    args = parser.parse_args(argv)
    if args.command == "compile":
        result = compile_notebook(args.input, args.output)
    elif args.command == "validate":
        result = validate_notebook(args.input)
    else:
        parser.error("unsupported command")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
