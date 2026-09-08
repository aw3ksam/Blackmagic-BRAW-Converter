# Blackmagic BRAW Converter

An automated hot-folder ingest and high-performance video transcoding workstation for Blackmagic RAW (`.braw`) footage.

Built with **Electron Forge + Vite** for the cross-platform desktop UI, backed by a zero-copy **Metal GPU 3D LUT + Apple VideoToolbox / FFmpeg** transcoding engine, and featuring direct **Blackmagic Camera REST & FTP Auto-Ingest** (PYXIS 6K, Cinema Camera 6K, Pocket series).

---

## ✨ Key Features

* **⚡ Ultra-Fast Metal GPU Transcoding**: Direct in-process Metal GPU 3D LUT Compute Shader and Apple VideoToolbox hardware encoder producing pristine 10-bit H.265 (HEVC Main10) and H.264 deliverables at ~38+ fps on 6K footage with zero intermediate files.
* **🎥 Blackmagic Camera Direct Auto-Ingest**: Automatically connects to your Blackmagic camera (PYXIS 6K, Cinema Camera 6K, etc.) over REST and FTP, listens for record stop triggers (`recording: false`), isolates newly recorded takes, and transfers clips straight into the ingest queue.
* **📁 Automated Hot-Folder Pipeline**: Drop `.braw` clips into `00_IN_INGEST`. The multi-stage stability watcher verifies write completion, processes files, moves deliverables to `02_COMPLETED_MP4`, and archives source files to `03_ARCHIVE_BRAW`.
* **🎨 23 Bundled Blackmagic 3D LUTs**: Full Blackmagic Generation 5 and Gen 4 film-to-video / extended video LUT color conversions included out of the box in `assets/luts/`.
* **🖥️ Modern Desktop Workstation**: Responsive Electron dashboard with live encoding telemetry, FPS monitoring, queue status, camera connection management, and integrated log streaming.
* **🔓 Zero DaVinci Resolve Dependency**: Fully standalone. Runs without needing DaVinci Resolve Studio or dongles installed.

---

## 🚀 Quick Start

### 1. Prerequisites
* **macOS**: Sonoma 14+ or Sequoia 15+ (Apple Silicon recommended for Metal GPU acceleration)
* **Node.js**: v18+ (Node 20+ recommended)
* **Python**: 3.10+ (with `pyyaml`)
* **Optional**: FFmpeg in PATH (used for audio muxing & metadata tagging)

### 2. Setup & Installation

```bash
# Clone or navigate to the project folder
cd "Blackmagic BRAW Converter"

# Install dependencies
npm install

# (Optional) Rebuild the native Metal decoder & VideoToolbox engine
npm run build:decoder
```

> **Note**: A pre-compiled `bin/braw_decode` binary is already included for macOS Apple Silicon. You only need to run `npm run build:decoder` if you modify `src/native/` source files.

### 3. Launch the Application

```bash
# Start the Electron desktop workstation in development mode
npm start
```

### 4. Build Distributable Binaries

```bash
# Package local standalone app into out/
npm run package

# Create platform installers (DMG and ZIP)
npm run make
```

---

## 📂 Hot-Folder Workflow

The automated ingestion pipeline uses 5 immutable lifecycle folders under `watch_folders/`:

```text
watch_folders/
├── 00_IN_INGEST/          # Drop zone: Place new .braw files here (or auto-synced from camera)
├── 01_PROCESSING/         # Working zone: Active transcode in progress
├── 02_COMPLETED_MP4/      # Finished deliverables: 10-bit H.265 / H.264 MP4 videos
├── 03_ARCHIVE_BRAW/       # Archive: Original source .braw files safely stored here
└── 99_FAILED/             # Quarantine: Any corrupted or failed clips for inspection
```

### File Processing Lifecycle
1. Clip lands in `00_IN_INGEST/`.
2. The watcher monitors file size stability across multiple intervals to ensure camera writes/transfers are complete.
3. Once stable, the clip moves to `01_PROCESSING/`.
4. Native Metal GPU engine decodes the RAW stream, applies the selected 3D LUT, and encodes hardware HEVC Main10 video.
5. Final video is saved to `02_COMPLETED_MP4/`.
6. Source `.braw` file is safely moved to `03_ARCHIVE_BRAW/`.

---

## 💻 Command-Line Interface (CLI)

The Python engine can also run as a headless service or manual conversion tool:

```bash
# Start the automated hot-folder watcher daemon
python3 -m src.cli watch

# Transcode a single .braw clip manually
python3 -m src.cli transcode /path/to/clip.braw -o /path/to/output.mp4

# Transcode with a custom 3D LUT and bitrate
python3 -m src.cli transcode /path/to/clip.braw -o /path/to/output.mp4 \
  --lut "Blackmagic Gen 5 Film to Extended Video.cube" \
  --bitrate 35

# List all bundled 3D LUT profiles
python3 -m src.cli luts

# Run Blackmagic Camera auto-ingest sync once
python3 -m src.cli camera-ingest --ip 192.168.1.118
```

---

## ⚙️ Configuration

Application settings, render presets, and watch folder locations can be adjusted in `config/config.yaml`:

```yaml
# Storage and Hot Folder Paths
storage:
  ingest_dir: "./watch_folders/00_IN_INGEST"
  processing_dir: "./watch_folders/01_PROCESSING"
  completed_dir: "./watch_folders/02_COMPLETED_MP4"
  archive_dir: "./watch_folders/03_ARCHIVE_BRAW"
  failed_dir: "./watch_folders/99_FAILED"

# Ingest & Watcher Settings
watcher:
  poll_interval: 2.0
  stability_checks: 3
  stability_delay: 2.0

# Transcode Defaults
transcode:
  container: "mp4"
  codec: "H265"
  encoding_profile: "Main10"
  resolution: "source"
  frame_rate: "source"
  bitrate_mbps: 0 # 0 = Best / auto rate control
  color:
    mode: "lut"
    lut_path: "Blackmagic Gen 5 Film to Extended Video.cube"
```

---

## 🧪 Testing & Verification

Run the comprehensive unit and integration test suite:

```bash
# Run all automated tests
npm test
```

---

## 🏗️ Project Architecture

```text
Blackmagic BRAW Converter/
├── assets/                     # Application icons and 23 bundled Blackmagic 3D LUTs
├── bin/                        # Compiled native braw_decode Metal GPU binary
├── config/                     # YAML configuration presets
├── entitlements/               # macOS Hardened Runtime security entitlements
├── forge.config.js             # Electron Forge build & packaging configuration
├── scripts/                    # Build decoders and automation scripts
├── src/
│   ├── camera/                 # Blackmagic camera REST API & FTP auto-transfer service
│   ├── common/                 # Config loader, logger, and watch folder stability guard
│   ├── electron/               # Electron desktop app (Main, Preload bridge, Renderer UI)
│   ├── ffmpeg_engine/          # Transcoding pipeline, LUT manager, and decoder bridge
│   ├── native/                 # Metal GPU 3D LUT compute shader & VideoToolbox encoder
│   └── cli.py                  # Standalone CLI interface
├── tests/                      # Automated unit and integration tests
├── watch_folders/              # Hot-folder processing directories (.gitkeep)
├── AGENTS.md                   # Core development & architecture guidelines
└── package.json                # Dependencies and build scripts
```

---

## 📄 License

Apache-2.0 License. See source files for details.
