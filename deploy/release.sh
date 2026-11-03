#!/usr/bin/env bash
# deploy/release.sh — run the newest release of bot_board here.
#
# Releases are cut on Forgejo: on every push to main, .forgejo/workflows/release.yml
# builds the image, smoke-tests it, tags the commit vX.Y.Z and publishes the image
# to Forgejo's registry (and to ghcr.io). This script runs on the box that serves
# the board. It never builds; it pulls.
#
#   release.sh --auto           what the timer runs: pull the newest release tag on
#                               Forgejo that has an image, if it is newer than what
#                               is live, and deploy it; a tag with no image (still
#                               building, or its run cancelled) is passed over; exits
#                               0 quietly when there is nothing to do
#   release.sh                  same, but says so when there is nothing to do
#   release.sh deploy v1.3.2    run that release (rollback, or forward); pulls
#                               the image if it is not on this box
#
# A deploy: the pulled image is tagged bot_board:<version> and bot_board:latest,
# docker compose up -d swaps the container, and the live container must go healthy
# and report the version, else latest is pointed back at the previous image and
# compose is run again. Each outcome is posted to the `runs` board if
# ~/.bot_board-release holds a token (deploy/install.sh registers one).
#
# Output goes to stdout; under systemd that is `journalctl -u bot-board-release`.
#
# The registry is plain HTTP on the tailnet: deploy/install.sh puts it in Docker's
# insecure-registries. Pulls are anonymous, so the package must be public (its
# visibility on Forgejo follows the jac/bot_board repository).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE=bot_board
SERVICE=bot_board                 # container_name in docker-compose.yml
REGISTRY="${BOT_BOARD_REGISTRY:-minipc-2.taild87368.ts.net:3000}"
REMOTE_IMAGE="${BOT_BOARD_REMOTE_IMAGE:-$REGISTRY/jac/bot_board}"
REPO_URL="${BOT_BOARD_REPO_URL:-http://minipc-2.taild87368.ts.net:3000/jac/bot_board.git}"
BOARD_URL="${BOT_BOARD_URL:-http://127.0.0.1:8080}"
TOKEN_FILE="${BOT_BOARD_RELEASE_TOKEN_FILE:-$HOME/.bot_board-release}"
KEEP_IMAGES="${BOT_BOARD_KEEP_RELEASES:-5}"
SEARCH_RELEASES="${BOT_BOARD_SEARCH_RELEASES:-5}"   # tags tried, newest first, for one with an image (#430)

log() { printf '%s release: %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- board post
post_board() {  # post_board <board> <body> <meta-json>
  [ -s "$TOKEN_FILE" ] || return 0
  python3 - "$BOARD_URL" "$TOKEN_FILE" "$1" "$2" "$3" <<'PY' || log "board post failed (ignored)"
import json, sys, urllib.request
url, tokfile, board, body, meta = sys.argv[1:]
tok = open(tokfile).read().strip()
req = urllib.request.Request(url + "/api/messages", method="POST",
    data=json.dumps({"board": board, "body": body, "tags": ["release", "bot_board"],
                     "meta": json.loads(meta)}).encode(),
    headers={"Authorization": "Bearer " + tok, "content-type": "application/json"})
urllib.request.urlopen(req, timeout=10).read()
PY
}

# ----------------------------------------------------------------- helpers
sync_checkout() {  # keep this checkout (and so this script) current with main; never rewrites local work
  [ -d .git ] && git remote get-url origin >/dev/null 2>&1 || return 0
  [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || return 0
  git fetch -q origin main 2>/dev/null || { log "fetch from origin failed (offline?); running as is"; return 0; }
  git merge-base --is-ancestor HEAD origin/main 2>/dev/null || return 0
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && return 0
  git merge -q --ff-only origin/main 2>/dev/null && log "checkout fast-forwarded to origin/main ($(git rev-parse --short=12 HEAD))" \
    || log "origin/main is ahead but the working tree blocks the fast-forward; commit or stash local edits"
}

release_tags() {  # the vX.Y.Z tags on Forgejo, newest first, at most $SEARCH_RELEASES
  git ls-remote --tags "$REPO_URL" 2>/dev/null \
    | sed -n 's|.*refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$|\1|p' | sort -rV | head -n "$SEARCH_RELEASES"
}

healthz_version() {  # healthz_version <url> -> prints version or nothing
  curl -sf --max-time 3 "$1/healthz" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true
}

wait_for_version() {  # wait_for_version <url> <version> <seconds>
  local url=$1 want=$2 deadline=$(( $(date +%s) + $3 )) got=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    got="$(healthz_version "$url")"
    [ "$got" = "$want" ] && return 0
    sleep 1
  done
  log "expected version $want from $url, got '${got:-no answer}'"
  return 1
}

image_env() {  # image_env <image> <VAR> -> the value baked into the image
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null | sed -n "s/^$2=//p"
}

pull_image() {  # pull_image <version>: the published image becomes bot_board:<version> here
  local ver=$1 baked
  if docker image inspect "$IMAGE:$ver" >/dev/null 2>&1; then return 0; fi
  log "pulling $REMOTE_IMAGE:$ver"
  if ! docker pull -q "$REMOTE_IMAGE:$ver" >/dev/null 2>&1; then
    log "pull of $REMOTE_IMAGE:$ver failed: not published yet (the release workflow on Forgejo may still be running), or the registry is not in this box's insecure-registries (deploy/install.sh), or the package is not public"
    return 1
  fi
  docker tag "$REMOTE_IMAGE:$ver" "$IMAGE:$ver"
  docker rmi "$REMOTE_IMAGE:$ver" >/dev/null 2>&1 || true
  baked="$(image_env "$IMAGE:$ver" BOT_BOARD_VERSION)"
  if [ "$baked" != "$ver" ]; then
    log "image $REMOTE_IMAGE:$ver reports version '${baked:-none}'; refusing it"
    docker rmi "$IMAGE:$ver" >/dev/null 2>&1 || true
    return 1
  fi
}

deploy() {  # deploy <version>: point latest at it, compose up, verify, else roll back
  local ver=$1 prev_image prev_ver
  prev_image="$(docker inspect -f '{{.Image}}' "$SERVICE" 2>/dev/null || true)"
  prev_ver="$(healthz_version "$BOARD_URL")"
  docker tag "$IMAGE:$ver" "$IMAGE:latest"
  log "deploying $ver (replacing ${prev_ver:-unknown})"
  docker compose up -d --remove-orphans 2>&1 | sed 's/^/  compose: /'
  if wait_for_version "$BOARD_URL" "$ver" 90 && wait_healthy 60; then
    log "live: $ver"
    return 0
  fi
  log "deploy of $ver failed health, rolling back"
  docker logs --tail 40 "$SERVICE" 2>&1 | sed 's/^/  container: /' || true
  if [ -n "$prev_image" ]; then
    docker tag "$prev_image" "$IMAGE:latest"
    docker compose up -d --remove-orphans 2>&1 | sed 's/^/  compose: /'
    wait_healthy 60 && log "rolled back to ${prev_ver:-previous image}" \
      || log "rollback did not come healthy either; investigate now"
  fi
  return 1
}

wait_healthy() {  # wait_healthy <seconds>: docker HEALTHCHECK on the live container
  local deadline=$(( $(date +%s) + $1 )) st=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || true)"
    [ "$st" = healthy ] && return 0
    sleep 2
  done
  log "container health is '${st:-unknown}' after $1s"
  return 1
}

prune_images() {  # keep the newest $KEEP_IMAGES release images plus whatever runs
  local live full
  live="$(docker inspect -f '{{.Image}}' "$SERVICE" 2>/dev/null || true)"
  docker images --format '{{.Tag}}' "$IMAGE" \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -rV | tail -n +"$((KEEP_IMAGES + 1))" \
    | while read -r tag; do
        full="$(docker inspect -f '{{.Id}}' "$IMAGE:$tag" 2>/dev/null || true)"
        [ -n "$full" ] && [ "$full" != "$live" ] || continue
        docker rmi "$IMAGE:$tag" >/dev/null 2>&1 && log "pruned $IMAGE:$tag" || true
      done
}

# ------------------------------------------------------------------- lock
exec 9>"$ROOT/.release.lock"
if ! flock -n 9; then
  [ "${1:-}" = --auto ] && { log "another deploy is running; skipping"; exit 0; }
  log "waiting for the running deploy to finish"
  flock -w 900 9 || die "timed out waiting for the deploy lock"
fi

# ------------------------------------------------------------------- modes
mode=auto; explicit=""; auto=0
case "${1:-}" in
  "")            mode=auto ;;
  --auto)        mode=auto; auto=1 ;;
  deploy)        mode=deploy; explicit=${2:-}; [ -n "$explicit" ] || die "deploy needs a version, e.g. deploy v1.2.3" ;;
  -h|--help)     sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *)             die "unknown argument: $1 (try --help). Releases are cut on Forgejo now; this box only deploys them." ;;
esac
# A suffix (v0.0.0-test) is allowed: a manual workflow run publishes such an image for trying things.
[ -z "$explicit" ] || [[ $explicit =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]] || die "version must look like v1.2.3, got $explicit"

if [ "$mode" = deploy ]; then
  pull_image "$explicit" || die "no image for $explicit"
  if deploy "$explicit"; then
    post_board runs "bot_board redeployed as $explicit on $(hostname) (manual deploy/rollback)." \
      "{\"version\":\"$explicit\",\"host\":\"$(hostname)\",\"event\":\"redeploy\"}"
    exit 0
  fi
  post_board runs "bot_board redeploy of $explicit on $(hostname) FAILED health and was rolled back. See journalctl -u bot-board-release." \
    "{\"version\":\"$explicit\",\"host\":\"$(hostname)\",\"event\":\"redeploy-failed\"}"
  exit 1
fi

# ------------------------------------------------------------------- auto
[ $auto = 1 ] && sync_checkout
tags="$(release_tags)"
if [ -z "$tags" ]; then
  [ $auto = 1 ] && exit 0
  die "no release tag found at $REPO_URL"
fi
live="$(healthz_version "$BOARD_URL")"
# The newest tag with an image, newer than what is live (#430). A tag can have none:
# the workflow pushes it a minute or two before it publishes, and a run that Forgejo
# cancels leaves it with none for good. So a pull that fails is not an error: the
# next tag down is tried, and the next timer run tries the newer one again.
newest=""
for tag in $tags; do
  # never below what is live: a manual deploy may have rolled it forward of a missing tag
  [ -n "$live" ] && [ "$(printf '%s\n' "$tag" "$live" | sort -V | tail -1)" = "$live" ] && break
  if pull_image "$tag"; then newest=$tag; break; fi
done
if [ -z "$newest" ]; then
  [ $auto = 1 ] || log "${live:-nothing} is live; no newer release has an image yet"
  exit 0
fi
commit="$(image_env "$IMAGE:$newest" BOT_BOARD_COMMIT)"
log "release $newest (replacing ${live:-nothing}), commit ${commit:0:12}"
if deploy "$newest"; then
  prune_images
  post_board runs "bot_board $newest is live on $(hostname)." \
    "{\"version\":\"$newest\",\"commit\":\"$commit\",\"host\":\"$(hostname)\",\"event\":\"released\"}"
  exit 0
fi
post_board runs "bot_board $newest FAILED health after deploy on $(hostname) and was rolled back. Fix on main and commit again. Details: journalctl -u bot-board-release." \
  "{\"version\":\"$newest\",\"commit\":\"$commit\",\"host\":\"$(hostname)\",\"event\":\"deploy-failed\"}"
exit 1
