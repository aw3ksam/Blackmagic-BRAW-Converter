"""
Unit and Integration Tests for FFmpeg Standalone Engine and BRAW Decoder Bridge.
"""

import os
import tempfile
import unittest
from pathlib import Path

from src.common.config import TranscodeConfig, ColorConfig, AudioConfig
from src.ffmpeg_engine.decoder_bridge import DecoderBridge
from src.ffmpeg_engine.lut_manager import LutManager
from src.ffmpeg_engine.ffmpeg_pipeline import FFmpegPipeline


class TestFFmpegEngine(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)
        self.sample_braw = Path("Documents/Blackmagic RAW SDK/Media/sample.braw").resolve()
        self.real_sample_braw = Path("samples/A001_06201100_C073.braw").resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_lut_manager_discovery_and_resolution(self):
        """Verify that LutManager discovers bundled LUTs and resolves standard profiles."""
        lut_mgr = LutManager()
        available = lut_mgr.list_available_luts()
        self.assertGreater(len(available), 0, "Should find at least one bundled LUT profile.")

        # Test resolving standard Gen 5 LUT
        resolved = lut_mgr.resolve_lut("Blackmagic Gen 5 Film to Extended Video.cube")
        self.assertIsNotNone(resolved, "Should resolve Blackmagic Gen 5 Film to Extended Video LUT.")
        self.assertTrue(resolved.is_file(), f"Resolved LUT path should be a real file: {resolved}")

        # Test shorthand resolution
        shorthand = lut_mgr.resolve_lut("Gen 5 Film to Extended Video")
        self.assertIsNotNone(shorthand, "Should resolve shorthand LUT name.")

    def test_decoder_bridge_metadata_inspection(self):
        """Verify that DecoderBridge inspects BRAW clips and extracts metadata."""
        bridge = DecoderBridge()
        self.assertTrue(bridge.binary_path.is_file(), "braw_decode binary must exist and be executable.")

        if self.sample_braw.is_file():
            meta = bridge.get_clip_metadata(self.sample_braw)
            self.assertEqual(meta.width, 4608)
            self.assertEqual(meta.height, 2592)
            self.assertGreater(meta.frame_count, 0)
            self.assertGreater(meta.frame_rate, 0.0)

        if self.real_sample_braw.is_file():
            meta = bridge.get_clip_metadata(self.real_sample_braw)
            self.assertEqual(meta.width, 6048)
            self.assertEqual(meta.height, 4032)
            self.assertEqual(meta.frame_count, 540)
            self.assertAlmostEqual(meta.frame_rate, 29.97, places=2)
            self.assertTrue(meta.has_audio)

    def test_transcode_pipeline_execution(self):
        """Verify end-to-end transcode execution of sample clip."""
        if not self.sample_braw.is_file():
            self.skipTest("sample.braw not found.")

        config = TranscodeConfig(
            container="mp4",
            codec="H265",
            encoding_profile="Main10",
            bitrate_mbps=20,
            resolution="source",
            color=ColorConfig(
                mode="lut",
                lut_path="Blackmagic Gen 5 Film to Extended Video.cube"
            ),
            audio=AudioConfig(codec="aac", sample_rate=48000, bitrate_kbps=320)
        )

        pipeline = FFmpegPipeline(config=config)
        output_file = self.output_dir / "sample_transcode.mp4"

        success = pipeline.transcode_clip(
            braw_path=self.sample_braw,
            output_file=output_file,
        )

        self.assertTrue(success, "Transcode should succeed for sample.braw.")
        self.assertTrue(output_file.is_file(), f"Output MP4 must exist: {output_file}")
        self.assertGreater(output_file.stat().st_size, 1000, "Output file must have non-zero size.")


if __name__ == "__main__":
    unittest.main()
