FROM python:3.13-slim-trixie

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the publisher runtime. subscriber.py, schema/, and tests/ are host-side
COPY __init__.py     ./cmd_publisher/__init__.py
COPY publisher.py    ./cmd_publisher/publisher.py
COPY translator.py   ./cmd_publisher/translator.py
COPY records.py      ./cmd_publisher/records.py
COPY emit.py         ./cmd_publisher/emit.py
COPY device.py       ./cmd_publisher/device.py

ENV PYTHONPATH=/app
CMD ["python", "-u", "-m", "cmd_publisher.publisher"]
