import unittest

from pipeline.silver.about import FEATURE_RULES, transform


def group(group_id, *options):
    return {
        "id": group_id,
        "options": [{"name": name, "enabled": enabled} for name, enabled in options],
    }


def features(about):
    return {record["feature_key"]: record for record in transform(about)}


class AboutTests(unittest.TestCase):
    def test_positive_uses_group_and_keeps_literal_evidence(self):
        found = features([
            group("payments", ("Wi-Fi", True)),
            group("amenities", (" Wi‑Fi ", True), ("Wi-Fi", True)),
            group("atmosphere", ("Quiet", False)),
        ])
        self.assertEqual(len(found), 16)
        self.assertEqual(set(found), set(FEATURE_RULES))
        self.assertEqual(found["has_wifi"], {
            "feature_key": "has_wifi",
            "value": True,
            "reason": "explicit_true",
            "evidence": [
                {"group_id": "amenities", "option": " Wi‑Fi ", "enabled": True,
                 "group_index": 1, "option_index": 0},
                {"group_id": "amenities", "option": "Wi-Fi", "enabled": True,
                 "group_index": 1, "option_index": 1},
            ],
        })
        self.assertIs(found["quiet"]["value"], False)
        self.assertEqual(found["quiet"]["reason"], "explicit_false")
        self.assertEqual(found["cash_only"]["reason"], "not_listed")
        self.assertIsNone(found["cash_only"]["value"])

    def test_conflicting_flags_and_partial_negatives_remain_unknown(self):
        found = features([
            group("amenities", ("Wi-Fi", False), ("Free Wi-Fi", True)),
            group("payments", ("Credit cards", False)),
            group("pets", ("Dogs allowed inside", False)),
            group("parking", ("Free street parking", False)),
        ])
        self.assertIsNone(found["has_wifi"]["value"])
        self.assertEqual(found["has_wifi"]["reason"], "conflict")
        for key in ("accepts_card", "pet_friendly", "has_parking"):
            self.assertIsNone(found[key]["value"])
            self.assertEqual(found[key]["reason"], "partial_negative")

    def test_composite_negative_requires_all_card_options(self):
        found = features([group("payments", ("Credit cards", False), ("Debit cards", False))])
        self.assertIs(found["accepts_card"]["value"], False)
        found = features([group("payments", ("Credit cards", False), ("Debit cards", True))])
        self.assertIs(found["accepts_card"]["value"], True)

    def test_unlisted_missing_empty_and_malformed_sources_are_distinct(self):
        for raw, reason in (
            (None, "missing_about"),
            ("  ", "missing_about"),
            ([], "empty_about"),
            ('[]', "empty_about"),
            ("broken json", "malformed_about"),
            ({}, "malformed_about"),
            ([{"id": "amenities", "options": {}}], "malformed_about"),
            ([group("parking", ("Parking", True))], "not_listed"),
            ([group("amenities", ("Wi-Fi", "false"))], "malformed_option"),
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(features(raw)["has_wifi"]["value"])
                self.assertEqual(features(raw)["has_wifi"]["reason"], reason)

    def test_legacy_csv_json_strings_use_same_contract(self):
        found = features('[{"id":"highlights","options":[{"name":"Live music","enabled":true}]}]')
        self.assertIs(found["live_music"]["value"], True)
        self.assertIsNone(found["quiet"]["value"])


if __name__ == "__main__":
    unittest.main()
