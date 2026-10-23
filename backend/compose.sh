#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENV_FILE="${DAVE_ENV_FILE:-$ROOT/backend/.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo 'Prima copia backend/.env.example in backend/.env e configura UID/GID e SSH.' >&2
  exit 2
fi
export DAVE_ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
FILES=(-f backend/compose.yaml)
COMMAND=(docker compose)
if docker --version 2>/dev/null | grep -qi podman; then
  COMMAND=(podman-compose --in-pod=false)
  if [[ "$(podman info --format '{{.Host.Security.Rootless}}')" == true ]]; then
    FILES+=(-f backend/compose.podman.yaml)
  fi
fi
if [[ "${DAVE_FORWARD_AGENT:-0}" == 1 ]]; then
  if [[ ! -S "${SSH_AUTH_SOCK:-}" ]]; then
    echo 'DAVE_FORWARD_AGENT=1 richiede un SSH_AUTH_SOCK valido sul PC.' >&2
    exit 2
  fi
  FILES+=(-f backend/compose.agent.yaml)
fi
exec "${COMMAND[@]}" --env-file "$DAVE_ENV_FILE" "${FILES[@]}" "$@"
