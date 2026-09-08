"""
Tool 1: Auto Video Clip Transfer.
Monitors Blackmagic camera recording status via REST API.
When recording stops, automatically transfers new video files over FTP.
Ignores pre-existing clips unless 'Import Same-Day Clips' is triggered.
Deposits newly completed clips directly into the specified ingest directory.
"""

from __future__ import annotations
from datetime import datetime
import logging
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from .camera_client import CameraClient
from .ftp_client import FtpClient

logger = logging.getLogger("camera.tool1")


class AutoTransferTool:
    """Tool 1: Automated Camera Clip Transfer Engine."""

    def __init__(
        self,
        camera_client: CameraClient,
        ftp_client: FtpClient,
        dest_dir: str | Path = "./watch_folders/00_IN_INGEST",
        poll_interval: float = 0.5,
        on_event_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.camera = camera_client
        self.ftp = ftp_client
        self.dest_dir = Path(dest_dir)
        self.poll_interval = poll_interval
        self.on_event_cb = on_event_cb

        # State variables
        self.is_active = False
        self.activation_time: Optional[datetime] = None
        self.known_clip_ids: Set[int] = set()
        self.known_file_names: Set[str] = set()
        self.current_recording = False
        self.last_record_start_time: Optional[float] = None

        # Concurrency & Queues
        self._monitor_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cancel_transfer_event = threading.Event()
        self._transfer_queue: queue.Queue = queue.Queue()
        self._queued_clip_ids: Set[int] = set()
        self._lock = threading.Lock()

        # Telemetry & Stats
        self.active_transfer: Optional[Dict[str, Any]] = None
        self.transfer_history: List[Dict[str, Any]] = []
        self.total_transferred_bytes = 0
        self.total_transferred_files = 0

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
                logger.debug(f"Error in event callback: {e}")

    def activate(self, auto_import_same_day: bool = False) -> Dict[str, Any]:
        """Activate Tool 1. Takes baseline snapshot of existing clips on camera."""
        with self._lock:
            if self.is_active:
                return {"success": True, "message": "Tool 1 is already active"}

            self.is_active = True
            self.activation_time = datetime.now()
            self._stop_event.clear()
            self._cancel_transfer_event.clear()

            # 1. Snapshot baseline existing clips to ignore
            self.known_clip_ids.clear()
            self.known_file_names.clear()
            baseline_count = 0
            try:
                clips = self.camera.get_clips()
                for c in clips:
                    cid = c.get("clipUniqueId")
                    fp = c.get("filePath", "")
                    if cid is not None:
                        self.known_clip_ids.add(cid)
                    if fp:
                        self.known_file_names.add(Path(fp).name)
                baseline_count = len(self.known_clip_ids)
                logger.info(f"Tool 1 activated. Baseline snapshotted {baseline_count} existing clips.")
            except Exception as e:
                logger.warning(f"Could not snapshot initial clips: {e}")

            # Check initial recording state
            try:
                self.current_recording = self.camera.get_record_state()
            except Exception:
                self.current_recording = False

            # 2. Start monitoring and worker threads
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="Tool1-Monitor"
            )
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True, name="Tool1-Worker"
            )
            self._monitor_thread.start()
            self._worker_thread.start()

            self._emit("tool1_status", {
                "active": True,
                "activation_time": self.activation_time.isoformat(),
                "baseline_clips_ignored": baseline_count,
            })

            # Optional retroactive import
            if auto_import_same_day:
                self.import_same_day_clips()

            return {
                "success": True,
                "active": True,
                "activation_time": self.activation_time.isoformat(),
                "baseline_clips_ignored": baseline_count,
            }

    def deactivate(self) -> Dict[str, Any]:
        """Deactivate Tool 1 and stop background monitoring."""
        with self._lock:
            if not self.is_active:
                return {"success": True, "message": "Tool 1 is not active"}

            self.is_active = False
            self._stop_event.set()
            self._cancel_transfer_event.set()

        # Wake worker thread if waiting on queue
        self._transfer_queue.put(None)

        self._emit("tool1_status", {
            "active": False,
            "activation_time": None,
        })
        logger.info("Tool 1 deactivated.")
        return {"success": True, "active": False}

    def import_same_day_clips(self) -> Dict[str, Any]:
        """
        Scan camera clips for all recordings made today (based on date in filename or metadata)
        and queue any missing ones for download directly into dest_dir.
        """
        today_str = datetime.now().strftime("%m%d")  # e.g., '0902'
        today_iso = datetime.now().strftime("%Y-%m-%d")

        try:
            clips = self.camera.get_clips()
            workingset = self.camera.get_workingset()
            volume = workingset[0].get("volume", "A001") if workingset else "A001"

            queued_clips = []
            for c in clips:
                fp = c.get("filePath", "")
                fname = Path(fp).name
                cid = c.get("clipUniqueId")
                file_size = c.get("fileSize", 0)

                # Match date in Blackmagic filename pattern (e.g. A001_09021115_C001.braw)
                is_today = False
                match = re.search(r'_(\d{4})\d{4}_', fname)
                if match and match.group(1) == today_str:
                    is_today = True

                # Also check clip object date if present
                if not is_today and today_iso in c.get("date", ""):
                    is_today = True

                # Check if destination file already exists
                local_file = self.dest_dir / fname
                already_downloaded = local_file.exists() and local_file.stat().st_size >= file_size > 0

                if is_today and not already_downloaded:
                    if cid not in self._queued_clip_ids:
                        clip_item = {
                            "clip_id": cid,
                            "file_path": fp,
                            "file_name": fname,
                            "file_size": file_size,
                            "volume": volume,
                            "is_manual_import": True,
                        }
                        self._transfer_queue.put(clip_item)
                        self._queued_clip_ids.add(cid)
                        queued_clips.append(fname)

            self._emit("tool1_import_today", {
                "queued_count": len(queued_clips),
                "queued_files": queued_clips,
            })
            return {"success": True, "queued_count": len(queued_clips), "queued_files": queued_clips}
        except Exception as e:
            logger.error(f"Error importing today's clips: {e}")
            return {"success": False, "error": str(e)}

    def _monitor_loop(self):
        """Monitors camera recording state transitions."""
        logger.info("Tool 1 monitor loop started.")
        while not self._stop_event.is_set():
            try:
                is_recording = self.camera.get_record_state()

                if not self.current_recording and is_recording:
                    # Recording started
                    self.current_recording = True
                    self.last_record_start_time = time.time()
                    logger.info("Camera started recording.")
                    self._emit("camera_record_started", {"time": datetime.now().isoformat()})

                elif self.current_recording and not is_recording:
                    # Recording stopped -> Wait for camera to finalize file, then queue new clip
                    self.current_recording = False
                    logger.info("Camera stopped recording. Waiting for file finalization...")
                    self._emit("camera_record_stopped", {"time": datetime.now().isoformat()})

                    # Short delay to allow camera OS to close file handle and update /clips index
                    time.sleep(1.5)
                    self._detect_and_queue_new_clips()

            except Exception as e:
                logger.debug(f"Monitor loop error: {e}")

            self._stop_event.wait(self.poll_interval)

    def _detect_and_queue_new_clips(self):
        """Compare current camera clips with baseline known clips and queue new ones."""
        try:
            clips = self.camera.get_clips()
            workingset = self.camera.get_workingset()
            volume = workingset[0].get("volume", "A001") if workingset else "A001"

            new_found = 0
            for c in clips:
                cid = c.get("clipUniqueId")
                fp = c.get("filePath", "")
                fname = Path(fp).name
                file_size = c.get("fileSize", 0)

                # If this clip was NOT in the pre-activation baseline
                if cid not in self.known_clip_ids and fname not in self.known_file_names:
                    if cid not in self._queued_clip_ids:
                        clip_item = {
                            "clip_id": cid,
                            "file_path": fp,
                            "file_name": fname,
                            "file_size": file_size,
                            "volume": volume,
                            "video_format": c.get("videoFormat", {}),
                            "codec": c.get("codecFormat", {}).get("codec", ""),
                            "is_manual_import": False,
                        }
                        self._transfer_queue.put(clip_item)
                        self._queued_clip_ids.add(cid)
                        # Mark as known clip
                        self.known_clip_ids.add(cid)
                        self.known_file_names.add(fname)
                        new_found += 1
                        logger.info(f"New clip detected and queued for transfer: {fname} ({file_size} bytes)")

            if new_found > 0:
                self._emit("tool1_new_clips_detected", {"new_clips_count": new_found})
        except Exception as e:
            logger.error(f"Error detecting new clips: {e}")

    def _worker_loop(self):
        """Worker thread processing the transfer queue sequentially."""
        logger.info("Tool 1 transfer worker loop started.")
        while not self._stop_event.is_set():
            try:
                item = self._transfer_queue.get(timeout=1.0)
                if item is None:
                    break  # Shutdown signal

                fname = item["file_name"]
                fp = item["file_path"]
                fsize = item["file_size"]
                volume = item.get("volume", "A001")

                logger.info(f"Starting transfer for {fname}...")
                self._cancel_transfer_event.clear()

                remote_path = self.ftp.resolve_remote_path(fp, volume=volume)

                # Progress reporting handler
                def on_progress(p_data: Dict[str, Any]):
                    self.active_transfer = {
                        "file_name": fname,
                        "file_size": fsize,
                        **p_data,
                    }
                    self._emit("tool1_transfer_progress", self.active_transfer)

                start_t = time.time()
                try:
                    dest_file = self.ftp.download_file(
                        remote_path=remote_path,
                        dest_dir=self.dest_dir,
                        expected_size=fsize,
                        progress_cb=on_progress,
                        cancel_event=self._cancel_transfer_event,
                    )
                    elapsed = round(time.time() - start_t, 2)
                    avg_speed_mb = round((fsize / (1024 * 1024)) / max(0.1, elapsed), 2)

                    record = {
                        "file_name": fname,
                        "file_size": fsize,
                        "dest_path": str(dest_file),
                        "status": "completed",
                        "duration_seconds": elapsed,
                        "speed_mbps": avg_speed_mb,
                        "completed_at": datetime.now().isoformat(),
                    }
                    self.transfer_history.insert(0, record)
                    self.total_transferred_files += 1
                    self.total_transferred_bytes += fsize
                    self.active_transfer = None

                    logger.info(f"Successfully transferred {fname} in {elapsed}s ({avg_speed_mb} MB/s)")
                    self._emit("tool1_transfer_completed", record)

                except Exception as e:
                    logger.error(f"Transfer failed for {fname}: {e}")
                    record = {
                        "file_name": fname,
                        "file_size": fsize,
                        "status": "failed",
                        "error": str(e),
                        "failed_at": datetime.now().isoformat(),
                    }
                    self.transfer_history.insert(0, record)
                    self.active_transfer = None
                    self._emit("tool1_transfer_failed", record)

                finally:
                    self._transfer_queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Worker exception: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Returns current operational status, active transfer, queue, and history."""
        return {
            "active": self.is_active,
            "activation_time": self.activation_time.isoformat() if self.activation_time else None,
            "current_recording": self.current_recording,
            "dest_dir": str(self.dest_dir.resolve()) if self.dest_dir.exists() else str(self.dest_dir),
            "queue_size": self._transfer_queue.qsize(),
            "active_transfer": self.active_transfer,
            "history": self.transfer_history[:20],
            "total_files": self.total_transferred_files,
            "total_bytes": self.total_transferred_bytes,
        }
