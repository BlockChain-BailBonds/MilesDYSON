#!/usr/bin/env bash
set -euo pipefail

REPO='BlockChain-BailBonds/dyson-markdown-tuning-panels'

if ! command -v gh >/dev/null 2>&1; then
  printf 'Missing GitHub CLI. Install gh from https://cli.github.com and run gh auth login.\n' >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  printf 'GitHub authentication is required: run gh auth login first.\n' >&2
  exit 1
fi
if [[ ! -f pyproject.toml || ! -f README.md ]]; then
  printf 'Run this script from the DYSON repository root.\n' >&2
  exit 1
fi
if [[ -d .git ]]; then
  printf 'A Git repository already exists here. Inspect remotes before publishing to avoid overwrites.\n' >&2
  exit 1
fi
if gh repo view "$REPO" >/dev/null 2>&1; then
  printf 'The requested GitHub repository already exists. Refusing to overwrite it.\n' >&2
  exit 1
fi

git init -b main
git add .
git commit -m 'Initial public DYSON Markdown Tuning Panels release'
gh repo create "$REPO" --public --source=. --remote=origin --push
printf '\nPublished: https://github.com/%s\n' "$REPO"
