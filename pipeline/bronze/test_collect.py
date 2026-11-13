import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.bronze import collect


class PublishedRunTest(unittest.TestCase):
    def test_retry_emits_original_snapshot_id(self):
        skipped = {"run_id": 20, "status": "skipped", "row_count": 0}
        original = {"run_id": 18, "status": "succeeded", "row_count": 2}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), patch.object(
                collect, "supabase_rpc", return_value=original
            ) as rpc:
                collect.emit_published_run("db-url", "key", skipped)
                rpc.assert_called_once_with("db-url", "key", "published_bronze_run", {"p_run_id": 20})
            self.assertEqual(output.read_text(), "bronze_run_id=18\n")

    def test_success_emits_exact_snapshot_id_without_a_second_request(self):
        published = {"run_id": 20, "status": "succeeded", "row_count": 2}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), patch.object(
                collect, "supabase_rpc"
            ) as rpc:
                collect.emit_published_run("db-url", "key", published)
                rpc.assert_not_called()
            self.assertEqual(output.read_text(), "bronze_run_id=20\n")

    def test_does_not_emit_an_unpublished_id(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                with self.assertRaisesRegex(collect.CollectionError, "missing_published_snapshot"):
                    collect.emit_published_run("db-url", "key", {"run_id": 20, "status": "running", "row_count": 0})
            self.assertFalse(output.exists())


class SourceImageTest(unittest.TestCase):
    def test_preserves_source_label_when_details_have_no_images(self):
        photos = [{"about": "Menu photo", "link": "https://images.example/menu.jpg"}]
        search = [{"place_id": "place", "images": photos}]
        with patch.object(collect, "omkar_results", return_value=[{"place_id": "place", "images": []}]) as provider:
            rows = list(collect.collect_details("key", search))
        provider.assert_called_once()
        self.assertEqual(rows[0]["images"], photos)

    def test_keeps_original_label_alongside_unlabeled_detail_images(self):
        unlabeled = {"link": "https://images.example/menu.jpg", "width": 1200}
        labeled = {"link": unlabeled["link"], "about": "Thực đơn"}
        self.assertEqual(collect.keep_search_image_labels([unlabeled], [labeled, labeled]), [unlabeled, labeled])
        self.assertEqual(collect.keep_search_image_labels([labeled], [labeled]), [labeled])

    def test_does_not_invent_gallery_labels(self):
        photos = [{"link": "https://images.example/gallery.jpg"}]
        search = [{"place_id": "place"}]
        details = [{"place_id": "place", "image_count": 4, "images": []}]
        with patch.object(collect, "omkar_results", side_effect=[details, photos]), patch.object(collect.time, "sleep"):
            rows = list(collect.collect_details("key", search))
        self.assertEqual(rows[0]["images"], photos)


if __name__ == "__main__":
    unittest.main()
