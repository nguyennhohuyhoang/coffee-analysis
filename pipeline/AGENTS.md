# 🐍 Pipeline Agent Guidelines (`pipeline/`)

> **File ngữ cảnh dành cho Agent làm việc trong thư mục `pipeline/`.**
> Nhiệm vụ: Thu thập dữ liệu từ Google Maps, bóc tách thực đơn (Menu OCR), trích xuất tiện ích & cơ sở vật chất, và đồng bộ vào Supabase PostgreSQL theo kiến trúc 3 tầng (Bronze $\rightarrow$ Silver $\rightarrow$ Gold).

---

## 1. Vai trò & Phạm vi
- Quản lý các kịch bản cào dữ liệu Google Maps (sử dụng thư viện `omkarcloud/google-maps-scraper` / `botasaurus`).
- Quản lý engine bóc tách thực đơn (dựa trên `colab_reference/menu_core.py` và PaddleOCR).
- Xây dựng module trích xuất cơ sở vật chất (`facilities/`): Bóc tách trường `about` và khai thác từ khóa trong `detailed_reviews`.
- Quản lý migrations và kết nối database Supabase (`db/`).

---

## 2. Kiến trúc Tầng Dữ liệu (Supabase DDL)

Pipeline snapshot hằng tuần chạy qua GitHub Actions và Omkar Cloud. Xem [hướng dẫn
Bronze](bronze/README.md) và [schema](db/migrations/001_bronze.sql) cho hai bảng
`collection_runs` và `cafe_snapshots`. Workflow này chỉ ghi snapshot vào Supabase
Postgres và giữ nguyên các tuần đã thu thập. Các mục bên dưới mô tả pipeline
OCR và cơ sở vật chất hiện có.


### 2.1. Bronze Layer (Raw)
```sql
CREATE TABLE IF NOT EXISTS raw_gmaps_scrapes (
    place_id        TEXT PRIMARY KEY,
    name            TEXT,
    query_keyword   TEXT,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'processing' | 'completed' | 'failed'
    error_message   TEXT,
    scraped_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_raw_status ON raw_gmaps_scrapes(status);
```

### 2.2. Silver Layer (Processed & Normalized)
Kế thừa các bảng từ [colab_reference/schema.sql](file:///d:/UNIVERSITY/coffee_analysis/pipeline/colab_reference/schema.sql) với các nâng cấp:

1. **`shop`**: Đổi kiểu cột `raw_json` sang `JSONB`.
2. **`shop_facilities`**: Bảng mới lưu trữ hạ tầng quán:
```sql
CREATE TABLE IF NOT EXISTS shop_facility (
    place_id            TEXT PRIMARY KEY REFERENCES shop(place_id) ON DELETE CASCADE,
    has_wifi            BOOLEAN,
    wifi_note           TEXT,
    power_outlets       TEXT CHECK (power_outlets IN ('plenty', 'few', 'none', 'unknown')),
    has_ac              BOOLEAN,
    work_friendly_score REAL,                  -- Thang 1.0 - 10.0
    noise_level         TEXT CHECK (noise_level IN ('quiet', 'moderate', 'lively', 'loud')),
    seating_types       JSONB DEFAULT '[]',    -- ["indoor", "outdoor", "rooftop"]
    parking_car         BOOLEAN,
    parking_motorbike   BOOLEAN,
    pet_friendly        BOOLEAN,
    payment_methods     JSONB DEFAULT '[]',    -- ["cash", "card", "nfc"]
    vibe_tags           JSONB DEFAULT '[]',    -- ["specialty_coffee", "cozy", "minimalist"]
    evidence            JSONB DEFAULT '{}',    -- Lưu câu trích dẫn từ review hoặc tag about
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
```
3. **`menu_image`**: Thêm cột `raw_boxes JSONB` để lưu cache kết quả detector toạ độ OCR. Khi điều chỉnh regex giá hoặc từ điển món, chỉ cần đọc lại `raw_boxes` mà không cần gọi lại GPU OCR.

### 2.3. Gold Layer (Views)
- **`v_typical_price`**: Giữ nguyên view từ `schema.sql` (tính giá min, max của từng món chuẩn theo từng quán).
- **`v_cafe_directory`**: View tổng hợp cho Web UI:
```sql
CREATE OR REPLACE VIEW v_cafe_directory AS
SELECT 
    s.place_id,
    s.name,
    s.address,
    s.lat,
    s.lng,
    s.rating,
    s.reviews,
    s.price_range,
    s.maps_link,
    f.has_wifi,
    f.has_ac,
    f.power_outlets,
    f.work_friendly_score,
    f.pet_friendly,
    f.seating_types,
    f.vibe_tags,
    COUNT(DISTINCT mi.item_id) AS total_menu_items
FROM shop s
LEFT JOIN shop_facility f ON f.place_id = s.place_id
LEFT JOIN menu_item mi ON mi.place_id = s.place_id
GROUP BY s.place_id, f.place_id;
```

---

## 3. Chiến lược Cào Khu vực An Thượng (Google Maps Ingestion)

An Thượng (Mỹ An, Ngũ Hành Sơn) có mật độ quán cafe rất cao. Google Maps giới hạn ~120 kết quả cho mỗi cụm tìm kiếm. Bắt buộc phải dùng danh sách truy vấn dạng lưới (Grid Querying):
```python
AN_THUONG_QUERIES = [
    "quán cafe An Thượng Ngũ Hành Sơn Đà Nẵng",
    "coffee shop An Thuong Da Nang",
    "cafe đường Châu Thị Vĩnh Tế Đà Nẵng",
    "cafe đường Hoàng Kế Viêm Đà Nẵng",
    "cafe đường Trần Bạch Đằng Đà Nẵng",
    "cafe đường Ngô Thì Sĩ Đà Nẵng",
    "cafe đường Phan Tứ Đà Nẵng",
    "specialty coffee An Thuong Da Nang",
    "coworking cafe An Thuong Da Nang"
]
```

### Cấu hình Scraper
- `max_images = 15` (lấy đủ ảnh bao gồm menu và không gian).
- `max_reviews = 25` (sắp xếp theo liên quan hoặc mới nhất để bóc tách tiện ích).
- `scrape_about = True` (bắt buộc để lấy Wi-Fi, thanh toán, dịch vụ).

---

## 4. Quy tắc Code & Sửa Lỗi Thừa kế từ Colab

1. **Upsert bắt buộc**:
   - Trong `menu_db.py`, thay thế hàm `insert_shop` bằng cơ chế `ON CONFLICT (place_id) DO UPDATE SET rating = EXCLUDED.rating, reviews = EXCLUDED.reviews`.
   - Tuyệt đối không dừng luồng khi thấy quán đã tồn tại.
2. **Xử lý ảnh đầu vào (`get_candidate_images`)**:
   - Đọc trực tiếp từ `images` của Omkar Scraper.
   - Ưu tiên sắp xếp ảnh có nhãn `Menu` lên trước.
3. **Prefetch ảnh đa luồng**:
   - Sử dụng `ThreadPoolExecutor(max_workers=6)` để tải ảnh trước khi đưa vào hàm xử lý OCR.
4. **Từ điển món Tây An Thượng**:
   - Mở rộng `CANON` với các món: `flat_white`, `cold_brew`, `kombucha`, `matcha_latte`, `smoothie_bowl`, `croissant`.

---

## 5. Danh sách Kiểm tra Kiểm thử (Testing Checklist)
- [ ] Chạy scraper với 1 query thử nghiệm $\rightarrow$ Ghi thành công vào `raw_gmaps_scrapes`.
- [ ] Chạy `run_pipeline.py --limit 3` $\rightarrow$ Đọc từ Bronze, trích xuất OCR + Cơ sở vật chất, đẩy lên Silver.
- [ ] Query view `v_cafe_directory` và `v_typical_price` kiểm tra dữ liệu ra đầy đủ, không bị null các trường chính.
- [ ] Chạy lại lệnh pipeline một lần nữa với cùng 3 quán $\rightarrow$ Xác nhận không crash và không tăng số lượng bản ghi trùng lặp.
