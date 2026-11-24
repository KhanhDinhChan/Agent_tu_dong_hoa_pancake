"""Cấu hình tập trung, đọc từ biến môi trường (.env).

Chạy nhiều page song song: set BOT_ENV_FILE trỏ tới file .env riêng của page đó
trước khi khởi động tiến trình (vd BOT_ENV_FILE=.env.news), mỗi tiến trình độc lập.
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("BOT_ENV_FILE", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # Pancake — API đã verify:
    #  - Public API (page_access_token, dùng lâu dài) cho bot: gửi tin/ảnh, tag, đọc hội thoại
    #  - Internal API (access_token = JWT đăng nhập) chỉ dùng lúc setup: liệt kê page, tạo page token
    pancake_page_id: str = ""
    pancake_page_access_token: str = ""       # token của page (long-lived) — bot dùng cái này
    pancake_access_token: str = ""            # JWT đăng nhập cá nhân — chỉ để setup, sẽ hết hạn
    pancake_base_url: str = "https://pages.fm/api/public_api/v1"
    pancake_internal_url: str = "https://pages.fm/api/v1"
    # Danh sách page ĐƯỢC PHÉP cho bot hoạt động (chặn thử nghiệm nhầm page).
    # Để trống = chỉ đúng PANCAKE_PAGE_ID. Nhiều page thì ngăn cách bằng dấu phẩy.
    allowed_page_ids: str = ""

    @property
    def allowed_page_id_set(self) -> set[str]:
        ids = {i.strip() for i in self.allowed_page_ids.split(",") if i.strip()}
        if self.pancake_page_id:
            ids.add(self.pancake_page_id.strip())
        return ids

    # Gemini (Google AI)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    # Độ "mềm mại/sáng tạo" của câu trả lời: 0.2 = rất bám mẫu, 0.9 = tự nhiên/đa dạng hơn.
    gemini_temperature: float = 0.1
    gemini_top_p: float = 0.95

    # File "bộ não" sale sinh ra từ Excel (xem build_knowledge.py)
    sales_playbook_path: str = "sales_playbook.md"

    # ==== Cấu hình riêng theo TỪNG DOANH NGHIỆP/TRANG (dùng chung 1 bộ code, khác nội dung qua .env) ====
    # Mỗi trang (Vật Lý Định Hướng, Vật Lý News, Đánh Giá Tư Duy...) chạy 1 tiến trình riêng với 1 file .env
    # riêng (BOT_ENV_FILE) — các đường dẫn dưới đây cho phép mỗi trang dùng bộ nội dung/tag/ảnh HOÀN TOÀN
    # riêng biệt, không đụng chạm nhau, dù cùng chạy chung 1 bộ code (orchestrator/poller/reengage...).
    # Giá trị mặc định GIỮ NGUYÊN như 2 trang Vật Lý hiện có (không set trong .env thì y hệt trước đây).
    business_name: str = "Vật lý siêu nhẩm"  # KHÔNG kèm chữ "Trung tâm" — prompt tự thêm "Trung tâm" phía trước
    known_image_urls_path: str = "known_image_urls.txt"
    schedule_image_map_path: str = "schedule_image_url_map.json"
    info_image_map_path: str = "info_image_url_map.json"
    # File JSON định nghĩa danh mục/tag nghiệp vụ riêng của trang (khóa học, đợt thi...) — xem cấu trúc
    # trong category_tags.json (2 trang Vật Lý) hay category_tags_dgtd.json (mẫu khung cho trang mới).
    category_tags_path: str = "category_tags.json"
    # File chứa TOÀN BỘ "luồng chốt" (system prompt) — phần quy trình/quy tắc vận hành, tách biệt khỏi
    # sales_playbook_path (phần dữ liệu FAQ/kịch bản mẫu). Đổi file này = đổi hẳn cách bot vận hành cho
    # 1 doanh nghiệp khác mà không đụng gì tới code hay 2 trang còn lại.
    system_prompt_path: str = "system_prompt_template.md"

    # Số tin nhắn gần nhất đọc lại làm ngữ cảnh trước khi trả lời (đọc "toàn bộ" hội thoại).
    history_messages: int = 60

    # Bot chỉ dừng khi đã thu đủ SĐT của khách (kịch bản quy_trinh_sale.xlsx: chỉ cần 1 số).
    stop_when_has_phone: bool = True
    required_phones: int = 1  # chỉ cần 1 SĐT (của người đang nhắn tin), không cần riêng SĐT học sinh

    # ==== Tái chăm (re-engage) ====
    reengage_enabled: bool = True
    reengage_after_hours: float = 6      # im lặng quá X giờ thì tái chăm
    reengage_active_start: int = 6       # chỉ gửi trong khung 6h..21h
    reengage_active_end: int = 21
    reengage_scan_minutes: int = 30      # chu kỳ quét (khi chạy scheduler trong main.py)
    reengage_dry_run: bool = True        # True = chỉ log, KHÔNG gửi thật (an toàn khi test)
    reengage_max_age_hours: float = 72   # không tái chăm hội thoại quá cũ (chết)
    reengage_tz_offset_hours: float = 7  # Pancake trả updated_at theo UTC, VN=+7 (đã kiểm chứng)
    reengage_retry_wait_hours: float = 48  # xin SĐT lần đầu chưa được -> đợi thêm X giờ mới thử lần 2
    reengage_max_attempts: int = 2         # tối đa 2 lần xin SĐT (lần 2 dùng câu khác, thuyết phục hơn)
    # An toàn chính sách Facebook: Messenger chỉ cho gửi tin TỰ DO trong 24h kể từ tin nhắn CUỐI của khách,
    # ngoài khung đó bắt buộc phải có "message tag" được duyệt (không dùng ở đây) mới gửi được, nếu không sẽ
    # bị Meta hạn chế quyền nhắn tin của Page. Để dưới 24h (không phải đúng 24h) để có biên an toàn cho lệch
    # giờ/độ trễ xử lý. Áp dụng cho MỌI tin tái chăm (reengage.py + tag_reengage.py) — kiểm tra lại NGAY TRƯỚC
    # khi gửi thật, dựa vào tin nhắn cuối cùng của khách đọc được tại thời điểm gửi (không dùng field cache).
    reengage_facebook_safe_window_hours: float = 23.5

    # ==== Tái chăm theo TAG "Bot tái chăm" (khách hẹn trả lời sau/hỏi người nhà/để suy nghĩ) ====
    # Khác reengage ở trên (tái chăm xin SĐT theo giờ im lặng, tin cố định): module này chọn hội thoại qua
    # TAG (AI tự gắn), tin do AI tự soạn riêng từng hội thoại, chỉ gửi trong khung giờ vàng.
    tag_reengage_enabled: bool = True
    tag_reengage_tag_name: str = "Cần tái chăm"
    tag_reengage_wait_hours: float = 20      # đợi ít nhất X giờ kể từ lần tái chăm trước (hoặc từ tin hẹn)
    # (giảm từ 24h -> 20h để còn nằm dưới reengage_facebook_safe_window_hours=23.5h tính tới lúc gửi thật,
    # có biên ~3.5h dự phòng cho chu kỳ quét + chờ khung giờ vàng — nếu không sẽ luôn bị chốt chặn 24h chặn)
    tag_reengage_max_attempts: int = 2       # tối đa N lần nhắn lại rồi dừng hẳn, tránh làm phiền khách
    tag_reengage_golden_start: int = 19      # khung giờ vàng — lúc khách thường online/rảnh trả lời
    tag_reengage_golden_end: int = 21
    tag_reengage_scan_minutes: int = 30      # chu kỳ quét (khi chạy scheduler trong main.py)
    tag_reengage_dry_run: bool = True        # True = chỉ log, KHÔNG gửi thật (an toàn khi test)
    tag_reengage_tz_offset_hours: float = 7  # Pancake trả inserted_at theo UTC, VN=+7

    # ==== Poller (tự quét tin mới qua API, thay cho webhook) ====
    poller_enabled: bool = True
    poller_interval_seconds: int = 5     # chu kỳ quét tin mới
    poller_dry_run: bool = False         # True = chỉ log, KHÔNG trả lời thật

    # Điều tiết tốc độ phản hồi (bot chăm sóc khách hàng 24/7, không giới hạn giờ hoạt động)
    reply_min_delay_seconds: float = 0
    reply_max_delay_seconds: float = 0
    debounce_seconds: float = 0
    max_messages_per_minute: int = 20

    # Webhook
    webhook_secret: str = "change_me"


settings = Settings()
