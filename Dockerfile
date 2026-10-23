FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Образ с pytest для проверки: docker build --target test -t telegram-lead-hunter-test .
FROM base AS test
RUN pip install --no-cache-dir -r requirements-dev.txt

# Рабочий образ (последний этап, берётся по умолчанию)
FROM base AS app
CMD ["python", "main.py"]
