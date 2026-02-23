FROM python:3.13-slim

# minimal OS deps + Node.js for Claude CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
     | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
  && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
     > /etc/apt/sources.list.d/nodesource.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /a0/usr/workdir/zeroclaw

# copy app
COPY . /a0/usr/workdir/zeroclaw

# create venv + install deps at build time (faster startup)
RUN python3 -m venv venv \
 && . venv/bin/activate \
 && pip install -U pip \
 && pip install -r requirements.txt

EXPOSE 9000

CMD ["bash", "-lc", "./docker/start_zeroclaw.sh"]
