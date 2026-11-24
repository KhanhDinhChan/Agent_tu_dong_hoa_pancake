"""
Tái chăm theo TAG "Bot tái chăm" — dành cho khách đã hẹn "trả lời sau"/"để hỏi bạn nhà mình"/"để suy nghĩ".

Khác với reengage.py (tái chăm xin SĐT theo giờ im lặng, tin nhắn cố định):
  - Chọn hội thoại qua TAG (AI tự gắn khi khách hẹn trả lời sau — xem ai_service.py), không theo ngưỡng
    giờ im lặng cố định.
  - Tin nhắn do AI TỰ SOẠN riêng từng hội thoại (nhắc lại thông tin đã tư vấn + hỏi thăm rõ ràng), không
    phải mẫu tĩnh.
  - CHỈ gửi trong KHUNG GIỜ VÀNG (mặc định 19h-21h) — lúc phụ huynh thường rảnh/hay xem điện thoại.
  - Đợi ít nhất N giờ (mặc định 24h) kể từ lần tái chăm trước (hoặc từ tin hẹn của khách), tối đa M lần
    (mặc định 2) rồi dừng hẳn, tránh làm phiền khách.

Trạng thái lưu cục bộ theo page (KHÔNG dùng tag Pancake để track, tránh xung đột với tag nghiệp vụ thật):
  tag_reengage_state_{page_id}.json -> {conv_id: {"attempts": int, "last_sent_at": iso, "done": bool}}
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re

from ai_service import ai, is_ai_message_safe
from config import settings
from pancake_client import has_human_reply, pancake

log = logging.getLogger("tag_reengage")

# Dùng khi AI soạn tin bị chặn vì nghi bịa số liệu (xem is_ai_message_safe trong ai_service.py) — đã gặp
# thực tế AI tự soạn tin tái chăm SĐT bịa học phí + cam kết hoàn tiền (ca Mai Tran, 2026-08-04).
TAG_REENGAGE_FALLBACK = "Dạ em hỏi thăm chị đã trao đổi/suy nghĩ xong chưa ạ? Nếu cần trung tâm hỗ trợ thêm thông tin gì thì chị cứ nhắn cho em nhé ạ."


def _state_path() -> str:
    return f"tag_reengage_state_{settings.pancake_page_id}.json"


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def in_golden_hour(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now()
    return settings.tag_reengage_golden_start <= now.hour < settings.tag_reengage_golden_end


async def _find_tag_id(tag_name: str) -> int | None:
    for tag in await pancake.get_tags():
        if tag.get("text", "").strip() == tag_name:
            return tag.get("id")
    return None


async def find_candidates(tag_id: int) -> list[dict]:
    """Quét hội thoại có tag 'Bot tái chăm', chỉ hội thoại inbox (bỏ bình luận), chưa có SĐT.

    Loại trừ hội thoại đang gắn 'Bot không rep' — AI đã tự đánh giá câu hỏi của khách NGOÀI phạm vi kịch
    bản/dữ liệu, cần NHÂN VIÊN THẬT xử lý, KHÔNG được tự động nhắn thêm cho tới khi tag đó được gỡ."""
    no_reply_tag_id = await _find_tag_id("Bot không rep")
    candidates: dict[str, dict] = {}
    cc = 0
    while True:
        try:
            convs = await pancake.list_conversations_internal(cc)
        except Exception as e:  # noqa: BLE001
            log.warning("  Lỗi phân trang (cc=%d): %s, đợi 10s rồi thử lại.", cc, e)
            await asyncio.sleep(10)
            continue
        if not convs:
            break
        for c in convs:
            cid = c.get("id") or ""
            if not cid.startswith(f"{pancake.page_id}_"):
                continue  # bình luận trên bài viết -> KHÔNG tái chăm
            if tag_id not in (c.get("tags") or []):
                continue
            if c.get("has_phone"):
                continue  # đã có SĐT -> nhân viên xử lý tiếp, không tự động nhắn nữa
            if no_reply_tag_id is not None and no_reply_tag_id in (c.get("tags") or []):
                continue  # đang gắn 'Bot không rep' -> chờ nhân viên xử lý, KHÔNG tự động nhắn thêm
            candidates[cid] = c
        cc += len(convs)
        await asyncio.sleep(0.4)
        if cc > 6000:
            break
    return list(candidates.values())


def _last_customer_ts_local(messages: list[dict], page_id: str) -> datetime.datetime | None:
    """Thời điểm tin cuối của KHÁCH (không phải bot), quy đổi sang giờ VN từ UTC Pancake trả về."""
    for m in reversed(messages):
        if str((m.get("from") or {}).get("id", "")) != page_id:
            ts = m.get("inserted_at", "")
            try:
                utc = datetime.datetime.fromisoformat(ts.replace("Z", ""))
            except ValueError:
                return None
            return utc + datetime.timedelta(hours=settings.tag_reengage_tz_offset_hours)
    return None


def _build_plain_history(messages: list[dict], page_id: str) -> list[dict]:
    history: list[dict] = []
    for m in messages:
        sender = str((m.get("from") or {}).get("id", ""))
        role = "assistant" if sender == page_id else "user"
        raw = m.get("message") or m.get("original_message") or ""
        content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
        if content:
            history.append({"role": role, "content": content})
    return history


async def tag_reengage_scan() -> dict:
    if not settings.tag_reengage_enabled:
        return {"status": "disabled"}
    if settings.pancake_page_id not in settings.allowed_page_id_set:
        return {"status": "page_not_allowed", "page": settings.pancake_page_id}

    now = datetime.datetime.now()
    if not in_golden_hour(now):
        log.info("Tái chăm tag: ngoài khung giờ vàng %dh-%dh, đợi tới đợt sau.",
                  settings.tag_reengage_golden_start, settings.tag_reengage_golden_end)
        return {"status": "outside_window", "hour": now.hour}

    tag_id = await _find_tag_id(settings.tag_reengage_tag_name)
    if tag_id is None:
        log.warning("Tái chăm tag: không tìm thấy tag '%s' trên Pancake — kiểm tra lại tên tag.",
                    settings.tag_reengage_tag_name)
        return {"status": "tag_not_found"}

    state = _load_state()
    candidates = await find_candidates(tag_id)
    log.info("Tái chăm tag: %d hội thoại đang gắn tag '%s'.", len(candidates), settings.tag_reengage_tag_name)

    sent = 0
    skipped_window = 0
    for c in candidates:
        conv_id = c["id"]
        entry = state.get(conv_id) or {"attempts": 0, "last_sent_at": None, "done": False}
        if entry.get("done"):
            continue
        if entry["attempts"] >= settings.tag_reengage_max_attempts:
            entry["done"] = True
            state[conv_id] = entry
            continue

        customer_id = (c.get("customers") or [{}])[0].get("id", "")
        try:
            msgs = (await pancake.get_messages(conv_id, customer_id)).get("messages", [])
        except Exception as e:  # noqa: BLE001
            log.warning("  Không đọc được tin %s: %s", conv_id, e)
            continue
        await asyncio.sleep(0.4)

        if has_human_reply(msgs, pancake.page_id):
            log.info("  Bỏ qua tái chăm tag %s: nhân viên đã nhắn tay -> dừng tự động.", conv_id)
            entry["done"] = True
            state[conv_id] = entry
            continue

        last_customer_ts = _last_customer_ts_local(msgs, pancake.page_id)
        last_sent_at = entry.get("last_sent_at")

        if last_sent_at:
            last_sent_dt = datetime.datetime.fromisoformat(last_sent_at)
            # Khách đã trả lời SAU lần tái chăm trước -> coi như thành công, dừng tự động (để luồng
            # tư vấn bình thường của orchestrator xử lý tiếp).
            if last_customer_ts and last_customer_ts > last_sent_dt:
                log.info("  Khách %s đã phản hồi sau lần tái chăm trước -> dừng tự động.", conv_id)
                entry["done"] = True
                state[conv_id] = entry
                continue
            hours_since = (now - last_sent_dt).total_seconds() / 3600.0
            if hours_since < settings.tag_reengage_wait_hours:
                continue
        else:
            # Lần đầu: đợi ít nhất N giờ kể từ tin cuối của khách (thời điểm khách hẹn trả lời sau).
            if not last_customer_ts:
                continue
            age_hours = (now - last_customer_ts).total_seconds() / 3600.0
            if age_hours < settings.tag_reengage_wait_hours:
                continue

        # CHỐT CHẶN AN TOÀN CHÍNH SÁCH FACEBOOK: xem ghi chú tương tự trong reengage.py — không gửi nếu đã
        # quá lâu kể từ tin cuối của khách (ngoài khung 24h Messenger cho phép gửi tự do, không tag).
        # LƯU Ý: với tag_reengage_wait_hours mặc định (24h), điều kiện "đủ giờ để gửi" ở trên đã tự nhiên vượt
        # ngưỡng an toàn này -> tính năng gần như sẽ KHÔNG gửi được lần đầu nào nữa trừ khi giảm wait_hours
        # xuống dưới reengage_facebook_safe_window_hours.
        if last_customer_ts:
            hours_since_customer = (now - last_customer_ts).total_seconds() / 3600.0
            if hours_since_customer > settings.reengage_facebook_safe_window_hours:
                log.warning(
                    "  Bỏ qua tái chăm tag %s: đã %.1fh kể từ tin cuối của khách (>%.1fh) -> ngoài khung an "
                    "toàn 24h của Facebook, KHÔNG gửi để tránh vi phạm chính sách.",
                    conv_id, hours_since_customer, settings.reengage_facebook_safe_window_hours,
                )
                skipped_window += 1
                continue

        history = _build_plain_history(msgs, pancake.page_id)
        try:
            message = await ai.generate_tag_reengage_message(history)
        except Exception as e:  # noqa: BLE001
            log.warning("  Lỗi soạn tin tái chăm tag %s: %s", conv_id, e)
            continue
        if not message:
            continue
        if not is_ai_message_safe(message):
            log.warning("  AI soạn tin tái chăm tag %s có dấu hiệu bịa số liệu -> dùng mẫu an toàn thay thế. Nội dung bị chặn: %s",
                        conv_id, message[:200])
            message = TAG_REENGAGE_FALLBACK

        if settings.tag_reengage_dry_run:
            log.info("  [DRY-RUN] sẽ tái chăm tag %s (lần %d): %s", conv_id, entry["attempts"] + 1, message[:100])
            continue  # KHÔNG cập nhật trạng thái khi dry-run -> tránh "dùng hết lượt" trước khi gửi thật

        try:
            await pancake.send_text(conv_id, message)
            await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            log.warning("  Lỗi gửi tái chăm tag %s: %s", conv_id, e)
            continue

        entry["attempts"] += 1
        entry["last_sent_at"] = now.isoformat()
        if entry["attempts"] >= settings.tag_reengage_max_attempts:
            entry["done"] = True
        state[conv_id] = entry
        sent += 1

    _save_state(state)
    return {
        "status": "ok",
        "candidates": len(candidates),
        "sent": sent,
        "skipped_window": skipped_window,
        "dry_run": settings.tag_reengage_dry_run,
    }


async def scheduler_loop() -> None:
    """Vòng lặp quét định kỳ (dùng trong main.py) — đợi hết 1 chu kỳ rồi mới quét lần đầu, giống reengage."""
    while True:
        await asyncio.sleep(settings.tag_reengage_scan_minutes * 60)
        try:
            result = await tag_reengage_scan()
            log.info("Tái chăm tag scan: %s", result)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi scheduler tái chăm tag: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(asyncio.run(tag_reengage_scan()))
