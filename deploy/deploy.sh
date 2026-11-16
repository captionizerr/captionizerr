#!/bin/sh
# Build from this checkout on a remote Docker host and restart the stack there, then drop the
# image the previous build left behind (only Captionizerr's own, by label).
# The stack directory keeps its own docker-compose.yml (with `build: ./src`) and .env.
# It waits (up to 15 min) for running translations to finish before restarting, because an
# interrupted job is re-queued and its partial API usage is lost. --force skips the wait.
# Usage: deploy/deploy.sh user@host /path/to/stack [--force]
set -eu
HOST=${1:?usage: deploy.sh user@host /path/to/stack [--force]}
DIR=${2:?usage: deploy.sh user@host /path/to/stack [--force]}
FORCE=${3:-}
git archive HEAD | ssh "$HOST" "set -e
  rm -rf '$DIR/src' && mkdir -p '$DIR/src' && tar -x -C '$DIR/src'
  cd '$DIR' && docker compose build -q
  if [ '$FORCE' != '--force' ]; then
    for i in \$(seq 1 90); do
      n=\$(curl -fsS localhost:8095/health 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"states\"].get(\"running\", 0))' 2>/dev/null || echo 0)
      [ \"\$n\" = 0 ] && break
      [ \$i = 1 ] && echo \"waiting for \$n running job(s) to finish before restarting…\"
      sleep 10
    done
  fi
  docker compose up -d
  docker image prune -f --filter label=org.opencontainers.image.title=captionizerr >/dev/null
  sleep 5; curl -fsS localhost:8095/health; echo"
