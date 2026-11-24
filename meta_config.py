"""Cấu hình RIÊNG cho phiên bản Meta Graph API trực tiếp (webhook Meta, không qua poller Pancake).

QUAN TRỌNG — CÁCH LY HOÀN TOÀN với config.py/Settings (bản poller đang chạy production):
  - File này KHÔNG sửa 1 dòng nào của config.py/Settings hiện có.
  - Chỉ chứa cấu hình CẤP APP (dùng CHUNG cho mọi trang — App ID/Secret/Verify Token không đổi theo trang).
    Thông tin RIÊNG từng trang (Graph API Page Access Token, trỏ file .env Pancake tương ứng) nằm trong
    meta_pages.json — xem meta_orchestrator.py (đọc đa trang, không còn giới hạn 1 trang/tiến trình).
  - Đọc từ file .env RIÊNG (mặc định .env.meta), set qua biến môi trường META_ENV_FILE nếu muốn đổi.
  - Chạy như 1 tiến trình HOÀN TOÀN TÁCH BIỆT (xem main_meta.py):
      META_ENV_FILE=.env.meta uvicorn main_meta:app --port 8100
    (Không còn cần BOT_ENV_FILE nữa — mỗi trang trong meta_pages.json tự khai trỏ file .env Pancake riêng.)
  - poller.py/pancake_client.py/orchestrator.py/main.py hiện tại không hề biết tới sự tồn tại của phiên
    bản này — không import, không gọi tới file nào ở đây.
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class MetaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("META_ENV_FILE", ".env.meta"), env_file_encoding="utf-8", extra="ignore"
    )

    # An toàn: phải bật RÕ RÀNG mới cho webhook xử lý — mặc định TẮT dù có token hay chưa.
    meta_enabled: bool = False

    # ---- Cấp APP (Meta for Developers) — CHUNG cho mọi trang trong meta_pages.json ----
    meta_app_id: str = ""             # App ID
    meta_app_secret: str = ""         # Dùng verify chữ ký webhook (header X-Hub-Signature-256)
    meta_webhook_verify_token: str = ""  # Chuỗi tự đặt, dùng cho bước Meta xác minh URL webhook (GET)
    meta_graph_api_version: str = "v21.0"
    meta_graph_base_url: str = "https://graph.facebook.com"

    # File khai từng trang: {page_id: {name, meta_page_access_token, pancake_env_file}}
    meta_pages_config_path: str = "meta_pages.json"

    # ---- Điều tiết giống bản cũ, để hành vi tư vấn nhất quán (KHÔNG còn đọc qua config.py/BOT_ENV_FILE —
    # vì 1 tiến trình giờ phục vụ NHIỀU trang cùng lúc, mỗi trang lẽ ra có thể có .env riêng với giá trị
    # khác nhau, nhưng để đơn giản cho bản này dùng chung 1 giá trị cho tất cả trang trong meta_pages.json).
    debounce_seconds: float = 0
    history_messages: int = 60
    stop_when_has_phone: bool = True
    required_phones: int = 1

    # ---- Tái chăm khách im lặng (xem meta_reengage.py) — câu mẫu cố định theo reengage_rephrase.md.
    reengage_enabled: bool = True
    reengage_dry_run: bool = True            # True = chỉ ghi log sẽ gửi gì, KHÔNG gửi thật
    reengage_first_after_hours: float = 3    # khách im quá X giờ sau tin bot -> tái chăm lần 1
    reengage_second_after_hours: float = 5   # lần 1 xong vẫn im thêm X giờ -> lần 2 (xin SĐT)
    # Facebook chỉ cho page nhắn chủ động trong 24h kể từ tin CUỐI của khách -> chừa biên an toàn.
    reengage_window_hours: float = 23.5
    reengage_send_start: int = 7             # chỉ gửi trong khung 7h..21h (giờ VN)
    reengage_send_end: int = 21
    reengage_scan_minutes: float = 10

    webhook_port: int = 8100


meta_settings = MetaSettings()
