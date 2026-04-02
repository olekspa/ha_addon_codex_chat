# Codex Chat Add-on

Home Assistant add-on that provides a chat UI and backend proxy for a Codex relay.

## Features
- List and open Codex threads.
- Start new threads and resume existing threads.
- Send chat turns and wait for completion.
- Keeps relay token server-side (not exposed to browser JS).
- Per-user route isolation from HA ingress identity.
- Alex-admin route switching (Lentus/Mulsus) with compact selector.
- Thread UX: search and quick new-thread creation.
- Message UX: Enter-to-send, retry failed send, pending/failed states.
- Performance: short thread-list cache and delta-based polling support.
- Optional Home Assistant TTS for assistant replies (manual + auto-speak).
- Optional Home Assistant Assist text processing (`conversation.process`) from chat.
- Optional HA webhook notifications for user push workflows.

## Add-on Options
- `relay_url`: Lentus relay URL, e.g. `http://192.168.1.50:8765`
- `relay_token`: Lentus relay bearer token (`CODEX_RELAY_TOKEN`)
- `mulsus_relay_url`: Mulsus relay URL, e.g. `http://192.168.1.10:8765`
- `mulsus_relay_token`: Mulsus relay bearer token
- `admin_person_entity_id`: Admin person entity allowed to switch routes (default: `person.alex`)
- `mulsus_person_entity_id`: Person entity pinned to Mulsus route (default: `person.tetyana`)
- `lentus_agent_label`: UI label for Lentus route (default: `Lentus`)
- `mulsus_agent_label`: UI label for Mulsus route (default: `Mulsus`)
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
- `notify_webhook_id`: Webhook id used by `/api/ha/notify` (default: `lentus_agent_webhook`)

When `tts_service` is `tts.speak`, set `tts_media_player_entity_id`.
If your Home Assistant Cloud TTS provider is configured in HA, this add-on will use it via the normal HA service call path.
Assist integration uses Home Assistant `conversation.process` service through the Supervisor Core API.

## Required add-on permissions
- `homeassistant_api: true` must be enabled in add-on `config.yaml`.
- Per-user route mapping reads person entities from Home Assistant Core API; without this permission, `/api/session` can fail with `401 Unauthorized`.

## Multi-user route policy
- Routing is derived from ingress header `X-Remote-User-Id` and person entity `attributes.user_id`.
- `admin_person_entity_id` user:
  - Can select either route in UI.
  - Defaults to Lentus on page load.
- `mulsus_person_entity_id` user:
  - Is pinned to Mulsus only.
  - Does not see the route selector.
- Unmapped users receive `403` on API calls.
- UI intentionally does not display model name yet (no reliable resolved model in current thread payloads).

## Webhook notifications
Use add-on endpoint:
- `POST /api/ha/notify`

Request body:
```json
{
  "title": "Lentus",
  "message": "Build completed",
  "level": "info"
}
```

The add-on posts this payload to:
- `/api/webhook/<notify_webhook_id>` in Home Assistant Core.

For your setup:
- `notify_webhook_id = lentus_agent_webhook`
- Status: validated on 2026-02-22 (HTTP 200 and user-confirmed push delivery)

Recommended HA automation for that webhook:
- Trigger: Webhook (`lentus_agent_webhook`, POST)
- Action 1: `notify.mobile_app_<your_phone>`
- Action 2 (optional fallback): `persistent_notification.create`
- Notification templates:
  - `title`: `{{ trigger.json.title | default('Lentus') }}`
  - `message`: `{{ trigger.json.message | default('No message') }}`
  - `data.url`: `{{ trigger.json.data.url | default('') }}`
  - `data.clickAction`: `{{ trigger.json.data.clickAction | default('') }}`
  - `data.uri`: `{{ trigger.json.data.uri | default('') }}`
  - Optional action button URI: set action to `URI` and use `{{ trigger.json.data.thread_url | default('') }}`

Mom-targeted variant:
- Create webhook automation `lentus_agent_webhook2` with the same message template.

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
- `image: ghcr.io/olekspa/lentus-agent-{arch}-codex_chat`

So version `0.4.1` must exist as image tags:
- `ghcr.io/olekspa/lentus-agent-amd64-codex_chat:0.4.3`
- `ghcr.io/olekspa/lentus-agent-aarch64-codex_chat:0.4.3`
- `ghcr.io/olekspa/lentus-agent-armv7-codex_chat:0.4.3`
