"""
Blackmagic Camera FTP Transfer Client.
Handles file downloading, progress tracking, path resolution, and same-day clip detection.
Supports hostnames (e.g. ftp://PYXIS-6K.local) and direct IPs (e.g. 192.168.1.118).
"""

from __future__ import annotations
from datetime import datetime
import ftplib
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable, Dict, List, Optional
import urllib.parse

logger = logging.getLogger("camera.ftp")


class ProgressCallback:
    """Computes transfer speed, ETA, and percentage updates."""

    def __init__(
        self,
        file_name: str,
        total_bytes: int,
        callback: Optional[Callable[[Dict[str, any]], None]] = None,
        update_interval: float = 0.25,
    ):
        self.file_name = file_name
        self.total_bytes = total_bytes
        self.callback = callback
        self.update_interval = update_interval
        self.transferred_bytes = 0
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.last_transferred_bytes = 0
        self.cancelled = False

    def __call__(self, chunk: bytes):
        if self.cancelled:
            raise InterruptedError("Transfer cancelled by user")
        chunk_len = len(chunk)
        self.transferred_bytes += chunk_len

        now = time.time()
        elapsed_since_update = now - self.last_update_time
        if elapsed_since_update >= self.update_interval or self.transferred_bytes >= self.total_bytes:
            total_elapsed = max(0.001, now - self.start_time)
            avg_speed = self.transferred_bytes / total_elapsed
            instant_speed = (
                (self.transferred_bytes - self.last_transferred_bytes) / elapsed_since_update
                if elapsed_since_update > 0
                else avg_speed
            )
            percent = (
                round((self.transferred_bytes / self.total_bytes) * 100, 1)
                if self.total_bytes > 0
                else 0.0
            )
            eta = (
                (self.total_bytes - self.transferred_bytes) / avg_speed
                if avg_speed > 0 and self.total_bytes > self.transferred_bytes
                else 0.0
            )

            if self.callback:
                self.callback({
                    "file_name": self.file_name,
                    "transferred_bytes": self.transferred_bytes,
                    "total_bytes": self.total_bytes,
                    "percent": percent,
                    "speed_bps": instant_speed,
                    "speed_mbps": round(instant_speed / (1024 * 1024), 2),
                    "eta_seconds": round(eta, 1),
                    "status": "transferring" if self.transferred_bytes < self.total_bytes else "completed",
                })
            self.last_update_time = now
            self.last_transferred_bytes = self.transferred_bytes


class FtpClient:
    """Client for transferring recorded video clips from Blackmagic Camera via FTP."""

    def __init__(self, host: str = "ftp://PYXIS-6K.local", port: int = 21, timeout: float = 10.0):
        # Normalize host string
        raw_host = host.strip()
        if raw_host.startswith("ftp://"):
            clean = raw_host[6:].split("/")[0]
            if ":" in clean:
                parts = clean.split(":")
                self.host = parts[0]
                try:
                    port = int(parts[1])
                except ValueError:
                    pass
            else:
                self.host = clean
        else:
            if ":" in raw_host:
                parts = raw_host.split(":")
                self.host = parts[0]
                try:
                    port = int(parts[1])
                except ValueError:
                    pass
            else:
                self.host = raw_host

        self.port = port
        self.timeout = timeout
        self._lock = threading.Lock()

    def _connect(self) -> ftplib.FTP:
        """Establish connection to camera FTP server."""
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login()  # Anonymous login on Blackmagic cameras
        return ftp

    def test_connection(self) -> bool:
        """Test if the FTP server is reachable and accepting connections."""
        try:
            with self._lock:
                ftp = self._connect()
                ftp.quit()
            return True
        except Exception as e:
            logger.debug(f"FTP connection test failed for {self.host}:{self.port}: {e}")
            return False

    def list_files_in_volume(self, volume: Optional[str] = None) -> List[Dict[str, any]]:
        """List all video files on the camera storage volume."""
        with self._lock:
            ftp = self._connect()
            try:
                root_dirs: List[str] = []
                ftp.retrlines("NLST", root_dirs.append)

                target_dir = ""
                if "usb" in root_dirs:
                    if volume:
                        target_dir = f"usb/{volume}"
                    else:
                        usb_dirs: List[str] = []
                        ftp.cwd("usb")
                        ftp.retrlines("NLST", usb_dirs.append)
                        ftp.cwd("/")
                        if usb_dirs:
                            target_dir = f"usb/{usb_dirs[0]}"
                        else:
                            target_dir = "usb"
                elif volume:
                    target_dir = volume

                if target_dir:
                    ftp.cwd(target_dir)

                files_info: List[Dict[str, any]] = []
                lines: List[str] = []
                ftp.retrlines("LIST", lines.append)

                for line in lines:
                    parts = line.split(maxsplit=8)
                    if len(parts) >= 9:
                        name = parts[8]
                        if not name.startswith(".") and name not in ("Proxy", "Stills"):
                            try:
                                size = int(parts[4])
                            except ValueError:
                                size = 0
                            is_dir = parts[0].startswith("d")
                            if not is_dir and (name.endswith(".braw") or name.endswith(".mov") or name.endswith(".mp4")):
                                files_info.append({
                                    "name": name,
                                    "size": size,
                                    "remote_path": f"{target_dir}/{name}" if target_dir else name,
                                    "raw_line": line,
                                })
                return files_info
            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass

    def resolve_remote_path(self, file_path: str, volume: Optional[str] = None) -> str:
        """
        Convert a relative clip filePath (e.g. 'A001_08071131_C266.braw')
        into full FTP path (e.g. 'usb/A001/A001_08071131_C266.braw').
        """
        clean_path = file_path.lstrip("/")
        if clean_path.startswith("usb/"):
            return clean_path
        if volume:
            return f"usb/{volume}/{clean_path}"
        return f"usb/{clean_path}"

    def download_file(
        self,
        remote_path: str,
        dest_dir: str | Path,
        expected_size: Optional[int] = None,
        progress_cb: Optional[Callable[[Dict[str, any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Path:
        """
        Download a file from camera FTP to local destination directory.
        Writes to a temporary .downloading file and performs atomic rename upon completion.
        """
        dest_path = Path(dest_dir) / Path(remote_path).name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dest = dest_path.with_suffix(dest_path.suffix + ".downloading")

        with self._lock:
            ftp = self._connect()
            try:
                total_size = expected_size or 0
                if total_size <= 0:
                    try:
                        total_size = ftp.size(remote_path) or 0
                    except Exception:
                        total_size = 0

                progress = ProgressCallback(
                    file_name=dest_path.name,
                    total_bytes=total_size,
                    callback=progress_cb,
                )

                with open(temp_dest, "wb") as f:
                    def write_chunk(chunk: bytes):
                        if cancel_event and cancel_event.is_set():
                            progress.cancelled = True
                            raise InterruptedError("Download cancelled")
                        f.write(chunk)
                        progress(chunk)

                    ftp.retrbinary(f"RETR {remote_path}", write_chunk, blocksize=1024 * 1024)

                # Rename temp file to final destination
                if temp_dest.exists():
                    if dest_path.exists():
                        dest_path.unlink()
                    temp_dest.rename(dest_path)

                final_size = dest_path.stat().st_size
                if expected_size and expected_size > 0 and final_size != expected_size:
                    logger.warning(
                        f"Size mismatch for {dest_path.name}: expected {expected_size}, got {final_size}"
                    )

                if progress_cb:
                    progress_cb({
                        "file_name": dest_path.name,
                        "transferred_bytes": final_size,
                        "total_bytes": final_size,
                        "percent": 100.0,
                        "speed_bps": 0,
                        "speed_mbps": 0,
                        "eta_seconds": 0,
                        "status": "completed",
                        "dest_path": str(dest_path),
                    })

                return dest_path
            except Exception as e:
                if temp_dest.exists():
                    try:
                        temp_dest.unlink()
                    except Exception:
                        pass
                if progress_cb:
                    progress_cb({
                        "file_name": dest_path.name,
                        "transferred_bytes": 0,
                        "total_bytes": expected_size or 0,
                        "percent": 0.0,
                        "status": "failed",
                        "error": str(e),
                    })
                raise
            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass
