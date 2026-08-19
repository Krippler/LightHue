FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

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
