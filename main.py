"""
Webhook server nhận tin nhắn mới từ Pancake và đẩy vào orchestrator.

Chạy:  uvicorn main:app --host 0.0.0.0 --port 8000
Webhook cần HTTPS public (dùng ngrok khi dev):  ngrok http 8000
Đăng ký URL webhook trong Pancake:  https://<domain>/webhook/pancake
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request

from config import settings
from orchestrator import orchestrator
from pancake_client import pancake
from poller import poller_loop
from reengage import scheduler_loop
from tag_reengage import scheduler_loop as tag_reengage_scheduler_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="Pancake CSKH AI")


@app.on_event("startup")
async def startup() -> None:
    await orchestrator.load_tags()
    log.info("Server sẵn sàng. Page thử nghiệm=%s | Danh sách page CHO PHÉP=%s",
             settings.pancake_page_id, settings.allowed_page_id_set)
    # Bật scheduler tái chăm chạy nền (mặc định dry-run, chỉ log).
    if settings.reengage_enabled:
        asyncio.create_task(scheduler_loop())
        log.info("Đã bật tái chăm: sau %.0fh, khung %dh-%dh, dry_run=%s",
                 settings.reengage_after_hours, settings.reengage_active_start,
                 settings.reengage_active_end, settings.reengage_dry_run)
    # Bật scheduler tái chăm theo tag "Bot tái chăm" (khách hẹn trả lời sau) — khung giờ vàng riêng.
    if settings.tag_reengage_enabled:
        asyncio.create_task(tag_reengage_scheduler_loop())
        log.info("Đã bật tái chăm tag '%s': đợi %.0fh, tối đa %d lần, khung giờ vàng %dh-%dh, dry_run=%s",
                 settings.tag_reengage_tag_name, settings.tag_reengage_wait_hours,
                 settings.tag_reengage_max_attempts, settings.tag_reengage_golden_start,
                 settings.tag_reengage_golden_end, settings.tag_reengage_dry_run)
    # Bật poller: tự quét tin mới qua API (thay webhook).
    if settings.poller_enabled:
        asyncio.create_task(poller_loop())
        log.info("Đã bật poller: quét %ds/lần, dry_run=%s",
                 settings.poller_interval_seconds, settings.poller_dry_run)


@app.on_event("shutdown")
async def shutdown() -> None:
    await pancake.aclose()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/pancake")
async def pancake_webhook(request: Request) -> dict:
    """
    Nhận payload webhook từ Pancake.
    ⚠️ Cấu trúc payload cần khớp docs thật — bên dưới là giả định phổ biến.
    Thường payload chứa: page_id, conversation_id, customer_id, message, và cờ phân biệt
    tin do khách gửi (from_page/is_page = false).
    """
    # (Tùy chọn) xác thực webhook bằng WEBHOOK_SECRET nếu Pancake hỗ trợ ký.
    body = await request.json()
    log.info("Webhook: %s", body)

    # KHÓA CỨNG: chỉ xử lý page được phép thử nghiệm. Bỏ qua mọi page khác.
    page_id = str(body.get("page_id", "") or "")
    if page_id and page_id not in settings.allowed_page_id_set:
        log.warning("Bỏ qua webhook từ page %s (không nằm trong danh sách cho phép %s)",
                    page_id, settings.allowed_page_id_set)
        return {"skipped": "page not allowed"}

    # Bỏ qua tin do chính page/bot gửi ra, chỉ xử lý tin của khách.
    if body.get("from_page") or body.get("is_page"):
        return {"skipped": "message from page"}

    conversation_id = str(body.get("conversation_id", ""))
    customer_id = str(body.get("customer_id", ""))
    text = body.get("message") or body.get("content") or ""

    if conversation_id and text:
        await orchestrator.on_customer_message(conversation_id, customer_id, text)

    return {"received": True}
