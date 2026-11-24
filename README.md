# AI Chăm sóc khách hàng cho Pancake Omnichannel

Bot AI **chốt sale + chăm sóc khách hàng** kết nối **Pancake Omnichannel** (pancake.vn — chạy trên Public API domain `pages.fm`). Dùng **Gemini (Google AI)** để hiểu ý khách và soạn trả lời, với "bộ não" quy trình sale nạp từ file Excel của bạn.

**3 tính năng chính:**
- 🖼️ **Gửi ảnh** — AI chọn ảnh trong thư viện mẫu (bảng giá, hướng dẫn…) theo ngữ cảnh.
- 🏷️ **Gắn tag** — AI phân loại hội thoại và gắn tag nghiệp vụ (chốt đơn, khiếu nại, cần người thật…).
- ⏱️ **Kiểm soát tốc độ** — độ trễ ngẫu nhiên, gộp tin (debounce), giới hạn tin/phút, giờ hoạt động, tự chuyển người thật khi không chắc chắn.

---

## Cấu trúc project

| File | Vai trò |
|------|---------|
| `main.py` | Webhook server (FastAPI) nhận tin từ Pancake |
| `orchestrator.py` | Điều phối: debounce → AI → gửi tin → gắn tag → handoff |
| `ai_service.py` | Gọi Gemini, trả JSON (reply + tags + handoff); reply nhúng URL ảnh + `[BREAK]` |
| `pancake_client.py` | Client gọi Pancake API (gửi text/ảnh, gắn tag) |
| `rate_controller.py` | Độ trễ, throttle (tin/phút), giờ hoạt động |
| `build_knowledge.py` | Tạo dữ liệu kiến thức từ các nguồn riêng do bạn cung cấp trong `thongtin/` |
| `download_images.py` | Tải ảnh trong kịch bản về `images/` + `manifest.csv` |
| `config.py` | Đọc cấu hình từ `.env` |
| `list_pages.py` | Script lấy `page_id` + `page_access_token` |
| `thongtin/` | Thư mục nguồn kiến thức riêng; không được đưa vào repository công khai |

---

Repository công khai này chỉ chứa mã nguồn và hướng dẫn. Không bao gồm thông tin xác thực, dữ liệu khách hàng, log chạy, ảnh hoặc tài liệu/kịch bản kinh doanh. Hãy chuẩn bị nội dung riêng trong `thongtin/` trước khi tạo knowledge base.

## Cấu hình API

Ứng dụng đọc thông tin xác thực từ `.env` tại máy chạy bot. Các giá trị ví dụ trong `.env.example` được để trống; hãy tự cấu hình token Pancake, API key Gemini và webhook secret của bạn.

---

## Cài đặt & chạy

```bash
# 1. Tạo môi trường ảo
python -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell/CMD)

# 2. Cài thư viện
pip install -r requirements.txt

# 3. Tạo .env từ .env.example rồi tự điền page ID, token và API key của bạn
#    -> Lấy Gemini API key: https://aistudio.google.com/app/apikey

# 4. Sinh "bộ não" sale từ thư mục thongtin/ (đã sinh sẵn, chạy lại khi cập nhật)
python build_knowledge.py     # -> sales_playbook.md + known_image_urls.txt
python download_images.py     # (tùy chọn) tải ảnh về images/

# 5. Chạy webhook server
uvicorn main:app --host 0.0.0.0 --port 8000

# 6. Mở HTTPS public cho webhook (khi dev)
ngrok http 8000
# -> Đăng ký URL https://<ngrok>/webhook/pancake trong Pancake
```

---

## Tùy chỉnh

- **Quy trình sale / ảnh:** tự cung cấp nguồn trong `thongtin/` rồi chạy `python build_knowledge.py`. Không commit tài liệu, dữ liệu hoặc thông tin xác thực riêng lên repository công khai.
- **Tải ảnh backup:** `python download_images.py` → lưu vào `images/` + `images/manifest.csv`.
- **Tag:** tạo các tag trong Pancake trùng tên với `ALLOWED_TAGS` trong `ai_service.py` (`can_tu_van`, `hoi_gia`, `khoi_cap2`, `khoi_cap3`, `da_co_sdt`, `tu_choi_sdt`, `can_nguoi_that`, `spam`).
- **Giọng / cách chốt:** sửa `SYSTEM_PROMPT` trong `ai_service.py`.
- **Đổi model:** `GEMINI_MODEL` trong `.env` — `gemini-2.5-flash` (nhanh, rẻ) hoặc `gemini-2.5-pro` (chất lượng cao hơn).
- **Tốc độ phản hồi:** chỉnh các biến `REPLY_*`, `DEBOUNCE_SECONDS`, `MAX_MESSAGES_PER_MINUTE`, `ACTIVE_HOUR_*` trong `.env`.

---

## Lưu ý an toàn & vận hành

- **Chính sách Meta:** bot trả lời tự động phải tuân thủ chính sách Facebook (cửa sổ 24h, không spam) để tránh bị khóa page.
- **Người thật review:** đơn hàng/khiếu nại quan trọng → AI tự đặt `handoff` và gắn tag `can_nguoi_that`.
- **Bảo mật:** không commit `.env` lên git; token và dữ liệu khách hàng cần được bảo vệ.
