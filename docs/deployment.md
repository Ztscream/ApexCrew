# Deployment

Build the static read-only bundle with `uv run --python 3.12 python scripts/build_webui.py dist/webui`.

The output contains only static assets and reads `/api/run`; it cannot submit commands, approvals, model requests, credentials, or repository mutations. The GitHub Pages deployment is a sanitized, deterministic fixture replay for inspection, not an execution service.

`pages.yml` deploys only after the `ci` workflow succeeds for `main`. It checks
out that completed CI run's exact SHA, rebuilds `dist/webui`, uploads it as a
Pages artifact, and deploys it with the job-scoped `pages: write` and
`id-token: write` permissions. An owner may also run `Deploy Pages` manually
from `main` to retry a deployment.

Before the first deployment, set GitHub repository **Settings -> Pages ->
Source** to **GitHub Actions**. After `Deploy Pages` succeeds, the public replay
URL is `https://ztscream.github.io/ApexCrew/`.
