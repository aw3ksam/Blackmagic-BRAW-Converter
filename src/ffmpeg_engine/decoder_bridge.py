"""
Native BRAW Decoder Bridge.
Executes the native braw_decode binary and extracts clip metadata.
Supports high-performance in-process GPU Metal + VideoToolbox transcoding.
"""

import os
import sys
import json
import time
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Any

from src.common.logger import setup_logger

logger = setup_logger("decoder_bridge")


@dataclass
class ClipMetadata:
    path: str
    width: int
    height: int
    frame_count: int
    frame_rate: float
    duration_seconds: float
    timecode: str
    has_audio: bool
    audio_channels: int
    audio_sample_rate: int
    audio_bit_depth: int
    camera_type: str


class DecoderBridge:
    """Manages the braw_decode native binary and metadata inspection."""

    def __init__(self, binary_path: Optional[str] = None):
        self.binary_path = self._resolve_binary(binary_path)

    def _resolve_binary(self, custom_path: Optional[str] = None) -> Path:
        """Locates the braw_decode binary or attempts compilation if missing."""
        if custom_path:
            p = Path(custom_path)
            if p.is_file() and os.access(p, os.X_OK):
                return p.resolve()

        root = Path(__file__).parent.parent.parent
        candidates = [
            root / "bin" / "braw_decode",
            Path("bin/braw_decode"),
            Path("/usr/local/bin/braw_decode"),
        ]

        for c in candidates:
            if c.is_file() and os.access(c, os.X_OK):
                return c.resolve()

        build_script = root / "scripts" / "build_decoder.sh"
        if build_script.is_file():
            logger.info("braw_decode binary not found. Triggering automated build...")
            try:
                subprocess.run(["bash", str(build_script)], check=True, cwd=str(root))
                target = root / "bin" / "braw_decode"
                if target.is_file():
                    return target.resolve()
            except Exception as e:
                logger.error(f"Failed to automatically build braw_decode: {e}")

        raise FileNotFoundError(
            "braw_decode binary not found. Please compile it using `bash scripts/build_decoder.sh`"
        )

    def get_clip_metadata(self, braw_path: Path) -> ClipMetadata:
        """Extracts clip metadata using `braw_decode --info`."""
        if not braw_path.is_file():
            raise FileNotFoundError(f"BRAW clip not found: {braw_path}")

        cmd = [str(self.binary_path), "--info", str(braw_path.resolve())]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"braw_decode --info failed for {braw_path.name}: {proc.stderr.strip()}"
            )

        try:
            data = json.loads(proc.stdout)
            return ClipMetadata(
                path=data.get("path", str(braw_path)),
                width=int(data.get("width", 0)),
                height=int(data.get("height", 0)),
                frame_count=int(data.get("frame_count", 0)),
                frame_rate=float(data.get("frame_rate", 24.0)),
                duration_seconds=float(data.get("duration_seconds", 0.0)),
                timecode=str(data.get("timecode", "00:00:00:00")),
                has_audio=bool(data.get("has_audio", False)),
                audio_channels=int(data.get("audio_channels", 2)),
                audio_sample_rate=int(data.get("audio_sample_rate", 48000)),
                audio_bit_depth=int(data.get("audio_bit_depth", 24)),
                camera_type=str(data.get("camera_type", "Blackmagic Camera")),
            )
        except Exception as e:
            raise ValueError(f"Failed to parse clip metadata JSON: {e}\nOutput was:\n{proc.stdout}")

    def transcode_native(
        self,
        braw_path: Path,
        output_file: Path,
        lut_path: Optional[Path] = None,
        codec: str = "hevc",
        bitrate_mbps: int = 50,
        use_main10: bool = True,
        ffmpeg_path: str = "ffmpeg",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_checker: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        Executes zero-copy GPU Metal + VideoToolbox hardware transcode directly in-process.
        """
        if not braw_path.is_file():
            logger.error(f"Input clip not found: {braw_path}")
            return False

        output_file.parent.mkdir(parents=True, exist_ok=True)
        temp_video = output_file.with_name(f".tmp_{output_file.stem}_{int(time.time())}.mp4")

        cmd = [
            str(self.binary_path),
            "--transcode",
            str(braw_path.resolve()),
            "-o",
            str(temp_video.resolve()),
            "--codec",
            "hevc" if "hevc" in codec.lower() or "h265" in codec.lower() else "h264",
            "--bitrate",
            str(bitrate_mbps),
        ]

        if use_main10:
            cmd.append("--main10")
        else:
            cmd.append("--main")

        if lut_path and lut_path.is_file():
            cmd.extend(["--lut", str(lut_path.resolve())])

        logger.info(f"Executing In-Process Metal Transcode: {' '.join(cmd)}")
        start_time = time.time()

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )

        try:
            while proc.poll() is None:
                if cancel_checker and cancel_checker():
                    proc.terminate()
                    break

                line = proc.stderr.readline()
                if not line:
                    time.sleep(0.01)
                    continue

                line_str = line.decode("utf-8", errors="ignore").strip()
                if line_str.startswith("PROGRESS:"):
                    try:
                        progress_data = json.loads(line_str[9:])
                        frame_cur = progress_data.get("frame", 0)
                        frame_tot = progress_data.get("total", 0)
                        pct = float(progress_data.get("percent", 0.0))
                        fps = float(progress_data.get("fps", 0.0))

                        logger.info(
                            f"Transcode Progress: {int(pct)}% (Frame {frame_cur}/{frame_tot}) Speed: {fps:.1f} fps"
                        )
                        sys.stdout.flush()

                        if progress_callback:
                            progress_callback(progress_data)
                    except Exception:
                        pass
                elif line_str:
                    logger.info(f"[native_engine] {line_str}")
                    sys.stdout.flush()

            rc = proc.wait()
            if rc != 0 or not temp_video.is_file() or temp_video.stat().st_size == 0:
                logger.error(f"Native transcode failed with return code {rc}")
                if temp_video.exists():
                    temp_video.unlink(missing_ok=True)
                return False

            # Remux audio from original BRAW file
            logger.info("Muxing audio track and finalizing MP4 container...")
            remux_cmd = [
                ffmpeg_path,
                "-hide_banner",
                "-nostats",
                "-y",
                "-i",
                str(temp_video.resolve()),
                "-i",
                str(braw_path.resolve()),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "320k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output_file.resolve()),
            ]

            remux_proc = subprocess.run(
                remux_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            temp_video.unlink(missing_ok=True)

            if remux_proc.returncode != 0 or not output_file.is_file() or output_file.stat().st_size == 0:
                logger.error(f"Audio remux failed: {remux_proc.stderr.decode('utf-8', errors='ignore')}")
                return False

            elapsed = time.time() - start_time
            size_mb = output_file.stat().st_size / (1024 * 1024)
            logger.info(
                f"Transcode succeeded for {braw_path.name} -> {output_file.name} "
                f"({size_mb:.2f} MB in {elapsed:.2f}s)"
            )
            return True

        except Exception as e:
            logger.exception(f"Exception during native transcode: {e}")
            if temp_video.exists():
                temp_video.unlink(missing_ok=True)
            if output_file.exists():
                output_file.unlink(missing_ok=True)
            return False
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.stderr:
                try:
                    proc.stderr.close()
                except Exception:
                    pass
