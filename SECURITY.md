# Security Policy

Skill Sync Sidecar is designed for private skill synchronization. Treat synced
skills as executable operational content.

## Supported Version

The current supported line is `v0.1.x`.

## Reporting Issues

Report security issues privately to the repository owner. Do not include
WebDAV credentials, API keys, OAuth tokens, or private skill contents in public
issues.

## Secret Handling

- Do not put credentials inside skill packages.
- Use environment variables or existing tool credential stores for WebDAV auth.
- Keep production WebDAV roots private and permissioned.
- Run `doctor`, `sync-plan`, and `openclaw-gate` before enabling unattended apply.

## Execution Boundary

The sidecar packages skill files and can install them into explicit target
roots. It does not modify tool databases, provider accounts, OAuth sessions, or
OpenClaw service state in the MVP release.
