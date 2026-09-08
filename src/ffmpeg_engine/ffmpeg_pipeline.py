"""
Transcoding Pipeline Controller for Blackmagic RAW Clips.
Primary Engine: Zero-Copy GPU Metal + Apple VideoToolbox In-Process Engine (v3.2).
Fallback Engine: FFmpeg Subprocess Pipe Streaming.
"""

import os
import sys
import time
import json
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any

from src.common.config import TranscodeConfig
from src.common.logger import setup_logger
from src.ffmpeg_engine.decoder_bridge import DecoderBridge, ClipMetadata
from src.ffmpeg_engine.lut_manager import LutManager

try:
    from debug_tools.core.health_server import global_app_state
except ImportError:
    global_app_state = None

logger = setup_logger("ffmpeg_pipeline")


class FFmpegPipeline:
    """Manages the full transcoding pipeline from BRAW to MP4/MOV."""

    def __init__(self, config: TranscodeConfig, ffmpeg_path: Optional[str] = None):
        self.config = config
        self.ffmpeg_path = self._resolve_ffmpeg(ffmpeg_path)
        self.decoder_bridge = DecoderBridge()
        self.lut_manager = LutManager()
        self._current_decoder_proc: Optional[subprocess.Popen] = None
        self._current_ffmpeg_proc: Optional[subprocess.Popen] = None
        self._is_cancelled = False

    def _resolve_ffmpeg(self, custom_path: Optional[str] = None) -> str:
        """Finds the ffmpeg binary."""
        if custom_path and shutil.which(custom_path):
            return custom_path

        candidates = [
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
            "ffmpeg",
        ]
        for c in candidates:
            if shutil.which(c):
                return c

        raise FileNotFoundError(
            "FFmpeg executable not found in PATH or standard locations (/opt/homebrew/bin/ffmpeg). "
            "Please ensure FFmpeg is installed."
        )

    def cancel_active_jobs(self) -> None:
        """Cancels any active transcode process."""
        self._is_cancelled = True
        logger.warning("Cancelling active transcode jobs...")
        if self._current_decoder_proc and self._current_decoder_proc.poll() is None:
            try:
                self._current_decoder_proc.terminate()
            except Exception:
                pass
        if self._current_ffmpeg_proc and self._current_ffmpeg_proc.poll() is None:
            try:
                self._current_ffmpeg_proc.terminate()
            except Exception:
                pass

    def transcode_clip(
        self,
        braw_path: Path,
        output_file: Path,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> bool:
        """
        Executes end-to-end transcode of a single BRAW clip.
        Uses high-speed in-process Metal + VideoToolbox engine by default on macOS.
        """
        self._is_cancelled = False
        if not braw_path.is_file():
            logger.error(f"Input clip not found: {braw_path}")
            return False

        # 1. Extract metadata
        logger.info(f"Extracting metadata for {braw_path.name}...")
        try:
            meta = self.decoder_bridge.get_clip_metadata(braw_path)
        except Exception as e:
            logger.exception(f"Failed to read clip metadata for {braw_path.name}: {e}")
            return False

        logger.info(
            f"Clip Info: {meta.width}x{meta.height} @ {meta.frame_rate:.3f} fps, "
            f"{meta.frame_count} frames ({meta.duration_seconds:.2f}s), TC: {meta.timecode}"
        )

        # 2. Resolve LUT
        lut_path: Optional[Path] = None
        if self.config.color.mode != "none":
            lut_path = self.lut_manager.resolve_lut(
                self.config.color.lut_path,
                self.config.color.fallback_lut_path,
            )
            if lut_path:
                logger.info(f"Applying 3D LUT: '{lut_path.name}' ({lut_path})")
            else:
                logger.warning(
                    f"Configured LUT '{self.config.color.lut_path}' could not be resolved. "
                    "Transcoding will proceed with standard color pass."
                )

        codec_setting = self.config.codec.upper()
        bitrate_mbps = self.config.bitrate_mbps
        if bitrate_mbps <= 0:
            bitrate_mbps = 50 if meta.width >= 3840 else 25

        use_main10 = (self.config.encoding_profile.lower() == "main10")

        def _internal_progress(progress_data: Dict[str, Any]):
            if global_app_state:
                try:
                    frame_cur = progress_data.get("frame", 0)
                    frame_tot = progress_data.get("total", 0)
                    fps = float(progress_data.get("fps", 0.0))
                    global_app_state.update_progress(frame_cur, frame_tot, fps)
                except Exception:
                    pass
            if progress_callback:
                try:
                    progress_callback(progress_data)
                except Exception:
                    pass

        # 3. High-Performance In-Process Metal + VideoToolbox Route
        res_cfg = str(self.config.resolution).lower()
        if sys.platform == "darwin" and res_cfg == "source":
            logger.info("Engaging Zero-Copy In-Process Metal + VideoToolbox Engine (v3.2)...")
            success = self.decoder_bridge.transcode_native(
                braw_path=braw_path,
                output_file=output_file,
                lut_path=lut_path,
                codec=codec_setting,
                bitrate_mbps=bitrate_mbps,
                use_main10=use_main10,
                ffmpeg_path=self.ffmpeg_path,
                progress_callback=_internal_progress,
                cancel_checker=lambda: self._is_cancelled,
            )
            if success:
                return True
            logger.warning("In-process native transcode returned false. Falling back to FFmpeg pipe engine...")

        # 4. Fallback Pipe Streaming Engine
        filters: List[str] = []
        if lut_path:
            escaped_lut = str(lut_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            filters.append(f"lut3d=file='{escaped_lut}':interp=trilinear")

        if res_cfg != "source":
            if res_cfg in ("4k", "3840x2160"):
                filters.append("scale=3840:2160")
            elif res_cfg in ("1080p", "1920x1080"):
                filters.append("scale=1920:1080")
            elif res_cfg in ("720p", "1280x720"):
                filters.append("scale=1280:720")

        if codec_setting in ("H265", "HEVC"):
            vcodec = "hevc_videotoolbox" if sys.platform == "darwin" else "libx265"
        elif codec_setting in ("H264", "AVC"):
            vcodec = "h264_videotoolbox" if sys.platform == "darwin" else "libx264"
        elif codec_setting == "PRORES":
            vcodec = "prores_videotoolbox" if sys.platform == "darwin" else "prores_ks"
        else:
            vcodec = "hevc_videotoolbox" if sys.platform == "darwin" else "libx265"

        decoder_cmd = [
            str(self.decoder_bridge.binary_path),
            "--stream",
            str(braw_path.resolve()),
        ]

        ffmpeg_cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "-s",
            f"{meta.width}x{meta.height}",
            "-r",
            f"{meta.frame_rate}",
            "-i",
            "-",
            "-i",
            str(braw_path.resolve()),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0?",
        ]

        if filters:
            ffmpeg_cmd.extend(["-vf", ",".join(filters)])

        ffmpeg_cmd.extend([
            "-c:v",
            vcodec,
        ])

        if sys.platform == "darwin" and "videotoolbox" in vcodec:
            ffmpeg_cmd.extend(["-prio_speed", "true"])

        if "hevc" in vcodec:
            ffmpeg_cmd.extend(["-tag:v", "hvc1"])

        ffmpeg_cmd.extend([
            "-b:v",
            f"{bitrate_mbps}M",
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-movflags",
            "+faststart",
        ])

        audio_codec = self.config.audio.codec.lower()
        if audio_codec == "aac":
            ffmpeg_cmd.extend([
                "-c:a",
                "aac",
                "-b:a",
                f"{self.config.audio.bitrate_kbps}k",
                "-ar",
                str(self.config.audio.sample_rate),
            ])
        else:
            ffmpeg_cmd.extend(["-c:a", "copy"])

        if meta.timecode:
            ffmpeg_cmd.extend(["-timecode", meta.timecode])

        output_file.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_cmd.append(str(output_file.resolve()))

        logger.info(f"Starting fallback pipe transcode: {braw_path.name} -> {output_file.name}")
        start_time = time.time()

        try:
            self._current_decoder_proc = subprocess.Popen(
                decoder_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )

            self._current_ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=self._current_decoder_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
            )

            self._current_decoder_proc.stdout.close()

            while self._current_decoder_proc.poll() is None:
                if self._is_cancelled:
                    break
                line = self._current_decoder_proc.stderr.readline()
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

                        _internal_progress(progress_data)
                    except Exception:
                        pass

            decoder_rc = self._current_decoder_proc.wait()
            ffmpeg_rc = self._current_ffmpeg_proc.wait()

            elapsed = time.time() - start_time
            if decoder_rc == 0 and ffmpeg_rc == 0 and output_file.is_file() and output_file.stat().st_size > 0:
                size_mb = output_file.stat().st_size / (1024 * 1024)
                logger.info(
                    f"Transcode succeeded for {braw_path.name} -> {output_file.name} "
                    f"({size_mb:.2f} MB in {elapsed:.2f}s)"
                )
                return True
            else:
                _, ffmpeg_err = self._current_ffmpeg_proc.communicate()
                logger.error(
                    f"Transcode failed. Decoder code: {decoder_rc}, FFmpeg code: {ffmpeg_rc}\n"
                    f"FFmpeg stderr:\n{ffmpeg_err.decode('utf-8', errors='ignore')}"
                )
                if output_file.exists():
                    output_file.unlink(missing_ok=True)
                return False

        except Exception as e:
            logger.exception(f"Exception during transcode of {braw_path.name}: {e}")
            if output_file.exists():
                output_file.unlink(missing_ok=True)
            return False
        finally:
            if self._current_decoder_proc and self._current_decoder_proc.stderr:
                self._current_decoder_proc.stderr.close()
            if self._current_ffmpeg_proc:
                if self._current_ffmpeg_proc.stdout:
                    self._current_ffmpeg_proc.stdout.close()
                if self._current_ffmpeg_proc.stderr:
                    self._current_ffmpeg_proc.stderr.close()

            self._current_decoder_proc = None
            self._current_ffmpeg_proc = None
