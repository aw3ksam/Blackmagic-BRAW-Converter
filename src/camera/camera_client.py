"""
Blackmagic Camera REST API Client.
Supports PYXIS 6K, Cinema Camera 6K, Pocket Cinema Camera, and URSA cine/broadcast models.
Zero third-party dependencies (uses Python standard library urllib).
"""

from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("camera.client")


class CameraClient:
    """Client for Blackmagic Camera REST API."""

    def __init__(
        self,
        host: str = "192.168.1.118",
        port: int = 80,
        protocol: str = "http",
        timeout: float = 4.0,
    ):
        # Normalize host in case http:// or https:// was passed
        if host.startswith("http://"):
            protocol = "http"
            host = host.replace("http://", "").split("/")[0]
        elif host.startswith("https://"):
            protocol = "https"
            host = host.replace("https://", "").split("/")[0]

        if ":" in host:
            parts = host.split(":")
            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                pass

        self.host = host
        self.port = port
        self.protocol = protocol
        self.timeout = timeout
        self.base_url = (
            f"{protocol}://{host}:{port}/control/api/v1"
            if port != 80
            else f"{protocol}://{host}/control/api/v1"
        )

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None) -> Any:
        """Helper to send HTTP request to the camera REST API."""
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        req_data = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                if status == 204:
                    return {"success": True, "status": 204}
                raw = resp.read()
                if not raw:
                    return {"success": True, "status": status}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            logger.warning(f"HTTP error {e.code} for {method} {url}: {err_body}")
            raise RuntimeError(f"Camera API error ({e.code}): {err_body or e.reason}") from e
        except urllib.error.URLError as e:
            logger.debug(f"Network error connecting to {url}: {e.reason}")
            raise ConnectionError(f"Could not connect to camera at {self.host}: {e.reason}") from e

    # System Information
    def get_system(self) -> Dict[str, Any]:
        """GET /system"""
        return self._request("GET", "/system")

    def get_product(self) -> Dict[str, Any]:
        """GET /system/product - Returns deviceName, productName, softwareVersion"""
        return self._request("GET", "/system/product")

    def get_supported_formats(self) -> List[Dict[str, Any]]:
        """GET /system/supportedFormats - Returns all supported resolutions, frame rates, codecs."""
        res = self._request("GET", "/system/supportedFormats")
        return res.get("supportedFormats", [])

    def get_format(self) -> Dict[str, Any]:
        """GET /system/format - Returns current format, codec, frame rate, record resolution."""
        return self._request("GET", "/system/format")

    def set_format(
        self,
        codec: str,
        record_resolution: Dict[str, int],
        frame_rate: Optional[str] = None,
        sensor_resolution: Optional[Dict[str, int]] = None,
        off_speed_enabled: bool = False,
        off_speed_frame_rate: Optional[int] = None,
    ) -> bool:
        """PUT /system/format - Sets the recording format, codec, and resolution."""
        payload: Dict[str, Any] = {
            "codec": codec,
            "recordResolution": record_resolution,
            "offSpeedEnabled": off_speed_enabled,
        }
        if frame_rate is not None:
            payload["frameRate"] = str(frame_rate)
        if sensor_resolution is not None:
            payload["sensorResolution"] = sensor_resolution
        else:
            payload["sensorResolution"] = record_resolution
        if off_speed_frame_rate is not None:
            payload["offSpeedFrameRate"] = off_speed_frame_rate

        self._request("PUT", "/system/format", payload)
        return True

    # Transport / Recording
    def get_record_state(self) -> bool:
        """GET /transports/0/record - Returns True if camera is currently recording."""
        res = self._request("GET", "/transports/0/record")
        return bool(res.get("recording", False))

    def start_record(self, clip_name: Optional[str] = None) -> bool:
        """POST /transports/0/record - Starts recording."""
        payload: Dict[str, Any] = {}
        if clip_name:
            payload["clipName"] = clip_name
        self._request("POST", "/transports/0/record", payload if payload else None)
        return True

    def stop_record(self) -> bool:
        """PUT /transports/0/record with recording: false - Stops recording."""
        self._request("PUT", "/transports/0/record", {"recording": False})
        return True

    def get_timecode(self) -> Dict[str, str]:
        """GET /transports/0/timecode - Returns display and timeline timecode."""
        return self._request("GET", "/transports/0/timecode")

    # Media & Clips
    def get_clips(self) -> List[Dict[str, Any]]:
        """GET /clips - Returns all clips recorded on the active media disk."""
        res = self._request("GET", "/clips")
        return res.get("clips", [])

    def get_active_media(self) -> Optional[Dict[str, Any]]:
        """GET /media/active - Returns active media device name and workingset index."""
        try:
            return self._request("GET", "/media/active")
        except Exception:
            return None

    def get_workingset(self) -> List[Dict[str, Any]]:
        """GET /media/workingset - Returns media devices, remaining time, free space."""
        res = self._request("GET", "/media/workingset")
        workingset = res.get("workingset", [])
        return [d for d in workingset if d is not None]
