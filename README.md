# Codex Chat Home Assistant Add-on Repository

This repository contains a Home Assistant add-on that provides a chat UI and thread controls for a Codex relay running on your LAN.

## Add-ons
- `codex_chat`: Chat frontend + backend proxy to `relay/codex_relay.py`

## Install in Home Assistant
1. Push this repository to GitHub.
2. In Home Assistant: **Settings -> Add-ons -> Add-on Store -> menu (top-right) -> Repositories**.
3. Add your GitHub repository URL.
4. Install **Codex Chat**.

## Prebuilt image workflow (recommended)
This repository is configured to publish prebuilt multi-arch images to GHCR from GitHub Actions.

1. Update `codex_chat/config.yaml` `version` (example: `0.1.1`).
2. Commit and push to `main`.
3. Create and push matching git tag:
```bash
git tag v0.1.1
git push origin v0.1.1
```
4. GitHub Action `.github/workflows/build-addon-images.yml` builds and pushes:
   - `ghcr.io/<owner>/amd64-codex_chat:0.1.1`
   - `ghcr.io/<owner>/aarch64-codex_chat:0.1.1`
   - `ghcr.io/<owner>/armv7-codex_chat:0.1.1`

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
