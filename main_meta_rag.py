"""Entry point RIÊNG cho PHIÊN BẢN RAG (định tuyến kịch bản theo lớp) — TÁCH BIỆT HOÀN TOÀN khỏi
main_meta.py đang chạy (không import main_meta.py, không chung port, không chung tiến trình). Dùng
CHUNG hạ tầng webhook/chữ ký (meta_client.py, meta_config.py — không phụ thuộc nội dung/AI) nhưng gọi
meta_orchestrator_rag thay vì meta_orchestrator.

Chạy (port KHÁC bản đang chạy — 8100 là bản gốc, đây dùng cổng khác, ví dụ 8101, để không xung đột nếu
lỡ chạy song song):
  uvicorn main_meta_rag:app --port 8101

Trước khi chạy: python build_knowledge_chunks.py (sinh knowledge_chunks.json từ thongtin/).

CHỈ dùng để TEST/so sánh (qua trình mô phỏng webhook, KHÔNG đăng ký webhook thật trỏ vào cổng này trừ
khi đã quyết định thay thế bản đang chạy) — Pancake/Graph API vẫn là hạ tầng THẬT như bản gốc nếu chạy
với dữ liệu thật, nên KHÔNG được chạy song song với main_meta.py trên cùng 1 trang thật (2 tiến trình
cùng trả lời 1 khách sẽ đá nhau).
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request, Response

from meta_client import verify_webhook_signature
from meta_config import meta_settings
from meta_orchestrator_rag import rag_orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main_meta_rag")

app = FastAPI()


@app.on_event("startup")
async def startup() -> None:
    if not meta_settings.meta_enabled:
        log.warning("META_ENABLED=False -> webhook sẽ TỪ CHỐI mọi request (an toàn mặc định).")
        return
    await rag_orchestrator.load_pages()


@app.get("/")
async def verify(request: Request) -> Response:
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
        log.warning("[RAG] Webhook Meta: chữ ký không hợp lệ -> từ chối request.")
        return {"status": "invalid_signature"}

    data = json.loads(raw)
    if data.get("object") != "page":
        return {"status": "ignored"}

    for entry in data.get("entry", []):
        page_id = entry.get("id", "")
        if not rag_orchestrator.has_page(page_id):
            log.info("[RAG] Webhook Meta: bỏ qua sự kiện của trang chưa cấu hình (id=%s).", page_id)
            continue
        for event in entry.get("messaging", []):
            try:
                await rag_orchestrator.handle_event(page_id, event)
            except Exception:  # noqa: BLE001
                log.exception("[RAG] Lỗi xử lý event webhook Meta (page=%s): %s", page_id, event)

    return {"status": "ok"}
