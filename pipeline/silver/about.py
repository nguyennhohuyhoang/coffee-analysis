"""Extract evidence-backed attributes from a café's Google Maps About payload."""

import json
import unicodedata
from collections import defaultdict

ABOUT_VERSION = "about-v1"

# Each rule specifies the source group, recognized options, and the options
# whose negative values together establish that the whole feature is absent.
# Parking accepts any listed option in the parking group, as the directory does.
FEATURE_RULES = {
    "has_wifi": ("amenities", {"wi-fi", "free wi-fi"}, {"wi-fi"}),
    "laptop_friendly": (
        "popular_for", {"good for working on laptop"}, {"good for working on laptop"}
    ),
    "outdoor_seating": ("service_options", {"outdoor seating"}, {"outdoor seating"}),
    "quiet": ("atmosphere", {"quiet"}, {"quiet"}),
    "cozy": ("atmosphere", {"cozy"}, {"cozy"}),
    "pet_friendly": (
        "pets", {"dogs allowed", "dogs allowed inside", "dogs allowed outside"}, {"dogs allowed"}
    ),
    "has_parking": ("parking", None, {"parking"}),
    "accepts_card": (
        "payments", {"credit cards", "debit cards"}, {"credit cards", "debit cards"}
    ),
    "accepts_nfc": ("payments", {"nfc mobile payments"}, {"nfc mobile payments"}),
    "cash_only": ("payments", {"cash-only"}, {"cash-only"}),
    "accepts_reservations": ("planning", {"accepts reservations"}, {"accepts reservations"}),
    "vegan_options": ("offerings", {"vegan options"}, {"vegan options"}),
    "vegetarian_options": ("offerings", {"vegetarian options"}, {"vegetarian options"}),
    "rooftop": ("highlights", {"rooftop seating"}, {"rooftop seating"}),
    "live_music": ("highlights", {"live music"}, {"live music"}),
    "good_for_kids": ("children", {"good for kids"}, {"good for kids"}),
}


def normalize_label(label):
    label = unicodedata.normalize("NFKC", label).replace("‐", "-")
    return " ".join(label.casefold().split())


def decode_about(about):
    if about is None or isinstance(about, str) and not about.strip():
        return [], "missing_about"
    if isinstance(about, str):
        try:
            about = json.loads(about)
        except json.JSONDecodeError:
            return [], "malformed_about"
    if about is None:
        return [], "missing_about"
    if not isinstance(about, list):
        return [], "malformed_about"
    return about, "not_listed" if about else "empty_about"


def group_options(groups):
    options_by_group = defaultdict(list)
    malformed_groups = set()
    for group_index, group in enumerate(groups):
        group_id = group.get("id") if isinstance(group, dict) else None
        if not isinstance(group_id, str) or not group_id.strip():
            malformed_groups.add(None)
            continue
        group_key = normalize_label(group_id)
        options = group.get("options")
        if not isinstance(options, list):
            malformed_groups.add(group_key)
            continue
        for option_index, option in enumerate(options):
            label = option.get("name") if isinstance(option, dict) else None
            if not isinstance(label, str) or not label.strip():
                malformed_groups.add(group_key)
                continue
            evidence = {
                "group_id": group_id,
                "option": label,
                "enabled": option.get("enabled"),
                "group_index": group_index,
                "option_index": option_index,
            }
            options_by_group[group_key].append((normalize_label(label), evidence))
    return options_by_group, malformed_groups


def feature_value(matches, negative_options, missing_reason):
    positives = {label for label, item in matches if item["enabled"] is True}
    negatives = {label for label, item in matches if item["enabled"] is False}
    has_negative = negative_options <= negatives
    if positives & negatives or positives and has_negative:
        return None, "conflict"
    if positives:
        return True, "explicit_true"
    if any(type(item["enabled"]) is not bool for _, item in matches):
        return None, "malformed_option"
    if has_negative:
        return False, "explicit_false"
    if negatives:
        return None, "partial_negative"
    return None, missing_reason


def transform(about):
    """Return one tri-state feature record per rule, including unknown values."""
    groups, missing_reason = decode_about(about)
    options, malformed_groups = group_options(groups)
    features = []
    for key, (group, labels, negative_options) in FEATURE_RULES.items():
        matches = [
            (label, item) for label, item in options[group] if labels is None or label in labels
        ]
        reason = "malformed_about" if group in malformed_groups or None in malformed_groups else missing_reason
        value, reason = feature_value(matches, negative_options, reason)
        features.append({
            "feature_key": key,
            "value": value,
            "reason": reason,
            "evidence": [item for _, item in matches],
        })
    return features
