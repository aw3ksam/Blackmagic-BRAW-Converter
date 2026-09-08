# Developer & AI Agent Guide — Electron Forge & Build Rules

This document defines the core architecture rules, build guidelines, and security constraints that any AI agent or developer **must follow** when developing or modifying the **Blackmagic BRAW Converter** codebase.

---

## 1. Project Overview & Architecture

* **Production Application**: The cross-platform **Electron Forge + Vite** desktop app located at repository root and `src/electron/` backed by a standalone transcoding pipeline using the official **Blackmagic RAW SDK** and **Apple VideoToolbox / FFmpeg**.
* **Core Engine**: The standalone transcoding engine (`src/native/braw_decode.mm`, `src/ffmpeg_engine/`, `src/cli.py`, `src/common/`, `src/camera/`) is orchestrated by the Electron Main process with zero dependency on DaVinci Resolve. Built-in Blackmagic 3D LUTs reside in `assets/luts/`.

---

## 2. Electron Forge & Vite Build Rules

### Entry Points & Manifest (`package.json`)
* The `"main"` field in `package.json` **must always point to** `.vite/build/main.js`.
* Standard npm scripts must be preserved:
  * `"start": "electron-forge start"` — Runs dev server with Vite HMR.
  * `"package": "electron-forge package"` — Generates local unpackaged app binary.
  * `"make": "electron-forge make"` — Produces platform distributables (DMG, ZIP, Squirrel, Deb, RPM).
  * `"build": "npm run package"` — Package convenience alias.
  * `"test": "..."` — Runs test suite.

### Vite Config Files
* **`vite.main.config.mjs`**: Must output `entryFileNames: 'main.js'` and externalize Node built-ins (`electron`, `child_process`, `fs`, `path`, `os`). **Note**: Keep main process dependencies zero-external (like inline YAML serialization) since Electron ASAR packaging does not bundle `node_modules`.
* **`vite.preload.config.mjs`**: Must output `entryFileNames: 'preload.js'` and externalize `electron`.
* **`vite.renderer.config.mjs`**: Bundles the UI from `src/electron/renderer/`.

### Forge Config (`forge.config.js`)
* Keep `packagerConfig` referencing `./assets/icons/icon` (resolves `.icns`, `.ico`, `.png` automatically).
* Keep macOS code signing entitlements pointing to `./entitlements/entitlements.mac.plist` and `./entitlements/entitlements.mac.inherit.plist`.
* **FusesPlugin Note**: FusesPlugin is commented out during ad-hoc signing (`identity: '-'`) because modifying fuse bits after ad-hoc signing invalidates the macOS code signature (`SIGKILL (Code Signature Invalid)`). Re-enable when `APPLE_SIGN_IDENTITY` is configured for developer ID signing.

---

## 3. Strict Security Invariants

When modifying `BrowserWindow` creation or front-end code, you **must adhere to the following**:

1. **Isolation & Sandboxing**:
   ```javascript
   webPreferences: {
     preload: path.join(__dirname, 'preload.js'),
     contextIsolation: true,
     nodeIntegration: false,
     sandbox: true,
     webSecurity: true,
   }
   ```
2. **No Direct Node in Renderer**: Never import `fs`, `child_process`, or `path` inside `src/electron/renderer/`.
3. **Hardened Preload Bridge**: All communication between Renderer and Main process must pass through explicit methods in `src/electron/preload/index.js` using `contextBridge.exposeInMainWorld('electronAPI', { ... })`.

---

## 4. Backend Engine & Hot-Folder Pipeline Rules

1. **5 Immutable Subfolder Names**:
   The folder staging names are fixed and must never be altered or localized:
   * `00_IN_INGEST`
   * `01_PROCESSING`
   * `02_COMPLETED_MP4`
   * `03_ARCHIVE_BRAW`
   * `99_FAILED`
2. **Always Use Unbuffered Python (`-u`)**:
   When spawning `src.cli` in `src/electron/main/index.js`, always include `-u` (e.g. `["-u", "-m", "src.cli", "watch", ...]`). Without this flag, Python buffers `stdout` in block mode and live log streaming will lag.
3. **Graceful Teardown (`SIGINT` first)**:
   Never terminate the backend process with immediate `SIGKILL`. Send `SIGINT` first to allow the watcher and FFmpeg transcoding pipelines to flush frames and close file handles cleanly. Use a 3-second fallback timeout for `SIGTERM`.

---

## 5. Verification Checklist Before Finishing Tasks

Always run the following commands to ensure builds and tests remain green:

```bash
# 1. Verify backend tests pass
npm test

# 2. Verify Electron Forge build & packaging succeeds
npm run package
```
