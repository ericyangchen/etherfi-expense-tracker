FROM python:3.13-slim

WORKDIR /app

# Install deps first (cached layer); pyproject.toml is the single source of truth.
COPY pyproject.toml ./
RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py", "bot"]
