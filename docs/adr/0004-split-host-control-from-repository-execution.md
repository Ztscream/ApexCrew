---
status: accepted
date: 2026-07-26
---

# Split trusted host control from repository execution

ApexCrew keeps Coordinator authority, Git/worktree and ref operations, SQLite, credentials, approvals, and recovery on the trusted host, while every repository-owned command runs in a digest-pinned, networkless, least-privilege Docker executor. Only a sanitized action/Verification Snapshot is mounted read-only, and commands run on a bounded disposable copy. This split preserves direct local Git/keyring recovery without granting untrusted tests or package scripts host credentials, persistent filesystem writes, network access, or the Docker socket.

Host-only execution was rejected because path checks and environment filtering cannot substantiate the repository-confinement claim against malicious scripts. Containerizing the whole control plane was rejected because local repository identity, keyring access, and child-container isolation would require complex host mounts or Docker-socket authority. The accepted split makes Docker availability a supported-host prerequisite and explicitly does not defend against a compromised host kernel or Docker daemon.
