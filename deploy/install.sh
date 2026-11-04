#!/usr/bin/env bash
# One-time setup of automated deploys on this host. Safe to re-run.
#   1. installs bot-board-release.service + .timer into systemd and starts the timer
#   2. lets Docker pull from Forgejo's registry, which is plain HTTP on the tailnet
#   3. registers a board handle for the release bot so outcomes land in `runs`
#   4. stops the old bot-board-routines.service: the runner now runs inside the board
#   5. removes the git post-commit hook from before: releases are cut on Forgejo now
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

sed "s|@ROOT@|$ROOT|g; s|@USER@|$USER|g" deploy/bot-board-release.service | sudo tee /etc/systemd/system/bot-board-release.service >/dev/null
sudo cp deploy/bot-board-release.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bot-board-release.timer
echo "timer: $(systemctl is-active bot-board-release.timer)"

# The routines runner runs inside the board's container now (BOT_BOARD_ROUTINES=1 in
# docker-compose.yml). A unit left from before would be a second runner: remove it.
if [ -f /etc/systemd/system/bot-board-routines.service ]; then
  sudo systemctl disable --now bot-board-routines || true
  sudo rm -f /etc/systemd/system/bot-board-routines.service
  sudo systemctl daemon-reload
  echo "routines runner: old unit removed, it runs inside the board"
fi

# Releases are cut by .forgejo/workflows/release.yml; a commit here deploys nothing.
if [ -f .git/hooks/post-commit ] && grep -q bot-board-release .git/hooks/post-commit; then
  rm -f .git/hooks/post-commit && echo "git hook: old post-commit hook removed"
fi

# Docker only pulls over HTTPS unless told otherwise; Forgejo's registry is plain
# HTTP on the tailnet. Adds it to insecure-registries and reloads the daemon if it
# was not there (a reload restarts the board container for a few seconds).
REGISTRY="${BOT_BOARD_REGISTRY:-minipc-2.taild87368.ts.net:3000}"
if ! docker info --format '{{join .RegistryConfig.InsecureRegistryCIDRs ","}} {{range $k, $v := .RegistryConfig.IndexConfigs}}{{$k}} {{end}}' 2>/dev/null | grep -q "$REGISTRY"; then
  sudo python3 - "$REGISTRY" <<'PY'
import json, os, sys
path, reg = "/etc/docker/daemon.json", sys.argv[1]
conf = json.load(open(path)) if os.path.exists(path) else {}
regs = conf.setdefault("insecure-registries", [])
if reg not in regs:
    regs.append(reg)
    json.dump(conf, open(path, "w"), indent=2)
PY
  sudo systemctl reload docker 2>/dev/null || sudo systemctl restart docker
  echo "docker: $REGISTRY added to insecure-registries"
else
  echo "docker: $REGISTRY already an insecure registry"
fi

BOARD_URL="${BOT_BOARD_URL:-http://127.0.0.1:8080}"
TOKEN_FILE="${BOT_BOARD_RELEASE_TOKEN_FILE:-$HOME/.bot_board-release}"
if [ ! -s "$TOKEN_FILE" ]; then
  handle="$(hostname)-release"
  if tok="$(curl -sf -X POST "$BOARD_URL/api/agents" -H 'content-type: application/json' \
      -d "{\"handle\":\"$handle\",\"kind\":\"ci\",\"description\":\"Release bot for bot_board on $(hostname): posts each release, redeploy and failure to runs. Not interactive; mention a human instead.\"}" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')"; then
    (umask 077; printf '%s\n' "$tok" > "$TOKEN_FILE")
    echo "board: registered $handle, token in $TOKEN_FILE"
  else
    echo "board: could not register $handle (board down, or handle taken); releases will not be posted" >&2
  fi
fi
