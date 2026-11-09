#!/usr/bin/env bash
# Start MiniTask on Fedora.
#
# Safe to repeat. Installs python3 when the command is missing, creates
# .venv when it is missing or broken, and installs dependencies when
# requirements.txt has changed. Then runs the app on 127.0.0.1:8000.

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"

die() {
  echo "minitask: $*" >&2
  exit 1
}

run_root() {
  if [[ $(id -u) -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    die "need root to run: $*"
  fi
}

if [[ ! -r /etc/os-release ]] || ! grep -q '^ID=fedora$' /etc/os-release; then
  die "this script supports Fedora"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "installing python3"
  run_root dnf install -y python3
fi
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH after installation"

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  die "Python 3.11 or newer is required ($(python3 --version 2>&1))"
fi

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  echo "creating .venv"
  rm -rf .venv
  python3 -m venv .venv
fi

stamp=.venv/.requirements.sha256
current=$(sha256sum requirements.txt | awk '{print $1}')
if [[ ! -x .venv/bin/uvicorn || ! -f $stamp || $(cat "$stamp") != "$current" ]]; then
  echo "installing Python dependencies"
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  printf '%s\n' "$current" >"$stamp"
else
  echo "Python environment is ready"
fi

mkdir -p data
echo "starting MiniTask at http://127.0.0.1:8000"
exec .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
