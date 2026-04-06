# RemoteAgent Auto Update

Remote agent now supports auto-update for installed `.exe` builds.

## 1) Configure environment

Set these keys in `.env` before build:

- `UPDATE_MANIFEST_URL=https://your-domain.com/remote-agent/latest.json`
- `AUTO_UPDATE_ENABLED=true`
- `UPDATE_CHECK_INTERVAL_SECONDS=3600`

- update

`AGENT_VERSION` is automatically stamped during each build and embedded inside the exe via `AGENT_VERSION_BUILD.txt`.

## 2) Manifest format (`latest.json`)

```json
{
  "version": "2026.04.04.220000",
  "url": "https://your-domain.com/remote-agent/RemoteAgent.exe",
  "sha256": "optional_sha256_hex"
}
```

## 3) Update behavior

- Agent checks manifest on startup/connection and periodically.
- If remote version is newer than local `AGENT_VERSION`, agent downloads new `.exe`.
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

## 5) Troubleshooting (`Failed to load Python DLL ... python314.dll`)

- Build with Python `3.12` (the build script now prefers `3.12` automatically).
- Re-publish the newly built `dist/RemoteAgent.exe`.
- If target PCs still fail, install Microsoft Visual C++ Redistributable 2015-2022 (x64).

If you see `Failed to load Python DLL ... python312.dll` during auto-update startup:

- Rebuild with latest config (`RemoteAgent.spec` now uses `upx=False`).
- Add antivirus exclusion for `RemoteAgent.exe` and `%LOCALAPPDATA%\Temp\_MEI*`.
- Ensure enough free disk space in system drive for temp extraction.
- Retry update after killing old process once (`taskkill /F /IM RemoteAgent.exe`).
