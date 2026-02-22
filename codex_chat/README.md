# Codex Chat Add-on

Home Assistant add-on that provides a chat UI and backend proxy for a Codex relay.

## Features
- List and open Codex threads.
- Start new threads and resume existing threads.
- Send chat turns and wait for completion.
- Keeps relay token server-side (not exposed to browser JS).

## Add-on Options
- `relay_url`: Base URL of your relay service, e.g. `http://192.168.1.50:8765`
- `relay_token`: Bearer token configured in relay (`CODEX_RELAY_TOKEN`)
- `default_wait`: Wait for turn completion before returning response
- `wait_timeout`: Max wait seconds for turn completion
- `poll_interval`: Poll interval when waiting for turn completion

## Relay Requirements
Relay should run on LAN host:

```bash
export CODEX_RELAY_TOKEN='replace-with-strong-token'
python3 relay/codex_relay.py --host 0.0.0.0 --port 8765
```

## UI Usage
- Open add-on panel via ingress.
- Choose a thread from the left list.
- Send messages in the composer.
- `Ctrl+Enter` / `Cmd+Enter` sends message quickly.
