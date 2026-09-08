"""
Unit and Integration Tests for Configuration and Hot Folder Watcher.
"""

import time
import tempfile
import unittest
from pathlib import Path

from src.common.config import load_config, AppConfig, StorageConfig, WatcherConfig, TranscodeConfig
from src.common.watcher import FileStabilityGuard, FolderWatcher


class TestConfigAndWatcher(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

        self.storage = StorageConfig(
            ingest_dir=self.base_path / "00_IN_INGEST",
            processing_dir=self.base_path / "01_PROCESSING",
            completed_dir=self.base_path / "02_COMPLETED_MP4",
            archive_dir=self.base_path / "03_ARCHIVE_BRAW",
            failed_dir=self.base_path / "99_FAILED",
        )
        self.storage.ensure_directories()

        self.config = AppConfig(
            storage=self.storage,
            watcher=WatcherConfig(
                poll_interval=0.2,
                stability_checks=2,
                stability_delay=0.3,
                extensions=[".braw"],
                include_sidecars=True,
            ),
            transcode=TranscodeConfig(),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_storage_directories_creation(self):
        """Verify that all required storage directories are created."""
        for path in [
            self.storage.ingest_dir,
            self.storage.processing_dir,
            self.storage.completed_dir,
            self.storage.archive_dir,
            self.storage.failed_dir,
        ]:
            self.assertTrue(path.exists(), f"Directory should exist: {path}")

    def test_file_stability_guard(self):
        """Verify that stability guard detects when a file is fully written."""
        test_file = self.storage.ingest_dir / "test_clip.braw"
        with open(test_file, "wb") as f:
            f.write(b"SAMPLE BRAW HEADER AND BYTES" * 100)

        is_stable = FileStabilityGuard.wait_for_complete_write(
            file_path=test_file,
            required_checks=2,
            delay_seconds=0.2,
            max_wait_seconds=5.0,
        )
        self.assertTrue(is_stable, "File should be declared stable after consecutive checks.")

    def test_folder_watcher_lifecycle_and_move(self):
        """Simulate ingesting a BRAW file with sidecar through the watcher state machine."""
        processed_files = []

        def mock_transcode(path: Path) -> bool:
            processed_files.append(path)
            # Create a mock output MP4
            output_mp4 = self.storage.completed_dir / f"{path.stem}.mp4"
            output_mp4.write_text("MOCK MP4 CONTENT")
            return True

        watcher = FolderWatcher(config=self.config, transcode_callback=mock_transcode)
        watcher.start()

        try:
            # Create mock BRAW file and sidecar in Ingest folder
            test_braw = self.storage.ingest_dir / "A001_C001_082710_001.braw"
            test_sidecar = self.storage.ingest_dir / "A001_C001_082710_001.sidecar"

            with open(test_braw, "wb") as f:
                f.write(b"RAW DATA" * 500)
            with open(test_sidecar, "w") as f:
                f.write("Sidecar metadata")

            # Allow watcher loop to pick up and process
            time.sleep(1.8)

            # Check that transcode callback was executed
            self.assertEqual(len(processed_files), 1, "Transcode callback should have been invoked once")

            # Check that output MP4 exists
            expected_mp4 = self.storage.completed_dir / "A001_C001_082710_001.mp4"
            self.assertTrue(expected_mp4.exists(), f"Output MP4 should exist: {expected_mp4}")

            # Check that original BRAW and sidecar are archived in 03_ARCHIVE_BRAW
            archived_braw = self.storage.archive_dir / "A001_C001_082710_001.braw"
            archived_sidecar = self.storage.archive_dir / "A001_C001_082710_001.sidecar"
            self.assertTrue(archived_braw.exists(), f"BRAW should be archived in: {archived_braw}")
            self.assertTrue(archived_sidecar.exists(), f"Sidecar should be archived in: {archived_sidecar}")

            # Check that ingest and processing folders are empty
            self.assertFalse((self.storage.ingest_dir / test_braw.name).exists())
            self.assertFalse((self.storage.processing_dir / test_braw.name).exists())

        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
