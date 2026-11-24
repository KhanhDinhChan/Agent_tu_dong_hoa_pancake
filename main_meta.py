"""Entry point RIÊNG cho phiên bản Meta Graph API trực tiếp — PHIÊN BẢN MỚI, HOÀN TOÀN TÁCH BIỆT khỏi
main.py (bản poller đang chạy production). Không import main.py, không chung port, không chung tiến trình.

ĐÃ VERIFY hoạt động thật 2026-08-20 cho trang Định Hướng (nhận tin, gửi tin, gắn tag, nhận diện nhân viên
can thiệp — xem ghi chú trong meta_orchestrator.py). Giờ hỗ trợ ĐA TRANG trong CÙNG 1 tiến trình — khai báo
từng trang (Graph API Page Access Token riêng) trong meta_pages.json, không cần chạy nhiều tiến trình/port.

Chạy (BOT_ENV_FILE chỉ còn ảnh hưởng nội dung/kịch bản AI dùng chung — xem giới hạn trong meta_orchestrator.py,
KHÔNG còn quyết định trang nào được xử lý nữa, việc đó do meta_pages.json quyết định):
  BOT_ENV_FILE=.env META_ENV_FILE=.env.meta uvicorn main_meta:app --port 8100

Đăng ký webhook trên Meta for Developers (app của bạn) trỏ về:
  https://<domain-cong-khai>/
Verify Token = đúng giá trị META_WEBHOOK_VERIFY_TOKEN trong .env.meta.
Nhớ BẬT field "message_echoes" khi đăng ký webhook (ngoài "messages") — cần cho việc phát hiện nhân viên
gửi tay (đã verify hoạt động đúng, xem meta_orchestrator._handle_echo).
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from meta_client import verify_webhook_signature
from meta_config import meta_settings
from meta_orchestrator import meta_orchestrator
from meta_reengage import reengage_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main_meta")

app = FastAPI()

# Ảnh lịch học tự host, phục vụ công khai qua tunnel: https://pages.vatlysieunham.online/img/<tên file>.
# Lý do: link content.pancake.vn chỉ lấy được qua API nội bộ Pancake (token 90 ngày, hay hết hạn — sự cố
# 2026-09-24); API công khai upload được nhưng chỉ trả mã nội bộ, không có link. Facebook tải ảnh đúng lúc bot
# gửi tin, tức lúc bot chắc chắn đang chạy, nên không thêm điểm hỏng mới nào.
app.mount("/img", StaticFiles(directory="public_images"), name="img")


@app.on_event("startup")
async def startup() -> None:
    if not meta_settings.meta_enabled:
        log.warning("META_ENABLED=False -> webhook sẽ TỪ CHỐI mọi request (an toàn mặc định).")
        return
    await meta_orchestrator.load_pages()
    # Chạy song song suốt đời tiến trình — ghi nhịp tim cho watchdog.py (tiến trình ngoài) + tự huỷ task
    # xử lý hội thoại nào bị treo quá lâu. Xem MetaOrchestrator.watchdog_loop().
    asyncio.create_task(meta_orchestrator.watchdog_loop())
    # Tái chăm khách im lặng chưa cho SĐT (câu mẫu cố định, không gọi AI). Xem meta_reengage.py.
    asyncio.create_task(reengage_loop(meta_orchestrator))
    # Danh bạ "hội thoại -> khách" ~1 năm để trả lời được khách CŨ quay lại nhắn. Xem customer_index_loop().
    asyncio.create_task(meta_orchestrator.customer_index_loop())


@app.get("/")
async def verify(request: Request) -> Response:
    """Bước xác minh URL webhook của Meta (gọi 1 lần khi đăng ký/đổi URL)."""
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == meta_settings.meta_webhook_verify_token:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/")
async def receive(request: Request) -> dict:
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    sig_ok = verify_webhook_signature(raw, signature)

    if not meta_settings.meta_enabled:
        return {"status": "disabled"}
    if not sig_ok:
        log.warning("Webhook Meta: chữ ký không hợp lệ -> từ chối request.")
        return {"status": "invalid_signature"}

    data = json.loads(raw)
    if data.get("object") != "page":
        return {"status": "ignored"}

    for entry in data.get("entry", []):
        page_id = entry.get("id", "")
        if not meta_orchestrator.has_page(page_id):
            log.info("Webhook Meta: bỏ qua sự kiện của trang chưa cấu hình (id=%s).", page_id)
            continue
        for event in entry.get("messaging", []):
            try:
                await meta_orchestrator.handle_event(page_id, event)
            except Exception:  # noqa: BLE001
                log.exception("Lỗi xử lý event webhook Meta (page=%s): %s", page_id, event)

    return {"status": "ok"}
