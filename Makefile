.PHONY: test lint demo secret-scan web-build build

UV_RUN := uv run --python 3.12

test:
	$(UV_RUN) pytest -q

lint:
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check .
	$(UV_RUN) mypy src

demo:
	$(UV_RUN) python -m apexcrew.demo

secret-scan:
	$(UV_RUN) python scripts/secret_scan.py .

web-build:
	$(UV_RUN) python scripts/build_webui.py dist/webui

build:
	uv build
	docker build --tag apexcrew-executor:local .
