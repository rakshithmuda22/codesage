FROM python:3.11-slim

WORKDIR /app

# Install git (needed for GitPython) and build deps for tree-sitter
RUN apt-get update && \
    apt-get install -y --no-install-recommends git gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8000

# Start both FastAPI server and RQ worker
CMD ["sh", "-c", "rq worker codesage --url $REDIS_URL & uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
