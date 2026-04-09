# RemoteAgent Auto Update

Remote agent now supports auto-update for installed `.exe` builds.

## 1) Configure environment

Set these keys in `.env` before build:

- `UPDATE_MANIFEST_URL=https://your-domain.com/remote-agent/latest.json`
- `AUTO_UPDATE_ENABLED=true`
- `UPDATE_CHECK_INTERVAL_SECONDS=3600`
- `UPDATE_DOWNLOAD_RETRY_COUNT=3`
- `UPDATE_DOWNLOAD_RETRY_DELAY_SECONDS=2`
- `MIN_UPDATE_BINARY_SIZE_BYTES=524288`

`AGENT_VERSION` is automatically stamped during each build and embedded inside the exe via `AGENT_VERSION_BUILD.txt`.

## 2) Manifest format (`latest.json`)

```json
{
  "version": "2026.04.04.220000",
  "url": "https://your-domain.com/remote-agent/RemoteAgent.exe",
  "sha256": "recommended_sha256_hex",
  "size": 73423872
}
```

## 3) Update behavior

- Agent checks manifest on startup/connection and periodically.
- If remote version is newer than local `AGENT_VERSION`, agent downloads new `.exe`.
- Download is retried (`.part` temp file) and validated before install.
- Validation includes: executable header check, minimum size check, optional `size` check, optional `sha256` check.
- A detached updater process replaces current `.exe` and starts new version.

## 4) Build

```bat
build_remote_agent.bat
```

Output:

- `dist/RemoteAgent.exe`
- `dist/RemoteAgent_<AGENT_VERSION>.exe`
- `dist/AGENT_VERSION.txt`
- `AGENT_VERSION_BUILD.txt` (build-time embedded version source)
- `dist/.env`

Note: `AGENT_VERSION.txt` is used by the running agent to prevent repeated update loops.
Important: always publish `dist/RemoteAgent.exe` for updates (do not publish `dist/RemoteAgent_<AGENT_VERSION>.exe`).

## 4.1) Generate manifest automatically (admin side)

Use this command after each build/upload:

```bat
py -3 generate_update_manifest.py --exe dist\RemoteAgent.exe --url https://your-domain.com/remote-agent/RemoteAgent.exe --output latest.json
```

It reads `dist\AGENT_VERSION.txt` automatically (or pass `--version`), then writes:

- `version`
- `url`
- `sha256`
- `size`

Or one command to prepare upload files together:

```bat
prepare_update_package.bat dist\RemoteAgent.exe https://your-domain.com/remote-agent/RemoteAgent.exe dist\publish
```

Then upload from `dist\publish`:

- `RemoteAgent.exe`
- `latest.json`

## 5) Troubleshooting (`Failed to load Python DLL ... python314.dll`)

- Build with Python `3.12` (the build script now prefers `3.12` automatically).
- Re-publish the newly built `dist/RemoteAgent.exe`.
- If target PCs still fail, install Microsoft Visual C++ Redistributable 2015-2022 (x64).

If you see `Failed to load Python DLL ... python312.dll` during auto-update startup:

- Rebuild with latest config (`RemoteAgent.spec` now uses `upx=False`).
- Add antivirus exclusion for `RemoteAgent.exe` and `%LOCALAPPDATA%\Temp\_MEI*`.
- Ensure enough free disk space in system drive for temp extraction.
- Retry update after killing old process once (`taskkill /F /IM RemoteAgent.exe`).
