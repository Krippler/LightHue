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
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
