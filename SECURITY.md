# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 3.x | Yes |
| 2.x | No |
| 1.x | No |

## Reporting a Vulnerability

Found a security issue? Please report it via:

- **GitHub Issues**: https://github.com/emiltsoi/hermes-agent-a2a/issues/new?assignees=&labels=security&template=security-vulnerability.md
- **Private disclosure**: contact the maintainer directly if public issue details would expose a live secret or exploitable deployment

Please do not disclose security issues publicly until a fix is available.

## Security Model

`hermes-agent-a2a` has two security surfaces:

1. **Inbound A2A server** — receives JSON-RPC `tasks/send` and `tasks/get` requests.
2. **Outbound A2A tools** — call Hermes fleet agents or external A2A-compatible agents.

The plugin is designed to keep credentials out of source control and prompt history.

## Identity and Secret Storage

Identity files live under:

```text
~/.hermes/fleet/a2a/agents/<agent-name>/identity.yaml
```

Auth values should reference environment variables:

```yaml
transports:
  a2a_rpc:
    url: https://external.example/a2a/rpc
    auth:
      type: api_key
      header: X-API-Key
      value_env: EXTERNAL_A2A_KEY
```

Supported auth references:

- `token_env` for bearer tokens
- `value_env` for API-key/custom-header values
- `secret_env` for internal webhook/HMAC-style secrets

Avoid storing raw `token`, `value`, or `secret` fields in committed files.

## Inbound Server Authentication

Set these for production or any non-local deployment:

```bash
export A2A_REQUIRE_AUTH=true
export A2A_AUTH_TOKEN='strong-random-token'
```

When `A2A_REQUIRE_AUTH=true`, inbound requests must provide:

```text
Authorization: Bearer <A2A_AUTH_TOKEN>
```

If `A2A_REQUIRE_AUTH` is not enabled, unauthenticated localhost requests may be accepted for local development. Do not rely on that in production.

## Bind Address

Default bind settings are conservative:

```bash
A2A_HOST=127.0.0.1
A2A_PORT=8081
```

Only bind to `0.0.0.0` or a public interface when:

- inbound auth is required,
- network ACLs/firewalls are configured,
- logs are monitored,
- and the profile is intended to receive remote A2A calls.

## Outbound URL Validation

Direct URL calls reject loopback targets. Named registry calls only allow loopback when the identity explicitly opts in with:

```yaml
transports:
  a2a_rpc:
    allow_loopback: true
```

Use this only for trusted local Hermes fleet agents.

## External Agent Registration

`a2a_discover(register=True, ...)` can create local identity files for external agents. Prefer:

```text
auth_token_env="EXTERNAL_A2A_TOKEN"
auth_value_env="EXTERNAL_A2A_KEY"
```

The registration path intentionally avoids persisting raw runtime secrets when env-var names are provided.

## Prompt and Payload Safety

The server applies inbound sanitization, outbound filtering, audit logging, response-size limits, and basic rate limiting. These controls reduce risk but do not replace:

- tool allowlisting,
- human review for sensitive operations,
- least-privilege API keys,
- short-lived credentials,
- and deployment network controls.

## Operational Checklist

Before exposing this plugin beyond localhost:

- Set `A2A_REQUIRE_AUTH=true`.
- Set a strong `A2A_AUTH_TOKEN`.
- Store third-party secrets in environment variables, not YAML files.
- Confirm `A2A_HOST` is intentional.
- Review identity files for raw secrets before committing.
- Use `a2a_help(topic="security")` and `a2a_help(topic="external_requirements")` during onboarding.
