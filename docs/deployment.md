# Deployment

Build the static read-only Run Evidence Console with `uv run --python 3.12
python scripts/build_webui.py dist/webui`.

The console replays one fixed, sanitized Crew Run. It presents the Coordinator
lifecycle, bounded Worker tasks, evidence freshness, Runtime Permit and Grant
state, budget use, and the Tier 1 Audit ledger. Reviewers can play, pause, step,
scrub, and filter that embedded record without contacting an API.

The output contains only static assets. It cannot submit commands, approvals,
model requests, credentials, or repository mutations. The Content Security
Policy disables runtime connections, and the browser script only projects the
embedded JSON through text and visibility state. GitHub Pages is therefore a
deterministic inspection surface, not an ApexCrew execution service.

`pages.yml` deploys only after the `ci` workflow succeeds for `main`. It checks
out that completed CI run's exact SHA, rebuilds `dist/webui`, uploads it as a
Pages artifact, and deploys it with the job-scoped `pages: write` and
`id-token: write` permissions. An owner may also run `Deploy Pages` manually
from `main` to retry a deployment.

Before the first deployment, set GitHub repository **Settings -> Pages ->
Source** to **GitHub Actions**. After `Deploy Pages` succeeds, the public replay
URL is `https://ztscream.github.io/ApexCrew/`.
