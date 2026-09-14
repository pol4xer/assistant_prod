.PHONY: install run dev lint format test check docker-build docker-up docker-down

install:
	poetry install --no-interaction

run:
	poetry run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

dev:
	poetry run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

lint:
	poetry run ruff check .
	poetry run ruff format --check .

format:
	poetry run ruff check --fix .
	poetry run ruff format .

test:
	poetry run pytest

check:
	poetry check --lock
	$(MAKE) lint
	$(MAKE) test
	node --check app/static/dashboard.js

docker-build:
	docker compose build

docker-up:
	docker compose up --build

docker-down:
	docker compose down
