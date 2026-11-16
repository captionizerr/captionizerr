#!/bin/sh
# Build from this checkout on a remote Docker host and restart the stack there, then drop the
# image the previous build left behind (only Captionizerr's own, by label).
# The stack directory keeps its own docker-compose.yml (with `build: ./src`) and .env.
# Usage: deploy/deploy.sh user@host /path/to/stack
set -eu
HOST=${1:?usage: deploy.sh user@host /path/to/stack}
DIR=${2:?usage: deploy.sh user@host /path/to/stack}
git archive HEAD | ssh "$HOST" "set -e
  rm -rf '$DIR/src' && mkdir -p '$DIR/src' && tar -x -C '$DIR/src'
  cd '$DIR' && docker compose build -q && docker compose up -d
  docker image prune -f --filter label=org.opencontainers.image.title=captionizerr >/dev/null
  sleep 5; curl -fsS localhost:8095/health; echo"
