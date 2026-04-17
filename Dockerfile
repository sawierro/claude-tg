# Claude-TG container. The bot runs the Claude Code / Codex CLI as a subprocess,
# so the image must contain Node.js + npm and a writable home for CLI state.
# This Dockerfile is suitable for Linux hosts; Windows / WSL-attached sessions
# are not supported inside a container.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20

# System deps: Node.js (for Claude/Codex CLIs), git (for /update), ca-certificates, tini
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        tini \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code + Codex CLIs globally
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# Non-root bot user
RUN useradd --create-home --shell /bin/bash --uid 1000 bot
USER bot
WORKDIR /home/bot/claude-tg

# Python deps — install first for better caching
COPY --chown=bot:bot requirements.txt ./
RUN pip install --user --no-cache-dir -r requirements.txt
ENV PATH="/home/bot/.local/bin:${PATH}"

# App code
COPY --chown=bot:bot bot ./bot
COPY --chown=bot:bot config.example.json ./

# Persistent state lives here — mount a volume for the DB and prompts
VOLUME ["/home/bot/claude-tg/data"]
ENV CLAUDE_TG_DB_PATH=/home/bot/claude-tg/data/claude_tg.db
ENV PROMPTS_DIR=/home/bot/claude-tg/data/prompts

# tini as PID 1 — forwards signals and reaps zombies, important because we spawn
# many claude/codex subprocesses.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "bot.main"]
