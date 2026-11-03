FROM python:3.11-slim
# The openssl CLI carries the Hue Entertainment DTLS-PSK stream (see sync.Hue.start).
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
# config.json, bridge_state.json and pyatv's ~/.pyatv.conf all live in the /data volume.
ENV DATA_DIR=/data HOME=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
CMD ["python", "bridge.py"]
