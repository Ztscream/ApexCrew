.PHONY: test coverage lint demo live-smoke secret-scan web-build build

UV_RUN := uv run --python 3.12

test:
	$(UV_RUN) pytest -q

coverage:
	$(UV_RUN) pytest --cov=apexcrew --cov-report=term-missing --cov-report=xml

lint:
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check .
	$(UV_RUN) mypy src

demo:
	$(UV_RUN) python -m apexcrew.demo

live-smoke:
	$(UV_RUN) pytest tests/integration/test_live_provider_smoke.py -q

secret-scan:
	$(UV_RUN) python scripts/secret_scan.py .

web-build:
	$(UV_RUN) python scripts/build_webui.py dist/webui

build:
	uv build
	docker build --tag apexcrew-executor:local .
