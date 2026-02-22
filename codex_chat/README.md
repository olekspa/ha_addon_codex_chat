# Codex Chat Add-on

Home Assistant add-on that provides a chat UI and backend proxy for a Codex relay.

## Features
- List and open Codex threads.
- Start new threads and resume existing threads.
- Send chat turns and wait for completion.
- Keeps relay token server-side (not exposed to browser JS).
- Thread UX: search, pinning, archive/unarchive, explicit materialize action.
- Message UX: Enter-to-send, retry failed send, pending/failed states.
- Performance: short thread-list cache and delta-based polling support.

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

## Release process (prebuilt images)
1. Bump `version` in `config.yaml`.
2. Push commit to GitHub.
3. Create matching tag `v<version>` and push it.
4. Wait for workflow `Build Add-on Images` to publish GHCR images.

The add-on uses:
- `image: ghcr.io/olekspa/{arch}-codex_chat`

So version `0.1.1` must exist as image tags:
- `ghcr.io/olekspa/amd64-codex_chat:0.1.1`
- `ghcr.io/olekspa/aarch64-codex_chat:0.1.1`
- `ghcr.io/olekspa/armv7-codex_chat:0.1.1`
