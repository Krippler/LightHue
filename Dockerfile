FROM python:3.11-slim

WORKDIR /srv

# tcpdump is here for one reason: when a stream times out, the only question
# left is whether our datagrams reach the wire and whether anything answers.
# openssl rides along as a third opinion on the handshake — an implementation
# neither this repo nor its library wrote, so agreeing with it means something.
# With host networking the container shares the host's stack, so capturing from
# in here sees exactly what the host would — and that beats asking whoever is
# debugging to install tools on their NAS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tcpdump openssl \
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

# A wedged event loop is the one failure a restart policy cannot see: the
# process is alive, so Docker leaves it running, while nothing it was asked to
# do is happening. /api/health answers only if the loop is still getting round
# to its own work, and 503s when it has fallen behind.
#
# Python rather than curl: the slim image has no curl, and this needs no extra
# package. start-period covers the first-run restore, which talks to the bridge
# before the listener is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
u='http://127.0.0.1:%s/api/health' % os.environ.get('PORT','26000'); \
sys.exit(0 if urllib.request.urlopen(u, timeout=4).status == 200 else 1)"

# Shell form so PORT can be overridden, which is the only way to move the
# listener when running with host networking (no port mapping to remap).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-26000}"]
