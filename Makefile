clean: format lint

format:
	uv run ruff format .

lint:
	uv run ruff check --fix .

test:
	uv run pytest

build:
	uv build
