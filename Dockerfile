FROM python:3.13-slim

# minimal OS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

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
