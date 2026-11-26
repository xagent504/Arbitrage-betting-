FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install-deps chromium \
    && playwright install chromium

COPY . .

CMD ["python", "bot.py"]
