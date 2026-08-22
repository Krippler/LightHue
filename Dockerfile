FROM python:3.11-slim

WORKDIR /srv

# tcpdump is here for one reason: when a stream times out, the only question
# left is whether our datagrams reach the wire and whether anything answers.
# With host networking the container shares the host's stack, so capturing from
# in here sees exactly what the host would — and that beats asking whoever is
# debugging to install tools on their NAS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tcpdump \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
# Diagnostics belong in the image: streaming problems have to be investigated
# on the machine that has the bridge, which is rarely the one with a checkout.
COPY scripts ./scripts

# Config/presets persist here — mount a volume to this path
RUN mkdir -p /data
VOLUME ["/data"]

ENV CONFIG_PATH=/data/config.json
# 26000 is Quake's own registered port — nothing else on a NAS tends to want it.
ENV PORT=26000
EXPOSE 26000

# Shell form so PORT can be overridden, which is the only way to move the
# listener when running with host networking (no port mapping to remap).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-26000}"]
