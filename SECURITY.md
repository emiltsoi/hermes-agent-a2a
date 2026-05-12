# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 2.x     | :white_check_mark: |
| 1.x     | :x:                |

## Reporting a Vulnerability

Found a security issue? Please report it via:

- **GitHub Issues** (preferred): https://github.com/emiltsoi/hermes-agent-a2a/issues/new?assignees=&labels=security&template=security-vulnerability.md
- **Private disclosure**: Reach out to the fleet security team directly

Please do not disclose security issues publicly until a fix is available.

## Security Model

- Bot tokens are stored in vault files or injected via environment variables at runtime
- Tokens are resolved via the vault resolution chain: `agent vault → profile vault → env vars → explicit config`
- No tokens are ever committed to the repository
- The plugin validates Telegram bot tokens on every boot via `getMe` API call — revoked tokens cause immediate startup failure
