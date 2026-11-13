"""Recognize menu-labeled Bronze photos and parse prices using the Colab core."""

from __future__ import annotations

import hashlib
import json
import re
from http.client import HTTPException
from importlib.metadata import PackageNotFoundError, version
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pipeline.colab_reference import menu_core

PARSER_VERSION = "menu-parser-v2"
OCR_VERSION = "ppocrv5-mobile-latin-clahe1800-v1"
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MENU_LABEL = re.compile(
    r"\b(?:menus?|menue|menyu|menyupan|thuc don|bang gia|price list|speisekarte|"
    r"carta de precios|cardapio)\b"
)
ADD_ON = re.compile(r"\b(?:extra|change to|upsize|up size|topping|add on|1 shot|flake)\b")
PHOTO_HOST = re.compile(r"(?:lh\d+|images\d*)\.googleusercontent\.com")


class ImageDownloadError(Exception):
    """The selected image could not be retrieved or decoded."""


class OCRSetupError(Exception):
    """The PaddleOCR runtime or model could not be initialized."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirect())


def _photo_url(url):
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return False
    return (
        not any(character.isspace() for character in url)
        and parts.scheme == "https"
        and bool(PHOTO_HOST.fullmatch(parts.netloc.lower()))
        and bool(parts.path.strip("/"))
        and not parts.fragment
    )


def menu_images(images):
    """Return unique source links whose own about label identifies a menu."""
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            return []
    if not isinstance(images, list):
        return []

    selected = {}
    for photo in images:
        if not isinstance(photo, dict):
            continue
        label = photo.get("about")
        url = photo.get("link")
        if not isinstance(label, str) or not isinstance(url, str):
            continue
        if not MENU_LABEL.search(menu_core.norm(label)) or not _photo_url(url):
            continue
        date = photo.get("published_at_date")
        selected.setdefault(
            url, {"url": url, "label": label, "photo_date": date if isinstance(date, str) else None}
        )
    return list(selected.values())


def fetch_image(url):
    """Return the SHA-256 and image bytes for a Google Maps photo."""
    if not isinstance(url, str) or not _photo_url(url):
        raise ImageDownloadError("unsupported_photo_url")

    # The desktop export asks for a thumbnail. Try the larger image first, as in Colab.
    larger_url = re.sub(r"=[wsh]\d+(?:-[a-z0-9]+)*$", "=s2000", url)
    for candidate in dict.fromkeys((larger_url, url)):
        request = Request(candidate, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
        try:
            with _OPENER.open(request, timeout=20) as response:
                if not response.headers.get_content_type().startswith("image/"):
                    continue
                data = response.read(MAX_IMAGE_BYTES + 1)
                if 0 < len(data) <= MAX_IMAGE_BYTES:
                    return hashlib.sha256(data).hexdigest(), data
        except (HTTPException, OSError, URLError):
            continue
    raise ImageDownloadError("photo_download_failed")


class PaddleEngine:
    """Lazily load the same preprocessing and multilingual model on either device."""

    def __init__(self, device="cpu"):
        self.device = device
        self._engine = None

    @property
    def version(self):
        runtimes = []
        for package in ("paddlepaddle", "paddlepaddle-gpu", "paddleocr", "paddlex", "opencv-contrib-python"):
            try:
                runtimes.append(f"{package}={version(package)}")
            except PackageNotFoundError:
                continue
        return ":".join((OCR_VERSION, self.device, *runtimes))

    def _load(self):
        if self._engine is None:
            try:
                from paddleocr import PaddleOCR

                self._engine = PaddleOCR(
                    device=self.device,
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="latin_PP-OCRv5_mobile_rec",
                    use_doc_orientation_classify=True,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    enable_mkldnn=False,
                    cpu_threads=4,
                )
            except Exception as error:
                raise OCRSetupError("paddleocr_initialization_failed") from error
        return self._engine

    def recognize(self, image_bytes):
        engine = self._load()
        import cv2
        import numpy as np

        try:
            image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        except cv2.error:
            raise ImageDownloadError("photo_decode_failed") from None
        if image is None:
            raise ImageDownloadError("photo_decode_failed")

        boxes = []
        for result in engine.predict(menu_core.preprocess(image, 1800)):
            for poly, text, confidence in zip(
                result["rec_polys"], result["rec_texts"], result["rec_scores"]
            ):
                points = np.asarray(poly, dtype=float).reshape(-1, 2)
                if len(points) == 0 or not np.isfinite(points).all() or not np.isfinite(confidence):
                    continue
                boxes.append(
                    {
                        "text": str(text),
                        "conf": float(confidence),
                        "x0": float(points[:, 0].min()),
                        "y0": float(points[:, 1].min()),
                        "x1": float(points[:, 0].max()),
                        "y1": float(points[:, 1].max()),
                    }
                )
        return boxes


def parse_observations(boxes, aliases=None):
    """Return the menu classification and all candidate items with review flags."""
    items, _ = menu_core.extract_items(boxes, bare_is_k=menu_core.decide_bare_is_k(boxes))
    items = menu_core.finalize(items)
    aliases = aliases or {}

    has_exact_priced_drink = False
    for item in items:
        reasons = item["review_reasons"]
        exact_id = aliases.get(item["name_norm"])
        if exact_id:
            item.update(canonical_id=exact_id, match_score=100, match_how="exact")
            if "match_low" in reasons:
                reasons.remove("match_low")
        if ADD_ON.search(item["name_norm"]):
            reasons.append("add_on")
        if not item["canonical_id"]:
            reasons.append("unmatched_name")
        if any(
            price["currency"] == "VND" and not 8000 <= price["amount"] <= 400000
            for price in item["price_observations"]
        ) and "price_outlier" not in reasons:
            reasons.append("price_outlier")
        item["needs_review"] = bool(reasons)
        if item["match_how"] == "exact" and item["ocr_conf"] >= 0.90 and not reasons:
            has_exact_priced_drink = True

    is_menu = menu_core.is_menu_like(boxes, items) or has_exact_priced_drink
    return ("ok" if is_menu else "not_menu"), items
