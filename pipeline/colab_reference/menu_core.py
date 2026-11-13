# -*- coding: utf-8 -*-
"""menu_core.py - lõi pipeline: tiền xử lý ảnh, dựng dòng từ box OCR, parse giá, khớp món, QC.
Không phụ thuộc OCR engine: engine chỉ cần trả về list box dạng
    {"text": str, "conf": float, "x0":..,"y0":..,"x1":..,"y1":..}
"""
import re, unicodedata, statistics, json

try:
    from rapidfuzz import fuzz
    _RF = True
except ImportError:                      # chỉ để test offline; trên Colab dùng rapidfuzz
    import difflib
    _RF = False
    class fuzz:                          # noqa
        @staticmethod
        def ratio(a, b):
            return 100 * difflib.SequenceMatcher(None, a, b).ratio()

# ------------------------------------------------------------------ chuẩn hoá chữ
def norm(s: str) -> str:
    """bỏ dấu, hạ chữ thường, bỏ ký tự lạ (đ -> d)."""
    s = (s or "").replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-zA-Z0-9\s&+]", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()

# ------------------------------------------------------------------ tiền xử lý ảnh
def deskew_angle(gray, max_deg=15):
    import cv2, numpy as np
    bw = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 255,
                               cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1)))   # nối chữ thành vệt ngang
    edges = cv2.Canny(bw, 50, 150)
    w = gray.shape[1]
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, threshold=80,
                            minLineLength=w // 6, maxLineGap=20)
    if lines is None:
        return 0.0
    angs = []
    for x1, y1, x2, y2 in lines[:, 0]:
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) <= max_deg:
            angs.append(a)
    return float(np.median(angs)) if len(angs) >= 5 else 0.0

def preprocess(img, target=1800):
    """resize cạnh dài ~target, xoay thẳng (lệch nhỏ), tăng tương phản CLAHE. img: BGR ndarray."""
    import cv2
    h, w = img.shape[:2]
    s = target / max(h, w)
    img = cv2.resize(img, None, fx=s, fy=s,
                     interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)
    ang = deskew_angle(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    if abs(ang) > 0.5:
        h, w = img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
        img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_REPLICATE)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

# ------------------------------------------------------------------ parse giá
_UNIT_NO = r"(?!\s*(?:%|(?:ml|g|gr|oz|cl|l|kg|cm|mm|cal|kcal|pcs|x|am|pm|h|km|phut|min)\b))"
PRICE_RE = re.compile(
    r"(?<![\w.,:])(?<!\d )(?:"
    r"(?P<usd>\$\s*\d+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?\s*(?:usd|\$))"
    r"|(?P<k>\d{1,3}(?:[.,]\d)?\s?[kK])(?![a-zA-Z])"
    r"|(?P<full>(?:\d{1,3}(?:[., ]\d{3})+)(?:\s?(?:đ|₫|d|vnd|vnđ))?"
    r"|\d{4,7}\s?(?:đ|₫|d|vnd|vnđ)(?!\w))(?!\d)"
    r"|(?P<bare>\d{2,3})" + _UNIT_NO + r"(?![\d.,:]*\d)"
    r")(?P<plus>\s*\+\+)?", re.I)

def parse_prices(text, bare_is_k=True):
    """Trả list {value, kind, raw, flags, span}. value tính bằng VND (USD giữ nguyên số, kind='usd')."""
    out = []
    for m in PRICE_RE.finditer(text):
        raw = m.group(0).strip()
        flags = []
        if m.group("plus"):
            flags.append("plus_plus")
        if m.group("usd"):
            v = float(re.sub(r"[^\d.,]", "", m.group("usd")).replace(",", "."))
            out.append(dict(value=v, kind="usd", raw=raw, flags=flags + ["usd"], span=m.span()))
            continue
        if m.group("k"):
            v = float(re.sub(r"[^\d.,]", "", m.group("k")).replace(",", ".")) * 1000
        elif m.group("full"):
            v = float(re.sub(r"[^\d]", "", m.group("full")))
        else:                                               # số trần: 35 -> 35.000 nếu menu ghi theo nghìn
            if not bare_is_k:
                continue
            v = float(m.group("bare")) * 1000
            flags.append("inferred_k")
        if 3000 <= v <= 3_000_000:                           # lọc số rác (sđt, năm, gram...)
            out.append(dict(value=v, kind="vnd", raw=raw, flags=flags, span=m.span()))
    return out

def split_name_price(text, bare_is_k=True):
    """'Cà phê sữa ....... 29K/35K' -> ('Cà phê sữa', [29000, 35000], flags)"""
    ps = parse_prices(text, bare_is_k)
    if not ps:
        return clean_name(text), [], []
    name = clean_name(text[: ps[0]["span"][0]])
    return name, ps, sorted({f for p in ps for f in p["flags"]})

def clean_name(s):
    return re.sub(r"[\s.\-–—:_·•|/\\]+$|^[\s.\-–—:_·•|/\\]+", "", s or "").strip()

# ------------------------------------------------------------------ dựng dòng từ toạ độ
def _cy(b): return (b["y0"] + b["y1"]) / 2
def _h(b): return max(1.0, b["y1"] - b["y0"])

def group_rows(boxes):
    if not boxes:
        return [], 1.0
    mh = statistics.median(_h(b) for b in boxes)
    rows = []
    for b in sorted(boxes, key=_cy):
        if rows and abs(_cy(b) - rows[-1]["cy"]) <= 0.6 * mh:
            r = rows[-1]; r["boxes"].append(b)
            r["cy"] = sum(_cy(x) for x in r["boxes"]) / len(r["boxes"])
        else:
            rows.append({"cy": _cy(b), "boxes": [b]})
    for r in rows:
        r["boxes"].sort(key=lambda b: b["x0"])
    return rows, mh

def merge_cells(row_boxes, mh):
    """gộp các box sát nhau (khoảng cách < ~1 chiều cao chữ) thành 1 ô."""
    cells = []
    for b in row_boxes:
        if cells and b["x0"] - cells[-1]["x1"] < 1.0 * mh:
            c = cells[-1]
            c["text"] += " " + b["text"]; c["x1"] = max(c["x1"], b["x1"])
            c["y0"] = min(c["y0"], b["y0"]); c["y1"] = max(c["y1"], b["y1"])
            c["conf"] = min(c["conf"], b["conf"])
        else:
            cells.append(dict(b))
    return cells

def extract_items(boxes, bare_is_k=True):
    """Box OCR -> (items có giá, list tên chưa gán giá). Mỗi item truy ngược được về box gốc."""
    rows, mh = group_rows(boxes)
    items, pending = [], []

    def mk(name, ps, flags, c):
        return dict(name_raw=name, name_norm=norm(name),
                    prices=[p["value"] for p in ps], currency=ps[0]["kind"],
                    price_observations=[
                        dict(amount=p["value"], currency=p["kind"].upper(), raw_text=p["raw"],
                             inferred_k="inferred_k" in p["flags"], plus_plus="plus_plus" in p["flags"])
                        for p in ps
                    ],
                    price_raw=" / ".join(p["raw"] for p in ps), flags=flags, ocr_conf=c["conf"],
                    bbox=[c["x0"], c["y0"], c["x1"], c["y1"]], line_text=c["text"])

    for row in rows:
        row_pending = []
        for c in merge_cells(row["boxes"], mh):
            name, ps, flags = split_name_price(c["text"], bare_is_k)
            if name and len(name) >= 2 and ps:
                items.append(mk(name, ps, flags, c))
            elif name and len(name) >= 2:
                c["name"] = name; row_pending.append(c); pending.append((row["cy"], c))
            elif ps:                                          # ô chỉ có giá -> gán cho tên gần nhất
                tgt = None
                if row_pending:                               # cùng dòng, ô tên liền trước
                    tgt = row_pending.pop()
                    pending[:] = [(y, p) for y, p in pending if p is not tgt]
                else:                                         # giá nằm dưới tên
                    cand = [(y, p) for y, p in pending
                            if 0 < row["cy"] - y <= 2.5 * mh
                            and c["x0"] <= p["x1"] + mh and c["x1"] >= p["x0"] - mh]
                    if cand:
                        y, tgt = max(cand, key=lambda t: t[0])
                        pending.remove((y, tgt))
                if tgt:
                    merged = dict(tgt); merged["conf"] = min(tgt["conf"], c["conf"])
                    merged["x1"] = max(tgt["x1"], c["x1"]); merged["text"] = tgt["text"] + " " + c["text"]
                    items.append(mk(tgt["name"], ps, flags + ["price_adjacent"], merged))
    return items, [p["name"] for _, p in pending]

# ------------------------------------------------------------------ từ điển món chuẩn + khớp mờ
CANON = {
    "ca_phe_den":  ("Cà phê đen",  ["ca phe den", "cafe den", "cf den", "den da", "den nong", "black coffee", "vietnamese black coffee", "ca phe phin den"]),
    "ca_phe_sua":  ("Cà phê sữa",  ["ca phe sua", "cafe sua", "cf sua", "ca phe nau", "nau da",
                                     "vietnamese iced coffee", "vietnamese milk coffee", "milk coffee", "brown coffee"]),
    "bac_xiu":     ("Bạc xỉu",     ["bac xiu", "bac siu", "bac sui", "bac xui", "bach xiu",
                                     "white coffee", "bac xiu truyen thong"]),
    "ca_phe_muoi": ("Cà phê muối", ["ca phe muoi", "cafe muoi", "salt coffee", "salted coffee"]),
    "ca_phe_trung":("Cà phê trứng",["ca phe trung", "cafe trung", "egg coffee"]),
    "ca_phe_dua":  ("Cà phê cốt dừa", ["ca phe cot dua", "coconut coffee", "cafe dua", "ca phe dua"]),
    "americano":   ("Americano",   ["americano", "iced americano", "hot americano"]),
    "espresso":    ("Espresso",    ["espresso", "expresso", "espresso shot"]),
    "latte":       ("Latte",       ["latte", "cafe latte", "ca phe latte", "iced latte", "hot latte"]),
    "cappuccino":  ("Cappuccino",  ["cappuccino", "capuchino", "capuccino", "cappucino"]),
    "flat_white":  ("Flat white",  ["flat white"]),
    "cold_brew":   ("Cold brew",   ["cold brew", "coldbrew", "cold brew coffee"]),
    "matcha_latte":("Matcha latte",["matcha latte", "matcha sua", "tra xanh latte", "matcha milk"]),
    "tra_dao":     ("Trà đào",     ["tra dao", "peach tea", "tra dao cam sa", "tra dao cam xa"]),
    "tra_vai":     ("Trà vải",     ["tra vai", "lychee tea"]),
    "sinh_to_bo":  ("Sinh tố bơ",  ["sinh to bo", "avocado smoothie", "bo xay"]),
    "nuoc_cam":    ("Nước cam",    ["nuoc cam", "nuoc cam vat", "orange juice", "fresh orange juice", "cam vat"]),
}
_MODIFIERS = {"da", "nong", "hot", "iced", "ice", "cold", "size", "s", "m", "l", "xl", "ly", "special", "dac", "biet"}
import os, json as _json
_EXTRA = "extra_aliases.json"
if os.path.exists(_EXTRA):
    for _cid, _d in _json.load(open(_EXTRA, encoding="utf-8")).items():
        if _cid in CANON:                                  # món đã có: thêm biệt danh
            CANON[_cid][1].extend(_d.get("aliases", []))
        else:                                              # món mới
            CANON[_cid] = (_d["display"], list(_d.get("aliases", [])))
_SYN = [(norm(s), cid) for cid, (_, syns) in CANON.items() for s in syns]
OK_T, REVIEW_T = 90, 75

def match_item(name_norm):
    """Trả (canonical_id|None, score, how). Giải thích được: 'exact' | 'contains' | 'fuzzy'."""
    if not name_norm:
        return None, 0, "none"
    best = (None, 0, "none")
    name_norm = re.sub(r"\b\d+\s?(ml|oz|g|cl)\b", " ", name_norm)
    name_norm = re.sub(r"\s+", " ", name_norm).strip()
    qtok = name_norm.split()
    for syn, cid in _SYN:
        if name_norm == syn:
            return cid, 100, "exact"
        s, how = 0, "fuzzy"
        stok = syn.split()
        if len(stok) >= 2 or len(syn) >= 7:                    # tránh khớp chứa với từ đơn ngắn
            for i in range(len(qtok) - len(stok) + 1):
                if qtok[i:i + len(stok)] == stok:
                    extra = [t for t in qtok if t not in stok]
                    s = 92 if all(t in _MODIFIERS for t in extra) and len(extra) <= 3 else 82
                    how = "contains"
                    break
        r = fuzz.ratio(name_norm, syn) if len(name_norm) >= 5 and abs(len(name_norm) - len(syn)) <= 3 else 0
        if len(qtok) != len(stok):
            r = min(r, 89)                                   # khác số từ -> luôn phải xem lại
        if r > s:
            s, how = r, "fuzzy"
        if s > best[1]:
            best = (cid, round(s, 1), how)
    return (best[0], best[1], best[2]) if best[1] >= REVIEW_T else (None, best[1], "none")

# ------------------------------------------------------------------ QC + gắn nhãn
def finalize(items, min_conf=0.80):
    """Khớp món + cờ 'cần xem lại'. Không tự đoán: điểm thấp / OCR thấp / giá bất thường -> needs_review."""
    out = []
    for it in items:
        cid, score, how = match_item(it["name_norm"])
        reasons = []
        if cid and score < OK_T: reasons.append("match_low")
        if it["ocr_conf"] < min_conf: reasons.append("ocr_low")
        if "usd" in it["flags"]: reasons.append("usd")
        if it["currency"] == "vnd" and cid and not (8000 <= min(it["prices"]) <= 400000):
            reasons.append("price_outlier")
        out.append({**it, "canonical_id": cid, "match_score": score, "match_how": how,
                    "needs_review": bool(reasons), "review_reasons": reasons})
    return out

def is_menu_like(boxes, items):
    """Ảnh có phải menu? nhiều chữ + nhiều giá. Dùng để loại ảnh đồ ăn / không gian."""
    return len(boxes) >= 15 and len(items) >= 5

def decide_bare_is_k(boxes):
    """Nếu đa số số trần là 2–3 chữ số và không có giá dạng 35.000 -> menu ghi theo nghìn."""
    full = sum(1 for b in boxes for p in parse_prices(b["text"], True) if "inferred_k" not in p["flags"])
    bare = sum(1 for b in boxes for p in parse_prices(b["text"], True) if "inferred_k" in p["flags"])
    return bare >= 3 and bare >= full