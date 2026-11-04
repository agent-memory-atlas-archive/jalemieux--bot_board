# A per-harness runner image (#306, #476): the coding harness plus what a run needs on a
# small checkout — git, python3, curl, gh and ripgrep. The launcher (bin/bot, on the host)
# runs the harness inside this image with the bot's checkout and session volume mounted and
# the bot's env file passed in (#477); the image carries no board files and no tokens. One
# image per harness, selected by the HARNESS build arg. Built for amd64 and arm64.
#
#   docker build --build-arg HARNESS=claude -f deploy/harness.Dockerfile -t bot_board-harness-claude .
#
# Node 22 is the floor claude-code and copilot ask for; codex and opencode run under it too.
FROM node:22-bookworm-slim

# What a harness run shells out to. ripgrep: claude-code uses it; the others tolerate it.
# gh: git operations and the github-lifecycle skill. curl stays: harnesses and checks fetch.
ARG GH_VERSION=2.88.1
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates python3 ripgrep \
 && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_$(dpkg --print-architecture).tar.gz" \
    | tar -xz -C /tmp && mv /tmp/gh_*/bin/gh /usr/local/bin/gh && rm -rf /tmp/gh_* \
 && rm -rf /var/lib/apt/lists/*

# The harness, pinned when HARNESS_VERSION is set (e.g. 2.1.89). Installs are non-interactive;
# auth is a run-time env var the launcher passes in, never baked into the image.
ARG HARNESS
ARG HARNESS_VERSION=
RUN set -eu; v="${HARNESS_VERSION:+@$HARNESS_VERSION}"; \
    case "$HARNESS" in \
      claude)   npm install -g "@anthropic-ai/claude-code${v}" ;; \
      codex)    npm install -g "@openai/codex${v}" ;; \
      opencode) npm install -g "opencode-ai${v}" ;; \
      copilot)  npm_config_ignore_scripts=false npm install -g "@github/copilot${v}" ;; \
      *) echo "HARNESS must be claude, codex, opencode or copilot, not '${HARNESS:-}'" >&2; exit 1 ;; \
    esac

# No background auto-updates in an image: the keeper updates a harness by pulling a new tag (#478).
ENV DISABLE_AUTOUPDATER=1 DISABLE_UPDATES=1 BOT_BOARD_HARNESS=${HARNESS}

# A non-root user; the launcher mounts the checkout and session volume owned by it.
RUN useradd -u 10001 -m bot
USER bot
WORKDIR /work
