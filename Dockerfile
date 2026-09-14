FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    APP_DEMO_MODE=true \
    AUTOMATION_POLLING_ENABLED=false

WORKDIR /app
RUN pip install "pip>=26.2,<27" "poetry==2.3.2"
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root
COPY app ./app
RUN useradd --create-home appuser && mkdir -p /app/var && chown appuser:appuser /app/var
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
