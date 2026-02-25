FROM python:3.13-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir "psycopg[binary]" playwright "discord.py" \
    && playwright install --with-deps chromium

CMD ["python", "main.py", "bot"]
