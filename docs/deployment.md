# Deployment

Build the static read-only bundle with `uv run --python 3.12 python scripts/build_webui.py dist/webui`.

The output contains only static assets and reads `/api/run`; it cannot submit commands, approvals, model requests, credentials, or repository mutations. The GitHub Pages deployment is a sanitized, deterministic fixture replay for inspection, not an execution service.
