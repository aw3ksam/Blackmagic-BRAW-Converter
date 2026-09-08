"""
Unit tests for Blackmagic Camera Ingest (Tool 1) in core test suite.
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from pathlib import Path
import tempfile
import threading
import unittest

from src.camera.camera_client import CameraClient
from src.camera.ftp_client import FtpClient
from src.camera.tool_auto_transfer import AutoTransferTool


class MockCameraHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/control/api/v1/system/product":
            self._send_json({"productName": "Blackmagic PYXIS 6K", "softwareVersion": "8.6"})
        elif self.path == "/control/api/v1/transports/0/record":
            self._send_json({"recording": False})
        elif self.path == "/control/api/v1/clips":
            self._send_json({
                "clips": [
                    {"clipUniqueId": 501, "filePath": "A001_09021400_C001.braw", "fileSize": 10240},
                ]
            })
        elif self.path == "/control/api/v1/media/workingset":
            self._send_json({
                "workingset": [{"volume": "A001", "bytesRemaining": 200000000000}]
            })
        else:
            self._send_json({}, status=404)


class TestCameraIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockCameraHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.client = CameraClient(host="127.0.0.1", port=self.port, timeout=2.0)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ingest_dir = Path(self.temp_dir.name) / "00_IN_INGEST"
        self.ingest_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_camera_client_queries(self):
        prod = self.client.get_product()
        self.assertEqual(prod.get("productName"), "Blackmagic PYXIS 6K")
        self.assertFalse(self.client.get_record_state())

    def test_tool1_activation_and_snapshot(self):
        ftp = FtpClient(host="127.0.0.1")
        tool1 = AutoTransferTool(
            camera_client=self.client,
            ftp_client=ftp,
            dest_dir=self.ingest_dir,
        )

        res = tool1.activate()
        self.assertTrue(res["success"])
        self.assertTrue(tool1.is_active)
        self.assertIn(501, tool1.known_clip_ids)

        status = tool1.get_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["queue_size"], 0)

        tool1.deactivate()
        self.assertFalse(tool1.is_active)


if __name__ == "__main__":
    unittest.main()
