FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create volume mount point for persistent SQLite DB
RUN mkdir -p /data

ENV PORT=8080
ENV SECRET_KEY=change-me-in-production
ENV DB_PATH=/data/users.db

EXPOSE 8080

# Single worker keeps memory under Render's 512MB cap AND keeps the in-process
# server-side bot state consistent (multiple workers = duplicated/!shared state).
# Threads provide request concurrency. Do NOT add --max-requests: recycling the
# worker would kill the running AI-bot threads.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 3 --timeout 120
