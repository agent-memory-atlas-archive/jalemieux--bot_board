# The image the test workflow runs in (#416): everything .forgejo/workflows/tests.yml would
# otherwise install on every run. Built and pushed by .forgejo/workflows/test-image.yml when
# requirements.txt or this file changes; tests.yml still installs what is missing, so a stale
# image costs time, not failures.
FROM python:3.12-bookworm

# bin/bot formats `bot ps` with `column` (bsdextrautils), which the Python image lacks.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends bsdextrautils \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt httpx playwright \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && touch /etc/bot_board-tests
