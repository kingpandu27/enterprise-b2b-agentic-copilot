# 1. Minimal, hardened Linux base image (<250MB)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 2. Docker Layer Caching: install dependencies first
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries 5 -r requirements.txt

# 3. Copy application code (rebuilds in 0.5s if code changes)
COPY handbook.py .
COPY app.py .

EXPOSE 8000

# 4. Start the Uvicorn ASGI server
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]