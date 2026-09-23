clean: format lint

format:
	uv run ruff format .

lint:
	uv run ruff check --fix .

build:
	uv build
