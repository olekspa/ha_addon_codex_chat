# Codex Chat Add-on

Home Assistant add-on that provides a chat UI and backend proxy for a Codex relay.

## Features
- List and open Codex threads.
- Start new threads and resume existing threads.
- Send chat turns and wait for completion.
- Keeps relay token server-side (not exposed to browser JS).
- Thread UX: search and quick new-thread creation.
- Message UX: Enter-to-send, retry failed send, pending/failed states.
- Performance: short thread-list cache and delta-based polling support.
- Optional Home Assistant TTS for assistant replies (manual + auto-speak).
- Optional Home Assistant Assist text processing (`conversation.process`) from chat.
- Optional HA webhook notifications for user push workflows.
- Compatible with `funis_conversation` custom Assist agent integration (native Assist routing to Funis).

## Add-on Options
- `relay_url`: Base URL of your relay service, e.g. `http://192.168.1.50:8765`
- `relay_token`: Bearer token configured in relay (`CODEX_RELAY_TOKEN`)
- `default_wait`: Wait for turn completion before returning response
- `wait_timeout`: Max wait seconds for turn completion
- `poll_interval`: Poll interval when waiting for turn completion
- `tts_enabled`: Enable auto-speak for assistant replies
- `tts_service`: HA service to call, e.g. `tts.speak`
- `tts_entity_id`: Optional TTS entity (for provider selection, e.g. Cloud TTS entity)
- `tts_media_player_entity_id`: Target media player entity used for playback
- `assist_enabled`: Enable auto-send of Codex replies to Assist
- `assist_agent_id`: Optional Assist agent id override
- `assist_language`: Optional language override for Assist processing
- `notify_webhook_id`: Webhook id used by `/api/ha/notify` (default: `velox_funis_webhook`)

When `tts_service` is `tts.speak`, set `tts_media_player_entity_id`.
If your Home Assistant Cloud TTS provider is configured in HA, this add-on will use it via the normal HA service call path.
Assist integration uses Home Assistant `conversation.process` service through the Supervisor Core API.

## Webhook notifications
Use add-on endpoint:
- `POST /api/ha/notify`

Request body:
```json
{
  "title": "Funis",
  "message": "Build completed",
  "level": "info"
}
```

The add-on posts this payload to:
- `/api/webhook/<notify_webhook_id>` in Home Assistant Core.

For your setup:
- `notify_webhook_id = velox_funis_webhook`
- Status: validated on 2026-02-22 (HTTP 200 and user-confirmed push delivery)

Recommended HA automation for that webhook:
- Trigger: Webhook (`velox_funis_webhook`, POST)
- Action 1: `notify.mobile_app_<your_phone>`
- Action 2 (optional fallback): `persistent_notification.create`
- Notification message template: `{{ trigger.json.message | default('No message') }}`

Mom-targeted variant:
- Create webhook automation `velox_funis_webhook2` with the same message template.

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

So version `0.3.0` must exist as image tags:
- `ghcr.io/olekspa/amd64-codex_chat:0.3.0`
- `ghcr.io/olekspa/aarch64-codex_chat:0.3.0`
- `ghcr.io/olekspa/armv7-codex_chat:0.3.0`
