"""
Blackmagic Camera Tooling Package.
Provides REST client, FTP transfer client, Auto Transfer Tool (Tool 1),
and Batch Recording Generator (Tool 2) for PYXIS 6K and compatible cameras.
"""

from .camera_client import CameraClient
from .ftp_client import FtpClient
from .tool_auto_transfer import AutoTransferTool
from .tool_batch_recorder import BatchRecorderTool, DURATION_PRESETS

__all__ = [
    "CameraClient",
    "FtpClient",
    "AutoTransferTool",
    "BatchRecorderTool",
    "DURATION_PRESETS",
]
