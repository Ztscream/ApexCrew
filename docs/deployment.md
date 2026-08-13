# Deployment

Build the static read-only bundle with `uv run --python 3.12 python scripts/build_webui.py dist/webui`.

The output contains only static assets and reads `/api/run`; it cannot submit commands, approvals, model requests, credentials, or repository mutations. Enabling GitHub Pages or another host deployment is an owner action and is intentionally not performed by this repository task.
