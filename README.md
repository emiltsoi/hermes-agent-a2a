# HermesA2A

Self-configuring A2A (Agent-to-Agent) protocol plugin for Hermes Agent.

**Fleet-agnostic. Vault resolution chain. Auto-source bootstrap. Multi-platform.**

## Features

- **Vault Resolution Chain**: agent vault -> profile vault -> env vars -> explicit config
- **Auto-Source Bootstrap**: zero-config source resolution for standard setups
- **Multi-Platform**: Telegram, Discord, Matrix (pluggable)
- **Fail-Fast Validation**: plugin refuses to start if identity resolution fails

## Install

```bash
hermes plugin install https://github.com/<org>/hermes-a2a
```

## Configure

```yaml
plugins:
  enabled:
    - hermes-a2a-v2

a2a:
  enabled: true
  vault: auto
```

## License

MIT
