import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from assistant.download_manager import DownloadManager, DownloadJob
from assistant.media_tools import JarvisMediaExtractor, DownloadVideoTool, ExtractAudioTool, CancelDownloadTool
from assistant.tools import ErrorCode


class TestPhase3DownloadManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.dm = DownloadManager(media_dir=self.temp_dir)
        self.dm.ytdlp_bin = None  # Force hermetic offline simulator mode for tests
        self.extractor = JarvisMediaExtractor(
            default_media_dir=self.temp_dir,
            download_manager=self.dm
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────
    # 1. DRM Protection
    # ─────────────────────────────────────────────────────────────

    def test_drm_detection_and_rejection(self):
        drm_urls = [
            "https://www.netflix.com/watch/80057281",
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            "https://www.primevideo.com/detail/0TIX4S8X8/",
            "https://www.disneyplus.com/video/xyz123",
            "https://www.hotstar.com/in/movies/sholay/123456"
        ]
        for url in drm_urls:
            self.assertTrue(self.dm.is_drm_protected(url), f"Failed to detect DRM on {url}")
            res = asyncio.run(self.dm.start_download(url=url))
            self.assertFalse(res.ok)
            self.assertEqual(res.error_code, ErrorCode.DRM_PROTECTED)
            self.assertIn("DRM-protected", res.message)

    def test_non_drm_urls_pass_drm_check(self):
        non_drm = [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://instagram.com/reel/C3zYx7/",
            "https://x.com/user/status/123456",
            "https://vimeo.com/12345678"
        ]
        for url in non_drm:
            self.assertFalse(self.dm.is_drm_protected(url))

    # ─────────────────────────────────────────────────────────────
    # 2. Safe External Cookie Path
    # ─────────────────────────────────────────────────────────────

    def test_safe_cookie_path_outside_repo(self):
        cookie_path = self.dm.get_safe_cookie_path()
        cwd = os.getcwd().lower()
        self.assertNotIn(cwd, cookie_path.lower())
        self.assertTrue(cookie_path.endswith(os.path.join(".kate", "auth", "cookies.txt")))

    # ─────────────────────────────────────────────────────────────
    # 3. Collision-Safe Numbered Filenames
    # ─────────────────────────────────────────────────────────────

    def test_collision_free_path_numbering(self):
        target_dir = os.path.join(self.temp_dir, "videos")
        # 1st file: does not exist yet
        path0 = self.dm.get_collision_free_path(target_dir, "sample_clip", "mp4")
        self.assertTrue(path0.endswith("sample_clip.mp4"))
        with open(path0, "w") as f:
            f.write("0")

        # 2nd file: base exists, should get (1)
        path1 = self.dm.get_collision_free_path(target_dir, "sample_clip", "mp4")
        self.assertTrue(path1.endswith("sample_clip (1).mp4"))
        with open(path1, "w") as f:
            f.write("1")

        # 3rd file: base and (1) exist, should get (2)
        path2 = self.dm.get_collision_free_path(target_dir, "sample_clip", "mp4")
        self.assertTrue(path2.endswith("sample_clip (2).mp4"))

    # ─────────────────────────────────────────────────────────────
    # 4. Dynamic Disk Space Check (2.0x safety factor & 2 GB fallback)
    # ─────────────────────────────────────────────────────────────

    def test_dynamic_disk_space_rejection_and_fallback(self):
        # 500 MB estimated -> requires 1000 MB (1.0 GB)
        with patch("shutil.disk_usage") as mock_usage:
            # Mock 800 MB free (less than 1000 MB required)
            mock_usage.return_value = MagicMock(free=800 * 1024 * 1024)
            ok, err, req = self.dm.check_disk_space(self.temp_dir, estimated_bytes=500 * 1024 * 1024)
            self.assertFalse(ok)
            self.assertEqual(req, 1000 * 1024 * 1024)
            self.assertIn("Insufficient disk space", err)

        # Unknown size -> requires 2 GB fallback
        with patch("shutil.disk_usage") as mock_usage:
            # Mock 1.5 GB free (less than 2 GB required)
            mock_usage.return_value = MagicMock(free=int(1.5 * 1024 * 1024 * 1024))
            ok, err, req = self.dm.check_disk_space(self.temp_dir, estimated_bytes=None)
            self.assertFalse(ok)
            self.assertEqual(req, 2 * 1024 * 1024 * 1024)
            self.assertIn("Insufficient disk space", err)

        # Sufficient disk space -> passes
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(free=10 * 1024 * 1024 * 1024)
            ok, err, req = self.dm.check_disk_space(self.temp_dir, estimated_bytes=500 * 1024 * 1024)
            self.assertTrue(ok)
            self.assertEqual(err, "")

    # ─────────────────────────────────────────────────────────────
    # 5. Process Tree Cancellation & Partial File Cleanup
    # ─────────────────────────────────────────────────────────────

    def test_cancel_download_and_cleanup_partials(self):
        job_id = "test_job_cancel"
        out_file = os.path.join(self.temp_dir, "videos", "cancelled_vid.mp4")
        part_file = os.path.join(self.temp_dir, "videos", "cancelled_vid.part")
        ytdl_file = os.path.join(self.temp_dir, "videos", "cancelled_vid.ytdl")

        with open(part_file, "w") as f:
            f.write("partial bytes")
        with open(ytdl_file, "w") as f:
            f.write("partial meta")

        job = DownloadJob(
            job_id=job_id,
            url="https://www.youtube.com/watch?v=cancel_me",
            download_type="video",
            status="downloading",
            output_path=out_file
        )
        self.dm._jobs[job_id] = job

        success = asyncio.run(self.dm.cancel_download(job_id))
        self.assertTrue(success)
        self.assertEqual(job.status, "cancelled")
        self.assertEqual(job.error_code, ErrorCode.CANCELLED)
        # Partials should be removed
        self.assertFalse(os.path.exists(part_file))
        self.assertFalse(os.path.exists(ytdl_file))

    # ─────────────────────────────────────────────────────────────
    # 6. Hermetic Offline Download & Audio Extraction via Media Tools
    # ─────────────────────────────────────────────────────────────

    def test_download_video_tool_hermetic(self):
        tool = DownloadVideoTool(self.extractor)
        res = asyncio.run(tool.execute(executor=MagicMock(), source="https://www.youtube.com/watch?v=testvid123"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "success")
        self.assertTrue(os.path.exists(res["data"]["file_path"]))
        self.assertIn("Download completed", res["message"])

    def test_extract_audio_tool_hermetic(self):
        tool = ExtractAudioTool(self.extractor)
        res = asyncio.run(tool.execute(executor=MagicMock(), source="https://www.youtube.com/watch?v=testaud123", format="mp3"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "success")
        self.assertTrue(os.path.exists(res["data"]["file_path"]))
        self.assertIn("mp3", res["data"]["format"])

    def test_cancel_download_tool_execution(self):
        cancel_tool = CancelDownloadTool(self.extractor)
        # No active downloads
        res = asyncio.run(cancel_tool.execute(executor=MagicMock()))
        self.assertTrue(res["ok"])
        self.assertIn("No active downloads", res["message"])


if __name__ == "__main__":
    unittest.main()
