"""
Hot Folder Watcher and File Stability Guard.
Monitors the ingest directory, ensures files are completely written before dispatching,
and manages atomic moves between storage states.
"""

import os
import sys
import time
import fcntl
import shutil
import threading
from pathlib import Path
from typing import Callable, Optional, Set, Dict

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

from src.common.config import AppConfig
from src.common.logger import setup_logger

logger = setup_logger("braw_watcher")


class FileStabilityGuard:
    """Verifies that incoming files are completely written and not in use."""

    @staticmethod
    def is_file_locked(file_path: Path) -> bool:
        """Tries to acquire a non-blocking exclusive POSIX lock on the file."""
        if not file_path.exists():
            return True
        try:
            with open(file_path, "rb") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return False
        except (IOError, OSError, PermissionError):
            return True

    @classmethod
    def wait_for_complete_write(
        cls,
        file_path: Path,
        required_checks: int = 3,
        delay_seconds: float = 2.0,
        max_wait_seconds: float = 7200.0,  # Max 2 hours for massive 100GB+ BRAW files
    ) -> bool:
        """
        Polls the file until its size remains constant for consecutive checks
        and no process holds an exclusive write lock.
        """
        logger.info(f"Monitoring file write completion: {file_path.name}")
        start_time = time.time()
        last_size = -1
        stable_count = 0

        while time.time() - start_time < max_wait_seconds:
            if not file_path.exists():
                logger.warning(f"File vanished during stability check: {file_path}")
                return False

            try:
                current_size = file_path.stat().st_size
            except OSError as e:
                logger.debug(f"stat failed on {file_path.name}: {e}")
                time.sleep(delay_seconds)
                continue

            # Check if file has data
            if current_size > 0 and current_size == last_size:
                # Also check file lock
                if not cls.is_file_locked(file_path):
                    stable_count += 1
                    logger.debug(
                        f"File size stable ({current_size:,} bytes) [{stable_count}/{required_checks}]: {file_path.name}"
                    )
                    if stable_count >= required_checks:
                        logger.info(
                            f"File stabilized ({current_size / (1024*1024):.2f} MB): {file_path.name}"
                        )
                        return True
                else:
                    logger.debug(f"File locked by external process: {file_path.name}")
                    stable_count = 0
            else:
                stable_count = 0
                last_size = current_size

            time.sleep(delay_seconds)

        logger.error(f"Timeout waiting for file stabilization: {file_path.name}")
        return False


class IngestHandler(FileSystemEventHandler):
    """Watchdog event handler for hot folder."""

    def __init__(self, watcher: "FolderWatcher"):
        super().__init__()
        self.watcher = watcher

    def on_created(self, event):
        if not event.is_directory:
            self.watcher.handle_candidate(Path(event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self.watcher.handle_candidate(Path(event.src_path))


class FolderWatcher:
    """Manages the folder observation, stability checking, and job dispatch."""

    def __init__(self, config: AppConfig, transcode_callback: Callable[[Path], bool]):
        self.config = config
        self.transcode_callback = transcode_callback
        self.pending_candidates: Set[Path] = set()
        self.in_flight: Set[Path] = set()
        self.lock = threading.Lock()
        self.running = False
        self.observer: Optional[Observer] = None
        self.worker_thread: Optional[threading.Thread] = None

    def handle_candidate(self, file_path: Path):
        """Filters extensions and queues candidate files for processing."""
        if file_path.suffix.lower() in self.config.watcher.extensions:
            with self.lock:
                if file_path not in self.in_flight and file_path not in self.pending_candidates:
                    self.pending_candidates.add(file_path)
                    logger.info(f"Discovered new candidate file: {file_path.name}")

    def _scan_existing_files(self):
        """Scans the ingest directory for any files already present on startup."""
        logger.info(f"Scanning ingest folder: {self.config.storage.ingest_dir}")
        for ext in self.config.watcher.extensions:
            for item in self.config.storage.ingest_dir.glob(f"*{ext}"):
                self.handle_candidate(item)
            for item in self.config.storage.ingest_dir.glob(f"*{ext.upper()}"):
                self.handle_candidate(item)

    def _worker_loop(self):
        """Background worker loop to inspect stability and trigger transcode."""
        while self.running:
            candidate = None
            with self.lock:
                if self.pending_candidates:
                    candidate = self.pending_candidates.pop()
                    self.in_flight.add(candidate)

            if candidate:
                self._process_candidate(candidate)
            else:
                time.sleep(self.config.watcher.poll_interval)
                # Re-scan periodically to catch non-notified filesystem events
                self._scan_existing_files()

    def _process_candidate(self, braw_path: Path):
        """Guards stability, moves to processing folder, and invokes transcode callback."""
        try:
            if not braw_path.exists():
                return

            stable = FileStabilityGuard.wait_for_complete_write(
                file_path=braw_path,
                required_checks=self.config.watcher.stability_checks,
                delay_seconds=self.config.watcher.stability_delay,
            )

            if not stable:
                logger.error(f"Stability check failed for: {braw_path.name}")
                return

            # Check for sidecar file
            sidecar_path = braw_path.with_suffix(".sidecar")
            has_sidecar = sidecar_path.exists()

            # Atomically move BRAW (and sidecar) to 01_PROCESSING
            processing_braw = self.config.storage.processing_dir / braw_path.name
            processing_sidecar = (
                self.config.storage.processing_dir / sidecar_path.name
                if has_sidecar
                else None
            )

            logger.info(f"Moving {braw_path.name} to {self.config.storage.processing_dir}")
            shutil.move(str(braw_path), str(processing_braw))
            if has_sidecar and processing_sidecar:
                shutil.move(str(sidecar_path), str(processing_sidecar))

            # Execute Transcode Callback
            success = self.transcode_callback(processing_braw)

            if success:
                # Move to Archive
                archive_braw = self.config.storage.archive_dir / braw_path.name
                logger.info(f"Archiving source file to {archive_braw}")
                shutil.move(str(processing_braw), str(archive_braw))
                if has_sidecar and processing_sidecar and processing_sidecar.exists():
                    shutil.move(
                        str(processing_sidecar),
                        str(self.config.storage.archive_dir / sidecar_path.name),
                    )
            else:
                # Move to Failed
                failed_braw = self.config.storage.failed_dir / braw_path.name
                logger.error(f"Transcode failed. Moving to: {failed_braw}")
                shutil.move(str(processing_braw), str(failed_braw))
                if has_sidecar and processing_sidecar and processing_sidecar.exists():
                    shutil.move(
                        str(processing_sidecar),
                        str(self.config.storage.failed_dir / sidecar_path.name),
                    )

        except Exception as e:
            logger.exception(f"Unexpected error handling candidate {braw_path.name}: {e}")
        finally:
            with self.lock:
                self.in_flight.discard(braw_path)

    def start(self):
        """Starts the watchdog observer and worker thread."""
        self.config.storage.ensure_directories()
        self.running = True

        self._scan_existing_files()

        event_handler = IngestHandler(self)
        self.observer = Observer()
        self.observer.schedule(
            event_handler,
            path=str(self.config.storage.ingest_dir),
            recursive=False,
        )
        self.observer.start()

        self.worker_thread = threading.Thread(
            target=self._worker_loop, name="WatcherWorkerThread", daemon=True
        )
        self.worker_thread.start()
        logger.info(f"Folder watcher active on: {self.config.storage.ingest_dir}")

    def stop(self):
        """Stops the watcher observer and waits for shutdown."""
        self.running = False
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.worker_thread:
            self.worker_thread.join(timeout=5.0)
        logger.info("Folder watcher stopped.")
