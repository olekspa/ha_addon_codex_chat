# Codex Chat Home Assistant Add-on Repository

This repository contains a Home Assistant add-on that provides a chat UI and thread controls for a Codex relay running on your LAN.

## Add-ons
- `codex_chat`: Chat frontend + backend proxy to `relay/codex_relay.py`

## Custom Integration
- `custom_components/funis_conversation`: Home Assistant Conversation Agent so Assist can route to Funis/Codex relay.

## Install in Home Assistant
1. Push this repository to GitHub.
2. In Home Assistant: **Settings -> Add-ons -> Add-on Store -> menu (top-right) -> Repositories**.
3. Add your GitHub repository URL.
4. Install **Codex Chat**.

## Prebuilt image workflow (recommended)
This repository is configured to publish prebuilt multi-arch images to GHCR from GitHub Actions.

1. Update `codex_chat/config.yaml` `version` (example: `0.2.9`).
2. Commit and push to `main`.
3. Create and push matching git tag:
```bash
git tag v0.2.9
git push origin v0.2.9
```
4. GitHub Action `.github/workflows/build-addon-images.yml` builds and pushes:
   - `ghcr.io/<owner>/amd64-codex_chat:0.2.9`
   - `ghcr.io/<owner>/aarch64-codex_chat:0.2.9`
   - `ghcr.io/<owner>/armv7-codex_chat:0.2.9`

Home Assistant then pulls prebuilt images directly (faster, more reliable than local build).

## Required relay setup
Run relay on your LAN host and configure token:

```bash
export CODEX_RELAY_TOKEN='replace-with-strong-token'
python3 relay/codex_relay.py --host 0.0.0.0 --port 8765
```

Then set add-on options:
- `relay_url`: `http://<relay-host>:8765`
- `relay_token`: same token

## Install Funis Assist Agent (custom component)
1. Copy `custom_components/funis_conversation` into your Home Assistant config:
   - `<ha_config>/custom_components/funis_conversation`
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration**.
4. Add **Funis Conversation Agent**.
5. Fill relay settings (`relay_url`, `relay_token`) and keep defaults for:
   - `approval_policy=never`
   - `sandbox_mode=danger-full-access`
6. Go to **Settings -> Voice assistants -> Assist** and select the Funis agent as default.

### Shared thread behavior
- Assist `conversation_id` is mapped to a Codex `threadId` and persisted.
- Threads created by Assist are visible in Codex Chat add-on thread list.
- This allows Assist and Codex Chat UI to continue work on the same underlying thread context.
