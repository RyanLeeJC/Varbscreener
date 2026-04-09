# Railway: use Docker instead of Railpack/mise so Python comes from Docker Hub,
# avoiding CPython mirror timeouts during `mise install`.
FROM python:3.12.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway overrides this with railway.toml `deploy.startCommand` if set.
CMD ["python3", "Varibot/varibot.py", "--live"]
