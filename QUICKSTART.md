# Quickstart — Hermes A2A Plugin

Get up and running in 5 minutes.

## 1. Install

```bash
curl -sSL https://raw.githubusercontent.com/your-org/hermes-agent-a2a/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/your-org/hermes-agent-a2a.git ~/.hermes/plugins/hermes-agent-a2a
pip install -e ~/.hermes/plugins/hermes-agent-a2a
```

## 2. Configure the vault

```bash
cp ~/.hermes/plugins/hermes-agent-a2a/vault.yaml.example \
   ~/.hermes/profiles/default/a2a/vault.yaml
```

Edit `vault.yaml` and fill in:

```yaml
telegram:
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"

agent:
  name: "your-agent-name"
```

## 3. Restart hermes-agent

```bash
# Restart your hermes-agent process, then:
```

## 4. Test

```bash
a2a_list
```

You should see your agent's capabilities listed. That's it — you're running an A2A server and have the 4 tools wired in.
