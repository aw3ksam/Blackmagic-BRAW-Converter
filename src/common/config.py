"""
Configuration Loader and Schema Validator for BRAW Video Converter.
"""

import os
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field
import yaml


@dataclass
class StorageConfig:
    ingest_dir: Path
    processing_dir: Path
    completed_dir: Path
    archive_dir: Path
    failed_dir: Path

    def ensure_directories(self) -> None:
        """Ensures that all storage directories exist."""
        for path in [
            self.ingest_dir,
            self.processing_dir,
            self.completed_dir,
            self.archive_dir,
            self.failed_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class WatcherConfig:
    poll_interval: float = 2.0
    stability_checks: int = 3
    stability_delay: float = 2.0
    extensions: List[str] = field(default_factory=lambda: [".braw"])
    include_sidecars: bool = True


@dataclass
class AudioConfig:
    codec: str = "aac"
    sample_rate: int = 48000
    bit_depth: int = 16
    bitrate_kbps: int = 320


@dataclass
class ColorConfig:
    mode: str = "lut"
    lut_path: str = "Blackmagic Gen 5 Film to Extended Video.cube"
    fallback_lut_path: str = "Blackmagic Film to Extended Video v4.cube"


@dataclass
class TranscodeConfig:
    container: str = "mp4"
    codec: str = "H265"
    encoding_profile: str = "Main10"
    video_quality: str = "Best"
    bitrate_mbps: int = 0
    resolution: str = "source"
    frame_rate: str = "source"
    audio: AudioConfig = field(default_factory=AudioConfig)
    color: ColorConfig = field(default_factory=ColorConfig)


@dataclass
class EngineConfig:
    type: str = "ffmpeg"
    ffmpeg_path: str = "ffmpeg"
    decoder_path: str = "bin/braw_decode"
    hardware_acceleration: bool = True


# Kept for backward compatibility
@dataclass
class DaVinciConfig:
    app_path: str = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"
    auto_start_headless: bool = False
    launch_timeout: int = 45
    project_name_prefix: str = "BRAW_Transcode_Job"
    cleanup_projects_after_render: bool = True


@dataclass
class AppConfig:
    storage: StorageConfig
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    transcode: TranscodeConfig = field(default_factory=TranscodeConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    davinci: DaVinciConfig = field(default_factory=DaVinciConfig)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Loads configuration from YAML file and returns an AppConfig instance."""
    if not config_path:
        # Check standard locations
        candidates = [
            Path("config/config.yaml"),
            Path("config/config.default.yaml"),
            Path(__file__).parent.parent.parent / "config" / "config.yaml",
            Path(__file__).parent.parent.parent / "config" / "config.default.yaml",
        ]
        for candidate in candidates:
            if candidate.is_file():
                config_path = str(candidate)
                break

    if not config_path or not Path(config_path).is_file():
        raise FileNotFoundError(f"Configuration file not found at: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    storage_data = data.get("storage", {})
    storage = StorageConfig(
        ingest_dir=Path(storage_data.get("ingest_dir", "./watch_folders/00_IN_INGEST")).resolve(),
        processing_dir=Path(storage_data.get("processing_dir", "./watch_folders/01_PROCESSING")).resolve(),
        completed_dir=Path(storage_data.get("completed_dir", "./watch_folders/02_COMPLETED_MP4")).resolve(),
        archive_dir=Path(storage_data.get("archive_dir", "./watch_folders/03_ARCHIVE_BRAW")).resolve(),
        failed_dir=Path(storage_data.get("failed_dir", "./watch_folders/99_FAILED")).resolve(),
    )

    watcher_data = data.get("watcher", {})
    watcher = WatcherConfig(
        poll_interval=float(watcher_data.get("poll_interval", 2.0)),
        stability_checks=int(watcher_data.get("stability_checks", 3)),
        stability_delay=float(watcher_data.get("stability_delay", 2.0)),
        extensions=[ext.lower() for ext in watcher_data.get("extensions", [".braw"])],
        include_sidecars=bool(watcher_data.get("include_sidecars", True)),
    )

    transcode_data = data.get("transcode", {})
    audio_data = transcode_data.get("audio", {})
    audio = AudioConfig(
        codec=str(audio_data.get("codec", "aac")),
        sample_rate=int(audio_data.get("sample_rate", 48000)),
        bit_depth=int(audio_data.get("bit_depth", 16)),
        bitrate_kbps=int(audio_data.get("bitrate_kbps", 320)),
    )

    color_data = transcode_data.get("color", {})
    color = ColorConfig(
        mode=str(color_data.get("mode", "lut")),
        lut_path=str(color_data.get("lut_path", "Blackmagic Gen 5 Film to Extended Video.cube")),
        fallback_lut_path=str(color_data.get("fallback_lut_path", "Blackmagic Film to Extended Video v4.cube")),
    )

    transcode = TranscodeConfig(
        container=str(transcode_data.get("container", "mp4")),
        codec=str(transcode_data.get("codec", "H265")),
        encoding_profile=str(transcode_data.get("encoding_profile", "Main10")),
        video_quality=str(transcode_data.get("video_quality", "Best")),
        bitrate_mbps=int(transcode_data.get("bitrate_mbps", 0)),
        resolution=str(transcode_data.get("resolution", "source")),
        frame_rate=str(transcode_data.get("frame_rate", "source")),
        audio=audio,
        color=color,
    )

    engine_data = data.get("engine", {}) or data.get("ffmpeg", {})
    engine = EngineConfig(
        type=str(engine_data.get("type", "ffmpeg")),
        ffmpeg_path=str(engine_data.get("ffmpeg_path", "ffmpeg")),
        decoder_path=str(engine_data.get("decoder_path", "bin/braw_decode")),
        hardware_acceleration=bool(engine_data.get("hardware_acceleration", True)),
    )

    davinci_data = data.get("davinci", {})
    davinci = DaVinciConfig(
        app_path=str(davinci_data.get("app_path", "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve")),
        auto_start_headless=bool(davinci_data.get("auto_start_headless", False)),
        launch_timeout=int(davinci_data.get("launch_timeout", 45)),
        project_name_prefix=str(davinci_data.get("project_name_prefix", "BRAW_Transcode_Job")),
        cleanup_projects_after_render=bool(davinci_data.get("cleanup_projects_after_render", True)),
    )

    return AppConfig(
        storage=storage,
        watcher=watcher,
        transcode=transcode,
        engine=engine,
        davinci=davinci,
    )
