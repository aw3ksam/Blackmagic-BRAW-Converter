"""
Unified Command-Line Interface for BRAW Video Converter (v4.0).
Standalone transcode engine powered by Blackmagic RAW SDK, FFmpeg, and Camera Ingest.
Supports background hot-folder monitoring, camera auto-transfer, manual batch transcoding, LUT inspection, and environment diagnostics.
"""

import os
import sys
import time
import argparse
import signal
import shutil
from pathlib import Path
from typing import Optional

from src.common.config import load_config, AppConfig
from src.common.logger import setup_logger
from src.common.watcher import FolderWatcher
from src.ffmpeg_engine.ffmpeg_pipeline import FFmpegPipeline
from src.ffmpeg_engine.lut_manager import LutManager
from src.ffmpeg_engine.decoder_bridge import DecoderBridge

logger = setup_logger("braw_cli")


def transcode_single_file(
    braw_path: Path,
    config: AppConfig,
    pipeline: Optional[FFmpegPipeline] = None,
    output_dir: Optional[Path] = None,
) -> bool:
    """Executes full standalone transcode pipeline for a single BRAW clip."""
    logger.info(f"Starting transcode job for: {braw_path.name}")
    if pipeline is None:
        pipeline = FFmpegPipeline(config=config.transcode, ffmpeg_path=config.engine.ffmpeg_path)

    dest_dir = output_dir or config.storage.completed_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    output_file = dest_dir / f"{braw_path.stem}.{config.transcode.container}"

    try:
        success = pipeline.transcode_clip(
            braw_path=braw_path,
            output_file=output_file,
        )
        if success:
            logger.info(f"Transcode succeeded for {braw_path.name} -> {output_file}")
        else:
            logger.error(f"Transcode failed for {braw_path.name}")
        return success
    except Exception as e:
        logger.exception(f"Transcode exception for {braw_path.name}: {e}")
        return False


def cmd_watch(args):
    """Starts the hot-folder watcher daemon."""
    config = load_config(args.config)
    logger.info("Initializing BRAW Hot Folder Watcher (v4.0 Standalone Engine)...")
    logger.info(f"Ingest Hot Folder: {config.storage.ingest_dir}")
    logger.info(f"Output MP4 Folder: {config.storage.completed_dir}")
    logger.info(f"LUT: {config.transcode.color.lut_path}")
    logger.info(f"Codec: {config.transcode.codec} (Profile: {config.transcode.encoding_profile})")

    # Initialize health server if available
    health_port = getattr(args, "health_port", 8765)
    try:
        from debug_tools.core.health_server import start_health_server
        health_srv = start_health_server(port=health_port)
        logger.info(f"Embedded Health API running on http://127.0.0.1:{health_port}")
    except Exception as e:
        logger.debug(f"Health server not started: {e}")
        health_srv = None

    # Initialize shared pipeline instance
    pipeline = FFmpegPipeline(config=config.transcode, ffmpeg_path=config.engine.ffmpeg_path)

    def on_transcode(processing_braw_path: Path) -> bool:
        return transcode_single_file(
            braw_path=processing_braw_path,
            config=config,
            pipeline=pipeline,
        )

    watcher = FolderWatcher(config=config, transcode_callback=on_transcode)
    watcher.start()

    # Graceful shutdown handler
    def handle_sigint(signum=None, frame=None):
        logger.info("Shutdown signal received. Stopping watcher and cancelling active pipelines...")
        if health_srv:
            health_srv.stop()
        watcher.stop()
        pipeline.cancel_active_jobs()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    logger.info("Hot Folder Watcher is running. Press Ctrl+C to exit.")
    while True:
        time.sleep(1.0)


def cmd_transcode(args):
    """Manually transcodes a file or directory."""
    config = load_config(args.config)
    target = Path(args.input_path).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else config.storage.completed_dir

    if not target.exists():
        logger.error(f"Input path does not exist: {target}")
        sys.exit(1)

    pipeline = FFmpegPipeline(config=config.transcode, ffmpeg_path=config.engine.ffmpeg_path)

    if target.is_file():
        success = transcode_single_file(
            braw_path=target,
            config=config,
            pipeline=pipeline,
            output_dir=output_dir,
        )
        sys.exit(0 if success else 1)
    elif target.is_dir():
        braw_files = sorted(list(target.glob("*.braw")) + list(target.glob("*.BRAW")))
        logger.info(f"Found {len(braw_files)} BRAW files in {target}")
        successes = 0
        for braw_file in braw_files:
            if transcode_single_file(
                braw_path=braw_file,
                config=config,
                pipeline=pipeline,
                output_dir=output_dir,
            ):
                successes += 1
        logger.info(f"Completed batch transcode: {successes}/{len(braw_files)} succeeded.")
        sys.exit(0 if successes == len(braw_files) else 1)


def cmd_list_luts(args):
    """Scans and lists available 3D LUTs."""
    lut_mgr = LutManager()
    luts = lut_mgr.list_available_luts()
    print("\n" + "=" * 60)
    print(" Available 3D LUT Profiles (Bundled & Custom)")
    print("=" * 60)
    for idx, item in enumerate(luts, 1):
        print(f"[{idx:02d}] {item['name']} ({item['filename']})")
    print(f"\nTotal LUTs available: {len(luts)}\n")


def cmd_test_env(args):
    """Diagnoses system requirements, FFmpeg, and native BRAW decoder setup."""
    print("\n" + "=" * 60)
    print(" BRAW Video Converter — Environment Diagnostics (v4.0)")
    print("=" * 60)

    # 1. Python Environment
    print(f"• Python Version: {sys.version.split()[0]} ({sys.executable})")

    # 2. FFmpeg and VideoToolbox
    ffmpeg_path = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if Path(ffmpeg_path).is_file():
        print(f"• FFmpeg Executable: FOUND ({ffmpeg_path})")
    else:
        print("• FFmpeg: NOT FOUND in PATH")

    # 3. Native BRAW Decoder Binary
    try:
        bridge = DecoderBridge()
        print(f"• Native BRAW Decoder (braw_decode): FOUND ({bridge.binary_path})")
    except Exception as e:
        print(f"• Native BRAW Decoder (braw_decode): NOT FOUND ({e})")

    # 4. Blackmagic RAW SDK Framework
    framework_path = Path("Documents/Blackmagic RAW SDK/Mac/Libraries/BlackmagicRawAPI.framework").resolve()
    print(f"• Blackmagic RAW SDK Framework: {'FOUND' if framework_path.exists() else 'MISSING'} ({framework_path})")

    # 5. LUT Library
    lut_mgr = LutManager()
    available_luts = lut_mgr.list_available_luts()
    print(f"• Bundled 3D LUTs: {len(available_luts)} profiles available")
    resolved_default = lut_mgr.resolve_lut("Blackmagic Gen 5 Film to Extended Video.cube")
    print(f"• Default Gen 5 LUT: {'RESOLVED' if resolved_default else 'UNRESOLVED'} ({resolved_default})")

    print("=" * 60 + "\n")


def cmd_camera_service(args):
    """Launches headless Blackmagic Camera background service."""
    from src.camera.service import CameraServiceDaemon
    daemon = CameraServiceDaemon(
        camera_ip=args.camera_ip,
        camera_ftp=args.camera_ftp,
        dest_dir=args.dest_dir,
    )
    if args.auto_start:
        daemon.tool1.activate()
    daemon.run()


def main():
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("-c", "--config", type=str, default=None, help="Path to config.yaml")
    config_parser.add_argument("--health-port", type=int, default=8765, help="Port for embedded Health API (default: 8765)")

    parser = argparse.ArgumentParser(
        description="BRAW to H.265 MP4 Automated Video Converter (v4.0 Standalone with Camera Ingest)",
        parents=[config_parser],
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Watch command
    sub_watch = subparsers.add_parser("watch", parents=[config_parser], help="Start background hot folder watcher")
    sub_watch.set_defaults(func=cmd_watch)

    # Camera service command
    sub_cam = subparsers.add_parser("camera-service", help="Start background camera ingest service")
    sub_cam.add_argument("--camera-ip", type=str, default="192.168.1.118", help="Camera IP address")
    sub_cam.add_argument("--camera-ftp", type=str, default="ftp://PYXIS-6K.local", help="Camera FTP host/URL")
    sub_cam.add_argument("--dest-dir", type=str, default="./watch_folders/00_IN_INGEST", help="Destination ingest folder")
    sub_cam.add_argument("--auto-start", action="store_true", help="Auto-activate Tool 1")
    sub_cam.set_defaults(func=cmd_camera_service)

    # Transcode command
    sub_trans = subparsers.add_parser("transcode", parents=[config_parser], help="Transcode a file or directory manually")
    sub_trans.add_argument("input_path", type=str, help="Path to .braw file or directory")
    sub_trans.add_argument("-o", "--output-dir", type=str, default=None, help="Custom output directory")
    sub_trans.set_defaults(func=cmd_transcode)

    # List LUTs command
    sub_luts = subparsers.add_parser("list-luts", parents=[config_parser], help="List available 3D LUTs")
    sub_luts.set_defaults(func=cmd_list_luts)

    # Diagnostics command
    sub_diag = subparsers.add_parser("test-env", parents=[config_parser], help="Run environment diagnostics")
    sub_diag.set_defaults(func=cmd_test_env)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
