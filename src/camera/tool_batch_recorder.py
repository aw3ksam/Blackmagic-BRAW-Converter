"""
Tool 2: Batch Video Recording Generator.
Allows generating batches of 15s, 30s, 45s, or 60s video clips (totaling ~1 hour of footage),
or custom clip count and duration for program debugging.
Dynamically configures camera resolution, codec/bitrate, and executes automated recording sequences.
"""

from __future__ import annotations
from datetime import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .camera_client import CameraClient

logger = logging.getLogger("camera.tool2")

# Presets mapping clip duration in seconds to total count for ~1 hour (3600s)
DURATION_PRESETS = {
    15: {"clips": 240, "label": "15 seconds (240 clips ~ 1 hr)"},
    30: {"clips": 120, "label": "30 seconds (120 clips ~ 1 hr)"},
    45: {"clips": 80,  "label": "45 seconds (80 clips ~ 1 hr)"},
    60: {"clips": 60,  "label": "60 seconds (60 clips ~ 1 hr)"},
}


class BatchRecorderTool:
    """Tool 2: Batch Video Recording Generator for camera debugging."""

    def __init__(
        self,
        camera_client: CameraClient,
        cooldown_seconds: float = 2.5,
        on_event_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.camera = camera_client
        self.cooldown_seconds = cooldown_seconds
        self.on_event_cb = on_event_cb

        # State
        self.is_active = False
        self.state = "idle"  # idle, recording, cooldown, paused, completed, aborted, error
        self.clip_duration = 60
        self.target_clips = 60
        self.current_clip_index = 0
        self.elapsed_total_seconds = 0.0
        self.current_clip_elapsed = 0.0
        self.selected_resolution: Optional[Dict[str, int]] = None
        self.selected_codec: Optional[str] = None
        self.selected_framerate: Optional[str] = None

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._lock = threading.Lock()
        self.error_message: Optional[str] = None

    def _emit(self, event_type: str, data: Dict[str, Any]):
        payload = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "data": data,
        }
        if self.on_event_cb:
            try:
                self.on_event_cb(payload)
            except Exception as e:
                logger.debug(f"Error in batch tool event callback: {e}")

    def get_supported_options(self) -> Dict[str, Any]:
        """Fetch all available resolutions, codecs, and framerates from camera."""
        try:
            formats = self.camera.get_supported_formats()
            current_fmt = self.camera.get_format()

            resolution_options = []
            codec_options = set()

            for fmt in formats:
                rec_res = fmt.get("recordResolution", {})
                w = rec_res.get("width", 0)
                h = rec_res.get("height", 0)
                desc = fmt.get("resolutionDescriptor", {})
                group = desc.get("group", "")
                description = desc.get("description", f"{w}x{h}")
                aspect = desc.get("aspectRatio", "")

                codecs = fmt.get("codecs", [])
                for c in codecs:
                    codec_options.add(c)

                resolution_options.append({
                    "width": w,
                    "height": h,
                    "label": f"{group} - {description} ({w}x{h}, {aspect})" if group else f"{w}x{h}",
                    "codecs": codecs,
                    "frameRates": fmt.get("frameRates", []),
                })

            return {
                "success": True,
                "current_format": current_fmt,
                "resolutions": resolution_options,
                "all_codecs": sorted(list(codec_options)),
                "duration_presets": [
                    {"duration": k, "clips": v["clips"], "label": v["label"]}
                    for k, v in DURATION_PRESETS.items()
                ],
            }
        except Exception as e:
            logger.debug(f"Failed to get supported options: {e}")
            return {
                "success": False,
                "error": str(e),
                "duration_presets": [
                    {"duration": k, "clips": v["clips"], "label": v["label"]}
                    for k, v in DURATION_PRESETS.items()
                ],
            }

    def start_batch(
        self,
        clip_duration: int = 60,
        custom_clip_count: Optional[int] = None,
        codec: Optional[str] = None,
        record_resolution: Optional[Dict[str, int]] = None,
        frame_rate: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Configure camera settings and initiate batch recording sequence."""
        with self._lock:
            if self.is_active and self.state in ("recording", "cooldown", "paused"):
                return {"success": False, "error": "A batch run is already in progress"}

            if clip_duration not in DURATION_PRESETS and not custom_clip_count:
                clip_duration = 60

            self.clip_duration = clip_duration
            self.target_clips = (
                custom_clip_count
                if custom_clip_count and custom_clip_count > 0
                else DURATION_PRESETS.get(clip_duration, {"clips": 60})["clips"]
            )
            self.selected_codec = codec
            self.selected_resolution = record_resolution
            self.selected_framerate = frame_rate

            self.is_active = True
            self.state = "starting"
            self.current_clip_index = 0
            self.elapsed_total_seconds = 0.0
            self.current_clip_elapsed = 0.0
            self.error_message = None

            self._stop_event.clear()
            self._pause_event.clear()

            # Apply camera format if specified
            if codec and record_resolution:
                try:
                    logger.info(f"Applying camera format: {codec}, {record_resolution}")
                    self.camera.set_format(
                        codec=codec,
                        record_resolution=record_resolution,
                        frame_rate=frame_rate,
                    )
                    time.sleep(1.0)  # Allow sensor/codec reconfiguration
                except Exception as e:
                    self.is_active = False
                    self.state = "error"
                    self.error_message = f"Failed to apply camera format: {e}"
                    return {"success": False, "error": self.error_message}

            self._thread = threading.Thread(
                target=self._batch_worker_loop, daemon=True, name="Tool2-BatchWorker"
            )
            self._thread.start()

            self._emit("tool2_batch_started", self.get_status())
            return {"success": True, "status": self.get_status()}

    def pause_batch(self) -> Dict[str, Any]:
        """Pause the batch recording after the current clip or during cooldown."""
        with self._lock:
            if not self.is_active or self.state not in ("recording", "cooldown"):
                return {"success": False, "error": "No active running batch to pause"}
            self._pause_event.set()
            self.state = "paused"
            self._emit("tool2_batch_paused", self.get_status())
            return {"success": True, "status": self.get_status()}

    def resume_batch(self) -> Dict[str, Any]:
        """Resume a paused batch."""
        with self._lock:
            if not self.is_active or self.state != "paused":
                return {"success": False, "error": "Batch is not paused"}
            self._pause_event.clear()
            self.state = "cooldown"
            self._emit("tool2_batch_resumed", self.get_status())
            return {"success": True, "status": self.get_status()}

    def stop_batch(self) -> Dict[str, Any]:
        """Abort/Cancel the current batch run."""
        with self._lock:
            if not self.is_active:
                return {"success": True, "message": "Batch is not active"}
            self.is_active = False
            self.state = "aborted"
            self._stop_event.set()
            self._pause_event.clear()

        # Stop camera recording immediately if recording
        try:
            if self.camera.get_record_state():
                self.camera.stop_record()
        except Exception as e:
            logger.warning(f"Failed to stop record during abort: {e}")

        self._emit("tool2_batch_stopped", self.get_status())
        return {"success": True, "status": self.get_status()}

    def _batch_worker_loop(self):
        """Executes the loop of starting, recording, stopping, and resting."""
        logger.info(
            f"Batch recording started: {self.target_clips} clips x {self.clip_duration}s each."
        )

        try:
            while self.current_clip_index < self.target_clips and not self._stop_event.is_set():
                # Check for pause
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)

                if self._stop_event.is_set():
                    break

                self.current_clip_index += 1
                clip_num = self.current_clip_index

                # 1. Start Recording
                logger.info(f"Starting batch clip {clip_num}/{self.target_clips}...")
                self.state = "recording"
                self.current_clip_elapsed = 0.0

                try:
                    self.camera.start_record()
                except Exception as e:
                    logger.error(f"Failed to trigger start record on clip {clip_num}: {e}")
                    self.state = "error"
                    self.error_message = f"Camera start record failed on clip {clip_num}: {e}"
                    self.is_active = False
                    self._emit("tool2_batch_error", self.get_status())
                    return

                # Record loop for clip duration
                clip_start = time.time()
                while self.current_clip_elapsed < self.clip_duration and not self._stop_event.is_set():
                    time.sleep(0.25)
                    self.current_clip_elapsed = round(time.time() - clip_start, 1)
                    self._emit("tool2_batch_progress", self.get_status())

                # 2. Stop Recording
                logger.info(f"Stopping batch clip {clip_num}/{self.target_clips}...")
                try:
                    self.camera.stop_record()
                except Exception as e:
                    logger.warning(f"Error stopping record for clip {clip_num}: {e}")

                self.elapsed_total_seconds += self.current_clip_elapsed
                self.current_clip_elapsed = 0.0

                if self.current_clip_index >= self.target_clips or self._stop_event.is_set():
                    break

                # 3. Cooldown / File Finalize Delay
                self.state = "cooldown"
                self._emit("tool2_batch_progress", self.get_status())
                cooldown_start = time.time()
                while time.time() - cooldown_start < self.cooldown_seconds and not self._stop_event.is_set():
                    time.sleep(0.25)

            if self.current_clip_index >= self.target_clips and not self._stop_event.is_set():
                self.state = "completed"
                self.is_active = False
                logger.info("Batch recording completed successfully!")
                self._emit("tool2_batch_completed", self.get_status())

        except Exception as e:
            logger.error(f"Batch recorder exception: {e}")
            self.state = "error"
            self.error_message = str(e)
            self.is_active = False
            self._emit("tool2_batch_error", self.get_status())

    def get_status(self) -> Dict[str, Any]:
        """Returns the current state of the batch recorder."""
        total_target_seconds = self.target_clips * self.clip_duration
        percent = (
            round((self.current_clip_index / max(1, self.target_clips)) * 100, 1)
            if self.target_clips > 0
            else 0.0
        )
        return {
            "is_active": self.is_active,
            "state": self.state,
            "clip_duration": self.clip_duration,
            "target_clips": self.target_clips,
            "current_clip_index": self.current_clip_index,
            "current_clip_elapsed": round(self.current_clip_elapsed, 1),
            "elapsed_total_seconds": round(self.elapsed_total_seconds, 1),
            "total_target_seconds": total_target_seconds,
            "percent": percent,
            "selected_resolution": self.selected_resolution,
            "selected_codec": self.selected_codec,
            "selected_framerate": self.selected_framerate,
            "error_message": self.error_message,
        }
