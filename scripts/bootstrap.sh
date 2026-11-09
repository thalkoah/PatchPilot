#!/usr/bin/env bash
# Prepare PatchPilot on Fedora or macOS and start it.
#
# Safe to repeat. An existing .env is kept. The demo checkout is initialized
# only when it has no .git directory. A model that is already installed is not
# pulled again. Compose is applied to the current project, which leaves a
# running stack in place.
#
# Ollama tags are not listed here. The script asks the app which models the
# current LLM_MODEL and LLM_STAGE_MODELS settings will call.

set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

START=1
SKIP_MODELS=0
SKIP_SYSTEM=0
PLATFORM=
DOCKER_DESKTOP=0
COMPOSE=()
MODELS=()
OLLAMA_PID=
READY_BODY=
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap.sh [--no-start] [--skip-models] [--skip-system]

Prepare a Fedora or macOS checkout and start PatchPilot.

  --no-start      Install dependencies and models, then stop before Compose
  --skip-models   Do not install Ollama or pull models
  --skip-system   Do not install operating-system packages

The script reads LLM_MODEL and LLM_STAGE_MODELS and pulls each distinct Ollama
model those settings name. Changing the model list does not require editing
this file. An existing .env is not overwritten.
EOF
}

die() {
  echo "bootstrap: $*" >&2
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

need_system() {
  if [[ $SKIP_SYSTEM -eq 1 ]]; then
    die "$1"
  fi
}

detect_platform() {
  case "$(uname -s)" in
    Darwin) PLATFORM=mac ;;
    Linux)
      if [[ -r /etc/os-release ]] && grep -q '^ID=fedora$' /etc/os-release; then
        PLATFORM=fedora
      else
        PLATFORM=linux
      fi
      ;;
    *)
      die "this script supports Fedora and macOS (found $(uname -s))"
      ;;
  esac
}

ensure_package() {
  local command_name=$1
  local fedora_package=$2
  local brew_package=$3
  local hint=$4
  if command -v "$command_name" >/dev/null 2>&1; then
    return
  fi
  need_system "$hint"
  echo "installing $command_name"
  case "$PLATFORM" in
    fedora)
      run_root dnf install -y "$fedora_package"
      ;;
    mac)
      if ! command -v brew >/dev/null 2>&1; then
        die "$hint"
      fi
      brew install "$brew_package"
      ;;
    *)
      die "$hint"
      ;;
  esac
  command -v "$command_name" >/dev/null 2>&1 || die "$command_name is not on PATH after installation"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
    return
  fi
  need_system "uv is not installed. See https://docs.astral.sh/uv/ and re-run with --skip-system."
  echo "installing uv"
  case "$PLATFORM" in
    fedora)
      run_root dnf install -y uv
      ;;
    mac)
      if command -v brew >/dev/null 2>&1; then
        brew install uv
      else
        curl -fsSL https://astral.sh/uv/install.sh -o "$tmpdir/uv-install.sh"
        sh "$tmpdir/uv-install.sh"
        export PATH="$HOME/.local/bin:$PATH"
      fi
      ;;
    *)
      curl -fsSL https://astral.sh/uv/install.sh -o "$tmpdir/uv-install.sh"
      sh "$tmpdir/uv-install.sh"
      export PATH="$HOME/.local/bin:$PATH"
      ;;
  esac
  command -v uv >/dev/null 2>&1 || die "uv is not on PATH after installation"
}

ensure_env_file() {
  if [[ -f .env ]]; then
    echo ".env already present"
    return
  fi
  cp .env.example .env
  echo "created .env from .env.example"
}

sync_python() {
  echo "installing Python dependencies"
  uv python install 3.12
  uv sync --frozen
}

read_models() {
  local model
  MODELS=()
  if ! uv run python -m patchpilot.llm.required_models >"$tmpdir/models"; then
    die "could not read the configured Ollama models"
  fi
  while IFS= read -r model; do
    [[ -n $model ]] || continue
    if [[ ! $model =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$ ]]; then
      die "refusing model name: $model"
    fi
    MODELS+=("$model")
  done <"$tmpdir/models"
  if [[ ${#MODELS[@]} -eq 0 ]]; then
    echo "configuration does not call Ollama"
  else
    echo "Ollama models required by the current settings:"
    printf '  %s\n' "${MODELS[@]}"
  fi
}

ollama_answers() {
  curl -fsS --max-time 2 http://127.0.0.1:11434/ >/dev/null 2>&1
}

ensure_ollama() {
  if command -v ollama >/dev/null 2>&1; then
    return
  fi
  if [[ -x /usr/local/bin/ollama ]]; then
    export PATH="/usr/local/bin:$PATH"
    return
  fi
  need_system "ollama is not installed. Install it from https://ollama.com/download and re-run with --skip-system."
  echo "installing Ollama"
  if [[ $PLATFORM == fedora || $PLATFORM == linux ]]; then
    echo "Using the upstream installer. Fedora's ollama package lags upstream and cannot load current models."
  fi
  if [[ $PLATFORM == mac ]] && command -v brew >/dev/null 2>&1; then
    brew install ollama
  else
    curl -fsSL https://ollama.com/install.sh -o "$tmpdir/ollama-install.sh"
    run_root sh "$tmpdir/ollama-install.sh"
    export PATH="/usr/local/bin:$PATH"
  fi
  command -v ollama >/dev/null 2>&1 || die "ollama is not on PATH after installation"
}

ollama_unit_installed() {
  local units
  command -v systemctl >/dev/null 2>&1 || return 1
  units=$(systemctl list-unit-files 'ollama.service' --no-legend 2>/dev/null || true)
  case "$units" in
    *ollama.service*) return 0 ;;
  esac
  return 1
}

serve_ollama_in_background() {
  local log_dir host=${1:-}
  log_dir=${XDG_STATE_HOME:-$HOME/.local/state}/patchpilot
  mkdir -p "$log_dir"
  if [[ -n $host ]]; then
    OLLAMA_HOST="$host" nohup ollama serve >>"$log_dir/ollama-serve.log" 2>&1 &
  else
    nohup ollama serve >>"$log_dir/ollama-serve.log" 2>&1 &
  fi
  OLLAMA_PID=$!
}

ensure_ollama_running() {
  if ollama_answers; then
    return
  fi
  echo "starting Ollama"
  if [[ $PLATFORM != mac ]] && ollama_unit_installed; then
    run_root systemctl enable --now ollama
  elif [[ $PLATFORM == mac ]] && command -v brew >/dev/null 2>&1 && brew list --formula ollama >/dev/null 2>&1; then
    brew services start ollama
  else
    serve_ollama_in_background
  fi
  local _attempt
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if ollama_answers; then
      return
    fi
    sleep 1
  done
  die "Ollama did not answer on http://127.0.0.1:11434"
}

pull_models() {
  local model
  [[ ${#MODELS[@]} -gt 0 ]] || return 0
  if [[ $SKIP_MODELS -eq 1 ]]; then
    echo "skipping Ollama model downloads"
    return
  fi
  ensure_ollama
  ensure_ollama_running
  for model in "${MODELS[@]}"; do
    if ollama show "$model" >/dev/null 2>&1; then
      echo "model already present: $model"
    else
      echo "pulling $model"
      ollama pull "$model"
    fi
  done
}

listening_beyond_loopback() {
  local listen=""
  if command -v ss >/dev/null 2>&1; then
    listen=$(ss -H -ltn 'sport = :11434' 2>/dev/null || true)
  fi
  if [[ -z $listen ]] && command -v lsof >/dev/null 2>&1; then
    listen=$(lsof -nP -iTCP:11434 -sTCP:LISTEN 2>/dev/null || true)
  fi
  printf '%s' "$listen" | grep -F -e '0.0.0.0:11434' -e '[::]:11434' -e '*:11434' >/dev/null
}

warn_firewall() {
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --quiet --query-port=11434/tcp; then
    echo "warning: firewalld already allows 11434/tcp. This script did not open it." >&2
    echo "warning: close that port if Ollama should stay off the LAN." >&2
  fi
}

bind_ollama_for_compose() {
  local dest=/etc/systemd/system/ollama.service.d/patchpilot.conf
  local plist="$HOME/Library/LaunchAgents/homebrew.mxcl.ollama.plist"
  if [[ -n $OLLAMA_PID ]]; then
    kill "$OLLAMA_PID" >/dev/null 2>&1 || true
    wait "$OLLAMA_PID" >/dev/null 2>&1 || true
    serve_ollama_in_background 0.0.0.0:11434
    return
  fi
  if [[ $PLATFORM != mac ]] && ollama_unit_installed; then
    if run_root test -f "$dest" && run_root grep -q 'OLLAMA_HOST=0.0.0.0:11434' "$dest"; then
      run_root systemctl restart ollama
    else
      cat >"$tmpdir/ollama-drop-in.conf" <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
EOF
      run_root mkdir -p /etc/systemd/system/ollama.service.d
      run_root cp "$tmpdir/ollama-drop-in.conf" "$dest"
      run_root systemctl daemon-reload
      run_root systemctl restart ollama
    fi
    return
  fi
  if [[ -f $plist && -x /usr/libexec/PlistBuddy ]] && command -v brew >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_HOST 0.0.0.0:11434" "$plist" >/dev/null 2>&1 \
      || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_HOST string 0.0.0.0:11434" "$plist"
    brew services restart ollama
    return
  fi
  die "Ollama is listening on loopback only, so the Compose worker cannot reach it. On Fedora the script sets a systemd drop-in. On macOS, install the Homebrew formula or allow local network access in the Ollama app, then re-run."
}

allow_compose_to_reach_ollama() {
  [[ $START -eq 1 ]] || return 0
  [[ ${#MODELS[@]} -gt 0 ]] || return 0
  [[ $SKIP_MODELS -eq 1 ]] && return 0
  if [[ $DOCKER_DESKTOP -eq 1 ]]; then
    echo "Docker Desktop reaches Ollama on 127.0.0.1 through host.docker.internal."
    return
  fi
  if listening_beyond_loopback; then
    echo "Ollama is already reachable from the Compose network. Port 11434 stays unpublished."
    warn_firewall
    return
  fi
  echo "Binding Ollama on 0.0.0.0:11434 so the worker can use host.docker.internal."
  echo "Compose does not publish port 11434. firewalld is not changed."
  bind_ollama_for_compose
  local _attempt
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if ollama_answers; then
      warn_firewall
      return
    fi
    sleep 1
  done
  die "Ollama did not answer after binding beyond loopback"
}

ensure_podman_machine() {
  if podman info >/dev/null 2>&1; then
    return
  fi
  if [[ $PLATFORM != mac ]]; then
    podman info || true
    die "podman is installed but not usable"
  fi
  local names
  names=$(podman machine list --format '{{.Name}}' 2>/dev/null || true)
  if [[ -z ${names//[[:space:]]/} ]]; then
    podman machine init
  fi
  podman machine start
  podman info >/dev/null
}

install_container_runtime() {
  need_system "Need Docker Compose or Podman. Install one and re-run with --skip-system."
  case "$PLATFORM" in
    fedora)
      echo "installing podman"
      run_root dnf install -y podman
      ;;
    mac)
      if ! command -v brew >/dev/null 2>&1; then
        die "Install Docker Desktop or Homebrew, then re-run."
      fi
      echo "installing podman"
      brew install podman
      ;;
    *)
      die "Install Docker or Podman, then re-run with --skip-system."
      ;;
  esac
}

choose_compose() {
  local info
  DOCKER_DESKTOP=0
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
    info=$(docker info 2>/dev/null || true)
    case "$info" in
      *"Docker Desktop"*) DOCKER_DESKTOP=1 ;;
    esac
    return
  fi
  if ! command -v podman >/dev/null 2>&1; then
    install_container_runtime
  fi
  command -v podman >/dev/null 2>&1 || die "podman is not on PATH after installation"
  ensure_podman_machine
  if podman compose version >/dev/null 2>&1; then
    COMPOSE=(podman compose)
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE=(podman-compose)
  else
    COMPOSE=(uvx podman-compose)
  fi
}

compose_exec() {
  local rendered
  rendered="${COMPOSE[*]}"
  case "$rendered" in
    *podman-compose*)
      "${COMPOSE[@]}" exec "$@"
      ;;
    *)
      "${COMPOSE[@]}" exec -T "$@"
      ;;
  esac
}

api_is_ready() {
  local body
  body=$(curl -fsS --max-time 2 http://127.0.0.1:8000/health/ready || true)
  case "$body" in
    *'"database":"ready"'*)
      READY_BODY=$body
      return 0
      ;;
  esac
  return 1
}

wait_ready() {
  local _attempt
  echo "waiting for the API"
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 \
    21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 \
    41 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 58 59 60; do
    if api_is_ready; then
      echo "ready: $READY_BODY"
      return 0
    fi
    sleep 2
  done
  die "the API did not become ready. Logs: ${COMPOSE[*]} logs api"
}

register_checkout() {
  local name=$1
  local path=$2
  local _attempt
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if compose_exec worker python -m patchpilot.source register \
      --name "$name" \
      --path "$path"; then
      return 0
    fi
    sleep 2
  done
  die "could not register examples/$path"
}

register_demo() {
  register_checkout "Demo service" demo_service
}

register_minitask() {
  register_checkout "MiniTask" minitask
}

refuse_no_new_privs() {
  local flag=
  [[ -r /proc/self/status ]] || return 0
  flag=$(sed -n 's/^NoNewPrivs:[[:space:]]*//p' /proc/self/status)
  [[ $flag == 1 ]] || return 0
  die "this shell has no_new_privs set, so SELinux denies nnp_transition and Podman cannot run. Start ./scripts/bootstrap.sh from a normal terminal."
}

repository_host_path() {
  local value=${REPOSITORY_ROOT_HOST:-}
  local line
  if [[ -z $value && -f .env ]]; then
    line=$(grep -E '^[[:space:]]*REPOSITORY_ROOT_HOST=' .env | tail -n 1 || true)
    value=${line#*=}
    value=${value%$'\r'}
    value=${value#\"}
    value=${value%\"}
    value=${value#\'}
    value=${value%\'}
  fi
  if [[ -z $value ]]; then
    value=./examples
  fi
  printf '%s\n' "$value"
}

# :Z runs when the worker container is created. `compose up` leaves a running
# worker mounted, so a checkout added afterwards stays user_home_t and
# registration dies with EACCES on .git/HEAD. Copy the label already on the
# mount point. Recreate the worker only when that mount was never labeled.
label_repository_root() {
  [[ $(getenforce 2>/dev/null || true) == Enforcing ]] || return 0
  local root label type level unlabeled
  root=$(repository_host_path)
  [[ -d $root ]] || die "repository root is missing: $root"
  label=$(stat -c %C "$root" 2>/dev/null || true)
  case $label in
    *container_file_t*) ;;
    *)
      echo "recreating the worker so SELinux can label $root"
      "${COMPOSE[@]}" up -d --force-recreate --no-deps worker
      label=$(stat -c %C "$root" 2>/dev/null || true)
      ;;
  esac
  case $label in
    *container_file_t*) ;;
    *)
      die "SELinux did not label $root for the worker (got ${label:-none})"
      ;;
  esac
  if ! unlabeled=$(find "$root" -xdev ! -context '*container_file_t*' -print -quit); then
    die "could not inspect SELinux labels under $root"
  fi
  [[ -n $unlabeled ]] || return 0
  type=${label#*:*:}
  type=${type%%:*}
  level=${label#*:*:*:}
  echo "applying the worker SELinux label under $root"
  chcon -R -t "$type" -l "$level" "$root"
}

start_stack() {
  [[ $START -eq 1 ]] || return 0
  refuse_no_new_privs
  if [[ $SKIP_MODELS -eq 1 && ${#MODELS[@]} -gt 0 ]]; then
    echo "warning: the current settings call Ollama, and --skip-models left those models unpulled." >&2
  fi
  choose_compose
  allow_compose_to_reach_ollama
  # Always apply the project. A ready API used to skip this, so a volume
  # change never reached the running worker. :Z still applies only when the
  # worker is created, so label_repository_root covers checkouts added later.
  if api_is_ready; then
    echo "API is already ready: $READY_BODY"
    echo "applying Compose (${COMPOSE[*]})"
    "${COMPOSE[@]}" up -d
  else
    echo "starting Compose (${COMPOSE[*]})"
    "${COMPOSE[@]}" up --build -d
    wait_ready
  fi
  label_repository_root
  register_demo
  register_minitask
  echo "dashboard: http://127.0.0.1:8000/"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start) START=0 ;;
    --skip-models) SKIP_MODELS=1 ;;
    --skip-system) SKIP_SYSTEM=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
  shift
done

detect_platform
ensure_package git git git "git is not installed."
ensure_package curl curl curl "curl is not installed."
ensure_uv
ensure_env_file
sync_python
read_models
pull_models
uv run python scripts/init_demo_repo.py
start_stack
