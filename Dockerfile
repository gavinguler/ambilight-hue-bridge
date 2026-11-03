# Build stage: some pyatv dependencies have no prebuilt wheel on arm64 and need a compiler.
FROM python:3.11-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.11-slim
# The openssl CLI carries the Hue Entertainment DTLS-PSK stream (see sync.Hue.start).
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt && rm -rf /wheels
COPY *.py ./
# config.json, bridge_state.json and pyatv's ~/.pyatv.conf all live in the /data volume.
ENV DATA_DIR=/data HOME=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
CMD ["python", "bridge.py"]
