clean: format lint

format:
	poetry run ruff format .

lint:
	poetry run ruff check --fix .
