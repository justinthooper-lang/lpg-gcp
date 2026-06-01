# syntax=docker/dockerfile:1.6

# Python base image — slim variant for smaller image size.
# Pinned to 3.12 because that's what we develop against.
FROM python:3.12-slim

# Don't write .pyc files to disk; don't buffer stdout/stderr.
# Both important for container environments — logs flush immediately,
# no orphan files on read-only filesystems.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Working directory inside the container.
WORKDIR /app

# Install Python dependencies first, separately from app code.
# Docker caches each layer; if requirements.txt doesn't change,
# we don't reinstall on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the app code. Order matters: copy code last so changes
# to .py files don't invalidate the dependency cache layer.
COPY *.py ./

# Cloud Run sets the PORT env var (defaults to 8080). Honor it.
# Bind to 0.0.0.0 so external traffic can reach us.
ENV PORT=8080
EXPOSE 8080

# Use sh -c so $PORT gets expanded at runtime, not build time.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
