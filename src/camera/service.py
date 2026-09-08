"""
Blackmagic Camera Background Service.
Headless IPC daemon designed to be spawned by Electron Main process with unbuffered I/O (-u).
Accepts JSON commands on stdin and streams JSON-ND telemetry events on stdout.
"""

from __future__ import annotations
import argparse
import json
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Any, Dict, Optional

from .camera_client import CameraClient
from .ftp_client import FtpClient
from .tool_auto_transfer import AutoTransferTool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("camera.service")


def send_event(event_type: str, data: Any):
    """Write line-delimited JSON event to stdout."""
    payload = {
        "type": event_type,
        "timestamp": time.time(),
        "data": data,
    }
    line = json.dumps(payload)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class CameraServiceDaemon:
    def __init__(
        self,
        camera_ip: str = "192.168.1.118",
        camera_ftp: str = "ftp://PYXIS-6K.local",
        dest_dir: str = "./watch_folders/00_IN_INGEST",
    ):
        self.camera_ip = camera_ip
        self.camera_ftp = camera_ftp
        self.dest_dir = Path(dest_dir)
        self.dest_dir.mkdir(parents=True, exist_ok=True)

        self.camera_client = CameraClient(host=self.camera_ip)
        self.ftp_client = FtpClient(host=self.camera_ftp)
        self.tool1 = AutoTransferTool(
            camera_client=self.camera_client,
            ftp_client=self.ftp_client,
            dest_dir=self.dest_dir,
            on_event_cb=self._on_tool_event,
        )
        self.running = True

    def _on_tool_event(self, event: Dict[str, Any]):
        send_event(event.get("type", "tool_event"), event.get("data", {}))

    def update_config(self, camera_ip: Optional[str] = None, camera_ftp: Optional[str] = None, dest_dir: Optional[str] = None):
        if camera_ip:
            self.camera_ip = camera_ip
            self.camera_client = CameraClient(host=self.camera_ip)
            self.tool1.camera = self.camera_client
        if camera_ftp:
            self.camera_ftp = camera_ftp
            self.ftp_client = FtpClient(host=self.camera_ftp)
            self.tool1.ftp = self.ftp_client
        if dest_dir:
            self.dest_dir = Path(dest_dir)
            self.dest_dir.mkdir(parents=True, exist_ok=True)
            self.tool1.dest_dir = self.dest_dir

        send_event("config_updated", {
            "camera_ip": self.camera_ip,
            "camera_ftp": self.camera_ftp,
            "dest_dir": str(self.dest_dir),
        })
        send_event("status", self.get_status())

    def get_status(self) -> Dict[str, Any]:
        connected = False
        product = {}
        recording = False
        try:
            product = self.camera_client.get_product()
            recording = self.camera_client.get_record_state()
            connected = True
        except Exception:
            connected = False

        return {
            "connected": connected,
            "camera_ip": self.camera_ip,
            "camera_ftp": self.camera_ftp,
            "product": product,
            "recording": recording,
            "tool1": self.tool1.get_status(),
        }

    def test_connection(self, test_ip: Optional[str] = None, test_ftp: Optional[str] = None) -> Dict[str, Any]:
        ip = test_ip or self.camera_ip
        ftp_host = test_ftp or self.camera_ftp
        c_client = CameraClient(host=ip, timeout=3.0)
        f_client = FtpClient(host=ftp_host, timeout=3.0)

        http_ok = False
        ftp_ok = False
        product_name = ""
        error_msg = ""

        try:
            prod = c_client.get_product()
            http_ok = True
            product_name = prod.get("productName", prod.get("deviceName", "Blackmagic Camera"))
        except Exception as e:
            error_msg = f"REST Error: {e}"

        try:
            ftp_ok = f_client.test_connection()
        except Exception as e:
            if not error_msg:
                error_msg = f"FTP Error: {e}"
            else:
                error_msg += f" | FTP Error: {e}"

        if http_ok and (ip != self.camera_ip or ftp_host != self.camera_ftp):
            self.camera_ip = ip
            self.camera_client = c_client
            self.tool1.camera = c_client
            self.camera_ftp = ftp_host
            self.ftp_client = f_client
            self.tool1.ftp = f_client
            send_event("status", self.get_status())

        return {
            "http_ok": http_ok,
            "ftp_ok": ftp_ok,
            "product_name": product_name,
            "error": error_msg,
            "camera_ip": ip,
            "camera_ftp": ftp_host,
        }

    def run(self):
        send_event("service_ready", self.get_status())

        # Heartbeat thread
        def heartbeat_loop():
            while self.running:
                time.sleep(5.0)
                try:
                    send_event("heartbeat", self.get_status())
                except Exception:
                    break

        hb_thread = threading.Thread(target=heartbeat_loop, daemon=True, name="Heartbeat")
        hb_thread.start()

        # Process stdin commands
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue

            try:
                cmd_data = json.loads(line)
                cmd = cmd_data.get("cmd")

                if cmd == "status":
                    send_event("status", self.get_status())

                elif cmd == "toggle":
                    active = cmd_data.get("active", not self.tool1.is_active)
                    import_today = cmd_data.get("import_today", False)
                    if active:
                        res = self.tool1.activate(auto_import_same_day=import_today)
                    else:
                        res = self.tool1.deactivate()
                    send_event("toggle_result", res)

                elif cmd == "import_today":
                    res = self.tool1.import_same_day_clips()
                    send_event("import_today_result", res)

                elif cmd == "test_connection":
                    res = self.test_connection(
                        test_ip=cmd_data.get("camera_ip"),
                        test_ftp=cmd_data.get("camera_ftp"),
                    )
                    send_event("test_connection_result", res)

                elif cmd == "update_config":
                    self.update_config(
                        camera_ip=cmd_data.get("camera_ip"),
                        camera_ftp=cmd_data.get("camera_ftp"),
                        dest_dir=cmd_data.get("dest_dir"),
                    )

                elif cmd == "quit":
                    self.running = False
                    if self.tool1.is_active:
                        self.tool1.deactivate()
                    send_event("service_stopped", {"status": "ok"})
                    break

                else:
                    send_event("error", {"message": f"Unknown command: {cmd}"})

            except json.JSONDecodeError:
                send_event("error", {"message": f"Invalid JSON on stdin: {line}"})
            except Exception as e:
                send_event("error", {"message": str(e)})


def main():
    parser = argparse.ArgumentParser(description="Blackmagic Camera Background Service")
    parser.add_argument("--camera-ip", type=str, default="192.168.1.118", help="Camera IP address")
    parser.add_argument("--camera-ftp", type=str, default="ftp://PYXIS-6K.local", help="Camera FTP host/URL")
    parser.add_argument("--dest-dir", type=str, default="./watch_folders/00_IN_INGEST", help="Destination ingest folder")
    parser.add_argument("--auto-start", action="store_true", help="Automatically activate Tool 1 immediately")
    args = parser.parse_args()

    daemon = CameraServiceDaemon(
        camera_ip=args.camera_ip,
        camera_ftp=args.camera_ftp,
        dest_dir=args.dest_dir,
    )

    if args.auto_start:
        daemon.tool1.activate()

    daemon.run()


if __name__ == "__main__":
    main()
