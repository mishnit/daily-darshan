FROM python:3.11-slim

# Avoid .pyc files and enable unbuffered stdout for real-time logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# The host provides $PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# ASGI server for FastAPI. Single worker keeps CSV-in-Git writes serialized
# (see README: avoid concurrent writers for the MVP webhook).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
