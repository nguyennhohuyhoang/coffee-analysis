import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch

from pipeline.silver import ocr


def box(text, y=0):
    return {"text": text, "conf": 0.98, "x0": 1, "x1": 180, "y0": y, "y1": y + 15}


class MenuImageTests(unittest.TestCase):
    def test_only_menu_labeled_google_map_images(self):
        images = [
            {"about": "Menu photo", "link": "https://lh3.googleusercontent.com/a=w1920-h1080"},
            {"about": "THỰC ĐƠN đồ uống", "link": "https://lh5.googleusercontent.com/b",
             "published_at_date": "2026-09-02"},
            {"about": "Bảng giá", "link": "https://lh4.googleusercontent.com/c"},
            {"about": "Menu", "link": "https://lh3.googleusercontent.com/a=w1920-h1080"},
            {"about": "Food & drink", "link": "https://lh3.googleusercontent.com/d"},
            {"about": "Photo", "link": "https://lh3.googleusercontent.com/menu.jpg"},
            {"link": "https://lh3.googleusercontent.com/e"},
            {"about": "Menu", "link": "https://lh3.googleusercontent.com.evil.example/f"},
            {"about": "Menu", "link": "https://lh3.googleusercontent.com@evil.example/f"},
            {"about": "Submenuet", "link": "https://lh3.googleusercontent.com/g"},
        ]
        expected = [
            {"url": item["link"], "label": item["about"], "photo_date": item.get("published_at_date")}
            for item in images[:3]
        ]
        self.assertEqual(ocr.menu_images(images), expected)
        self.assertEqual(ocr.menu_images(json.dumps(images)), expected)

    def test_missing_label_or_input_is_ignored(self):
        for data in (None, "invalid JSON", {}, [None, "Menu", {"about": None}]):
            with self.subTest(data=data):
                self.assertEqual(ocr.menu_images(data), [])

    def test_fetch_hashes_payload_and_tries_high_resolution_first(self):
        headers = Message()
        headers["Content-Type"] = "image/jpeg"
        response = Mock(headers=headers)
        response.read.return_value = b"sample photo bytes"
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(ocr._OPENER, "open", return_value=context) as open_image:
            digest, data = ocr.fetch_image("https://lh3.googleusercontent.com/a=w1920-h1080-k-no")
        self.assertEqual(open_image.call_args.args[0].full_url, "https://lh3.googleusercontent.com/a=s2000")
        self.assertEqual(data, b"sample photo bytes")
        self.assertEqual(digest, ocr.hashlib.sha256(data).hexdigest())


class MenuParserTests(unittest.TestCase):
    def test_sparsely_printed_menu_and_ungrouped_vnd_price(self):
        status, items = ocr.parse_observations([box("Cà phê sữa 35000đ")])
        self.assertEqual(status, "ok")
        self.assertEqual(items[0]["canonical_id"], "ca_phe_sua")
        self.assertEqual(
            items[0]["price_observations"],
            [{"amount": 35000.0, "currency": "VND", "raw_text": "35000đ",
              "inferred_k": False, "plus_plus": False}],
        )

    def test_currencies_and_flags_belong_to_each_variant(self):
        status, items = ocr.parse_observations([box("Latte $3 / 70K++")])
        self.assertEqual(status, "not_menu")
        self.assertTrue(items[0]["needs_review"])
        self.assertEqual(
            items[0]["price_observations"],
            [
                {"amount": 3.0, "currency": "USD", "raw_text": "$3",
                 "inferred_k": False, "plus_plus": False},
                {"amount": 70000.0, "currency": "VND", "raw_text": "70K++",
                 "inferred_k": False, "plus_plus": True},
            ],
        )

    def test_exact_db_alias_and_add_on(self):
        boxes = [box("Cà phê kem 50K"), box("Extra espresso shot 10K", 35)]
        status, items = ocr.parse_observations(boxes, aliases={"ca phe kem": "ca_phe_sua"})
        self.assertEqual(status, "ok")
        self.assertEqual(items[0]["canonical_id"], "ca_phe_sua")
        self.assertEqual(items[0]["match_score"], 100)
        self.assertIn("add_on", items[1]["review_reasons"])


if __name__ == "__main__":
    unittest.main()
