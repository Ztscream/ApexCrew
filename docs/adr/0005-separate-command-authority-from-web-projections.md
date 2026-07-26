---
status: accepted
date: 2026-07-26
---

# Separate command authority from Web projections

ApexCrew makes CLI the only command and credential interface. The loopback WebUI renders a token-protected, sanitized `RunReadModel`, while GitHub Pages renders the same projection from deterministic fixture records. Neither Web surface can start, approve, resume, integrate, purge, or execute a Crew Run.

Writable HTMX and React alternatives were rejected because they duplicate authority, approval, authentication, and failure behavior without advancing the evidence-freshness contribution. A hosted backend and temporary tunnel were rejected as ongoing or unreliable public attack surfaces. The consequence is deliberate CLI/WebUI asymmetry: the stable public course URL demonstrates inspection and mechanisms, while real work remains local and explicitly controlled.
