"""
Poller: tự quét tin nhắn mới qua API theo chu kỳ (thay cho webhook).

Cứ mỗi poller_interval_seconds, đọc danh sách hội thoại mới nhất; hội thoại nào có
tin CUỐI là của KHÁCH và MỚI hơn lần quét trước -> đẩy vào orchestrator để bot trả lời.

An toàn:
  - Lần quét ĐẦU khi khởi động chỉ GHI MỐC (baseline), KHÔNG trả lời loạt tin cũ tồn đọng.
    Chỉ tin đến SAU khi poller chạy mới được trả lời (giống hệt hành vi webhook).
  - Chỉ chạy trên page trong allowed_page_id_set.
  - Bỏ qua tin do chính page/bot gửi (last_sent_by.id == page_id) -> tránh tự trả lời mình.
  - orchestrator vẫn tự chặn khi đã đủ SĐT -> nhiều lớp bảo vệ.
  - poller_dry_run=True: chỉ log, không đẩy vào bot.

Bật kèm scheduler trong main.py. Không cần server public/webhook.
"""
from __future__ import annotations

import asyncio
import logging

from config import settings
from orchestrator import orchestrator
from pancake_client import pancake

log = logging.getLogger("poller")

# conv_id -> mốc last_customer_interactive_at đã xử lý (tránh trả lời lặp cùng 1 tin)
_seen: dict[str, str] = {}


async def _last_customer_text(conv_id: str, customer_id: str) -> str:
    """Đọc nội dung tin cuối cùng của KHÁCH trong hội thoại."""
    try:
        data = await pancake.get_messages(conv_id, customer_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Poller không đọc được tin %s: %s", conv_id, e)
        return ""
    page_id = str(pancake.page_id)
    for m in reversed(data.get("messages", [])):
        if str((m.get("from") or {}).get("id", "")) != page_id:
            return (m.get("message") or m.get("original_message") or "").strip()
    return ""


async def poll_once(first_run: bool = False) -> int:
    """Quét 1 vòng. Trả về số hội thoại được đẩy vào bot xử lý."""
    page_id = str(pancake.page_id)
    convs = await pancake.list_conversations_internal(0)
    dispatched = 0
    for c in convs:
        conv_id = c.get("id")
        if not conv_id:
            continue
        # Tin cuối phải là của KHÁCH (khác page_id) -> mới cần bot trả lời.
        if str((c.get("last_sent_by") or {}).get("id", "")) == page_id:
            continue
        ts = c.get("last_customer_interactive_at") or c.get("updated_at") or ""
        if _seen.get(conv_id) == ts:
            continue  # đã xử lý đúng tin này rồi
        if first_run:
            _seen[conv_id] = ts
            continue  # lần đầu chỉ ghi mốc, KHÔNG trả lời tin tồn đọng
        customer_id = (c.get("customers") or [{}])[0].get("id", "")
        text = await _last_customer_text(conv_id, customer_id)
        if not text:
            # KHÔNG đánh dấu _seen ở đây: API messages có thể lag vài giây so với API list
            # (list đã báo tin mới nhưng messages chưa trả về kịp) -> để vòng quét sau (5s) thử lại,
            # tránh bỏ sót vĩnh viễn hội thoại này (bug đã gặp thực tế: khách nhắn, bot im re mãi).
            continue
        _seen[conv_id] = ts
        log.info("Poller: tin mới ở %s: %s", conv_id, text[:60])
        if settings.poller_dry_run:
            continue
        await orchestrator.on_customer_message(conv_id, customer_id, text)
        dispatched += 1
    return dispatched


async def poller_loop() -> None:
    if not settings.poller_enabled:
        return
    if settings.pancake_page_id not in settings.allowed_page_id_set:
        log.warning("Poller: page %s không được phép -> tắt.", settings.pancake_page_id)
        return
    log.info("Poller bật: %ds/lần, dry_run=%s", settings.poller_interval_seconds, settings.poller_dry_run)
    first = True
    while True:
        try:
            n = await poll_once(first_run=first)
            if first:
                log.info("Poller: ghi mốc ban đầu %d hội thoại (không trả lời tin cũ).", len(_seen))
                first = False
            elif n:
                log.info("Poller: đã đẩy %d hội thoại vào bot.", n)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi poller: %s", e)
        await asyncio.sleep(settings.poller_interval_seconds)
