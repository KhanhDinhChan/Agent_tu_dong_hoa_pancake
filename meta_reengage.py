"""Tái chăm khách IM LẶNG cho bản webhook Meta (main_meta.py) — thêm 2026-10-02.

Lý do: so sánh 1 tháng dữ liệu thật (03/09-02/10) cho thấy page chính Vật Lý Siêu Nhẩm (nhân viên nhắn tay)
nhắn lại khách im lặng ở 18% hội thoại (nhóm này cho SĐT 36%), trong khi bản webhook KHÔNG có tái chăm nào
(reengage.py thuộc bản poller cũ, dùng API nội bộ Pancake với token 90 ngày — đã hết hạn).

Câu tái chăm là MẪU CỐ ĐỊNH lấy từ reengage_rephrase.md (không gọi AI -> không tốn tiền, không bịa):
  - Lần 1: hỏi lại ĐÚNG câu khách đang dừng nhưng diễn đạt khác (hỏi lớp / hình thức / tình hình học / SĐT).
  - Lần 2: xin SĐT luôn, bất kể đang dừng ở bước nào.

Chỉ tái chăm khi TẤT CẢ điều kiện đúng:
  - Hội thoại INBOX, khách CHƯA cho SĐT, tin cuối khách không phải từ chối.
  - Tin cuối cùng là của BOT (không phải nhân viên) và sau tin cuối của khách KHÔNG có nhân viên nào nhắn
    -> nhân viên đang phụ trách thì không chen vào.
  - Còn trong cửa sổ 24h Facebook cho phép nhắn chủ động (tính từ tin cuối của khách).
  - Trong khung giờ gửi (mặc định 7h-21h) và trong khung giờ hoạt động của page (nếu có).
  - Tổng số lần page xin SĐT trong hội thoại chưa tới 3 (đúng quy tắc kịch bản, tính cả luồng chính).
Khách trả lời lại -> mốc "tin cuối của khách" đổi -> đếm lại từ đầu (luồng trả lời chính lo phần tiếp theo).

Trạng thái lưu ra meta_reengage_{page_id}.json: {conv_id: {"cust_at": epoch, "attempts": n, "last_sent_at": epoch}}.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
import zlib

from meta_config import meta_settings
from meta_orchestrator import (
    BOT_TAG,
    MetaOrchestrator,
    PageContext,
    _parse_pancake_ts,
    customer_sent_zalo_contact,
    in_active_hours,
)

log = logging.getLogger("meta_reengage")

# ---- Câu mẫu (nguyên văn reengage_rephrase.md) ----
REPHRASE = {
    "grade": [
        "Dạ chị ơi, không biết bạn nhà mình năm nay đang học lớp mấy ạ, để em tư vấn đúng lớp giúp mình ạ?",
        "Dạ em chưa nắm được bạn nhà mình năm nay lên lớp mấy, chị cho em xin thông tin để em tư vấn phù hợp giúp mình nhé ạ?",
        "Chị cho em hỏi thêm bạn nhà mình hiện đang học lớp mấy ạ, để em gửi đúng thông tin lớp học cho mình ạ.",
    ],
    "format_multi": [
        "Dạ chị cho em hỏi thêm, bạn nhà mình tiện học trực tiếp tại 1 trong các cơ sở của trung tâm, hay học Online qua Zoom sẽ thuận tiện hơn ạ?",
        "Không biết mình đã quyết định cho bạn học trực tiếp tại cơ sở nào hay học Online chưa ạ, để em gửi đúng lịch học cho mình ạ?",
        "Chị ơi, bạn nhà mình hợp học Online hay học trực tiếp tại cơ sở hơn ạ? Em gửi lịch học phù hợp cho mình ạ.",
    ],
    "format_single": [
        "Dạ chị cho em hỏi thêm, bạn nhà mình hợp học trực tiếp tại cơ sở hay học Online qua Zoom hơn ạ?",
        "Không biết mình đã tiện chọn hình thức học trực tiếp hay Online cho bạn nhà mình chưa ạ?",
    ],
    "difficulty": [
        "Dạ chị cho em hỏi thêm, hiện tại bạn nhà mình học môn vật lý ở trường có chỗ nào chưa vững không ạ, để em tư vấn lộ trình phù hợp ạ?",
        "Không biết tình hình học vật lý trên trường của bạn nhà mình dạo này thế nào ạ, có cần hỗ trợ thêm phần nào không ạ?",
    ],
    "phone": [
        "Dạ chị cho em xin lại SĐT để trung tâm tiện gọi điện trao đổi kỹ hơn về lộ trình học cho bạn nhà mình ạ.",
        "Không biết chị để lại giúp em số điện thoại được không ạ, để trung tâm liên hệ tư vấn trực tiếp cho mình nhanh hơn ạ.",
    ],
}
SECOND_ATTEMPT = [
    "Dạ chị ơi, để tiện trao đổi nhanh hơn chị cho em xin SĐT, bên em sẽ gọi điện tư vấn trực tiếp giúp mình ạ.",
    "Dạ em thấy nhắn tin qua lại hơi bất tiện, chị cho em xin SĐT để trung tâm gọi điện tư vấn kỹ hơn cho bạn nhà mình ạ.",
]

MAX_PHONE_ASKS = 3  # tối đa 3 lần xin SĐT/hội thoại, tính cả luồng chính (chốt 2026-10-02)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)[\s.\-]?\d(?:[\s.\-]?\d){7,8}(?!\d)")
PHONE_ASK_RE = re.compile(r"xin\s*(?:lại\s*)?(?:sđt|sdt|số)|để lại giúp em (?:sđt|sdt|số)", re.I)
# Tin hệ thống Pancake/Facebook chèn vào hội thoại — không phải tin ai nhắn.
SYSTEM_MARKERS = ("Bạn đang phản hồi bình luận", "đã trả lời một quảng cáo", "Đã đặt giai đoạn")
# Khách đã từ chối/kết thúc -> không làm phiền thêm.
REFUSAL_RE = re.compile(
    r"không cần|ko cần|k cần|không có nhu cầu|chưa có nhu cầu|không quan tâm|thôi em|thôi nhé|thôi ạ|"
    r"đã có chỗ học|học chỗ khác|không học|ko học|đừng nhắn|không nhắn|xa quá|đắt quá",
    re.I,
)


def _text(m: dict) -> str:
    raw = m.get("original_message") or m.get("message") or ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()


def _role(m: dict, page_id: str) -> str:
    """'cust' | 'staff' | 'bot' | 'system'. Bot = tin page KHÔNG có admin_name (gồm cả tin chào tự động)."""
    frm = m.get("from") or {}
    if str(frm.get("id", "")) != page_id:
        return "cust"
    if any(s in _text(m) for s in SYSTEM_MARKERS):
        return "system"
    admin = frm.get("admin_name")
    if admin and admin != "Public API":
        return "staff"
    return "bot"


def classify_stall(text: str) -> str | None:
    """Câu bot đang chờ khách trả lời thuộc nhóm nào trong reengage_rephrase.md (None = không nhận ra)."""
    t = text.lower()
    if PHONE_ASK_RE.search(text):
        return "phone"
    if "khó khăn" in t or "chưa vững" in t:
        return "difficulty"
    # Hỏi lớp xét TRƯỚC hình thức: câu gộp "lớp mấy và học online hay trực tiếp" thì lớp mới là thứ còn thiếu.
    if "lớp mấy" in t:
        return "grade"
    if "cơ sở này hay" in t:
        return "format_single"
    if "cơ sở nào" in t or "online hay" in t or "hay học online" in t or "hay học trực tiếp" in t:
        return "format_multi"
    return None


def _pick(options: list[str], conv_id: str, already_sent: set[str]) -> str:
    """Chọn 1 câu chưa từng gửi trong hội thoại; thứ tự xoay theo conv_id để các khách nhận câu khác nhau."""
    start = zlib.crc32(conv_id.encode()) % len(options)
    rotated = options[start:] + options[:start]
    for o in rotated:
        if o not in already_sent:
            return o
    return rotated[0]


def decide(messages: list[dict], page_id: str, conv_id: str, state: dict | None, now: float,
           conv_has_phone: bool = False) -> tuple[str, int, str] | None:
    """Quyết định có tái chăm không. Trả (câu sẽ gửi, số lần tái chăm sau khi gửi, lý do) hoặc None.
    Hàm thuần — không gọi mạng, để kiểm thử được. `state` là trạng thái đã lưu của hội thoại này."""
    s = meta_settings
    msgs = []
    for m in messages:
        ts = _parse_pancake_ts(m.get("inserted_at", ""))
        if ts:
            msgs.append((ts.timestamp(), _role(m, page_id), _text(m), m))
    msgs.sort(key=lambda x: x[0])
    msgs = [x for x in msgs if x[1] != "system"]

    cust = [x for x in msgs if x[1] == "cust"]
    if not cust:
        return None
    if conv_has_phone or any(PHONE_RE.search(x[2]) or x[3].get("has_phone") for x in cust):
        return None
    if customer_sent_zalo_contact(messages, page_id):
        return None  # xin qua Zalo + gửi ảnh = đã để lại liên hệ, như cho SĐT
    cust_at, _, cust_text, _ = cust[-1]
    if REFUSAL_RE.search(cust_text):
        return None
    if now - cust_at > s.reengage_window_hours * 3600:
        return None  # quá cửa sổ 24h của Facebook — gửi sẽ bị từ chối

    after = [x for x in msgs if x[0] > cust_at]
    if any(x[1] == "staff" for x in after):
        return None  # nhân viên đang phụ trách
    bot_texts_after = [x for x in after if x[1] == "bot" and x[2]]
    if not bot_texts_after:
        return None  # bot chưa trả lời tin cuối của khách (luồng chính sẽ lo) -> không tái chăm

    attempts = 0
    last_sent_at = 0.0
    if state and abs(state.get("cust_at", 0) - cust_at) < 1:
        attempts = state.get("attempts", 0)
        last_sent_at = state.get("last_sent_at", 0.0)
    if attempts >= 2:
        return None

    page_texts = {x[2] for x in msgs if x[1] != "cust"}
    phone_asks = sum(1 for x in msgs if x[1] != "cust" and PHONE_ASK_RE.search(x[2]))

    if attempts == 0:
        last_bot_at = bot_texts_after[-1][0]
        if now - last_bot_at < s.reengage_first_after_hours * 3600:
            return None
        stall = classify_stall(bot_texts_after[-1][2])
        if stall and stall != "phone":
            return _pick(REPHRASE[stall], conv_id, page_texts), 1, f"lần 1 — hỏi lại bước '{stall}'"
        if phone_asks >= MAX_PHONE_ASKS:
            return None
        if stall == "phone":
            return _pick(REPHRASE["phone"], conv_id, page_texts), 1, "lần 1 — xin lại SĐT"
        # Không nhận ra bot đang chờ câu gì -> xin SĐT luôn và coi như đã hết lượt (không gửi lần 2).
        return _pick(SECOND_ATTEMPT, conv_id, page_texts), 2, "lần 1 — không rõ bước, xin SĐT"

    # attempts == 1
    if now - last_sent_at < s.reengage_second_after_hours * 3600:
        return None
    if phone_asks >= MAX_PHONE_ASKS:
        return None
    return _pick(SECOND_ATTEMPT, conv_id, page_texts), 2, "lần 2 — xin SĐT"


def _state_path(ctx: PageContext) -> str:
    return f"meta_reengage_{ctx.page_id}.json"


def _load_state(ctx: PageContext) -> dict:
    try:
        with open(_state_path(ctx), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(ctx: PageContext, state: dict) -> None:
    # Bỏ các hội thoại đã quá 3 ngày cho file khỏi phình.
    cutoff = time.time() - 3 * 86400
    state = {k: v for k, v in state.items() if v.get("cust_at", 0) > cutoff}
    with open(_state_path(ctx), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _in_send_hours() -> bool:
    return in_active_hours({"start": meta_settings.reengage_send_start, "end": meta_settings.reengage_send_end})


async def scan_page(orch: MetaOrchestrator, ctx: PageContext, dry_run: bool) -> list[dict]:
    """Quét 1 lượt cho 1 trang. Trả danh sách việc đã gửi/sẽ gửi (để log/kiểm tra)."""
    now = time.time()
    state = _load_state(ctx)
    done: list[dict] = []
    since = int(now - meta_settings.reengage_window_hours * 3600)
    convs: list[dict] = []
    for page_number in range(1, 21):
        batch = await ctx.pancake.list_conversations_public(since, int(now), page_number)
        if not batch:
            break
        convs.extend(batch)
        await asyncio.sleep(0.3)

    for c in convs:
        if c.get("type") != "INBOX" or c.get("has_phone"):
            continue
        last_by = c.get("last_sent_by") or {}
        if str(last_by.get("id", "")) != ctx.pancake.page_id or (
                last_by.get("admin_name") and last_by.get("admin_name") != "Public API"):
            continue  # tin cuối là của khách hoặc nhân viên -> không phải việc của tái chăm
        conv_id = c["id"]
        live = ctx.conversations.get(conv_id)
        if live and live.task and not live.task.done():
            continue  # luồng trả lời chính đang xử lý hội thoại này
        customer_id = (c.get("customers") or [{}])[0].get("id")
        if not customer_id:
            continue
        try:
            data = await ctx.pancake.get_messages(conv_id, customer_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] Tái chăm: lỗi đọc lịch sử %s: %s", ctx.name, conv_id, e)
            continue
        await asyncio.sleep(0.3)
        messages = data.get("messages", [])
        result = decide(messages, ctx.pancake.page_id, conv_id, state.get(conv_id), time.time(), bool(c.get("has_phone")))
        if not result:
            continue
        text, attempts, reason = result
        cust_at = max((_parse_pancake_ts(m.get("inserted_at", "")).timestamp() for m in messages
                       if _role(m, ctx.pancake.page_id) == "cust" and _parse_pancake_ts(m.get("inserted_at", ""))),
                      default=0)
        psid = conv_id.split("_", 1)[1]
        item = {"conv_id": conv_id, "reason": reason, "text": text}
        done.append(item)
        if dry_run:
            log.info("[%s] Tái chăm (CHẠY THỬ, không gửi) %s: %s -> %r", ctx.name, conv_id, reason, text)
            continue
        try:
            await ctx.meta.send_text(psid, text)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] Tái chăm: lỗi gửi %s: %s", ctx.name, conv_id, e)
            continue
        log.info("[%s] Tái chăm ĐÃ GỬI %s: %s -> %r", ctx.name, conv_id, reason, text)
        state[conv_id] = {"cust_at": cust_at, "attempts": attempts, "last_sent_at": time.time()}
        _save_state(ctx, state)
        await orch._apply_tag(ctx, conv_id, BOT_TAG)
    return done


async def reengage_loop(orch: MetaOrchestrator) -> None:
    """Chạy song song suốt đời tiến trình (main_meta.py khởi động)."""
    if not meta_settings.reengage_enabled:
        log.info("Tái chăm: đang TẮT (REENGAGE_ENABLED=false).")
        return
    log.info("Tái chăm: BẬT (%s) — lần 1 sau %.0fh im lặng, lần 2 sau thêm %.0fh, gửi %dh-%dh, quét mỗi %.0f phút.",
             "CHẠY THỬ, không gửi thật" if meta_settings.reengage_dry_run else "GỬI THẬT",
             meta_settings.reengage_first_after_hours, meta_settings.reengage_second_after_hours,
             meta_settings.reengage_send_start, meta_settings.reengage_send_end, meta_settings.reengage_scan_minutes)
    await asyncio.sleep(120)  # để bot khởi động xong hẳn
    while True:
        if _in_send_hours():
            for ctx in list(orch._pages.values()):
                if not in_active_hours(ctx.active_hours):
                    continue
                try:
                    items = await scan_page(orch, ctx, meta_settings.reengage_dry_run)
                    if items:
                        log.info("[%s] Tái chăm: lượt quét này %d hội thoại.", ctx.name, len(items))
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] Tái chăm: lỗi lượt quét: %s", ctx.name, e)
        await asyncio.sleep(meta_settings.reengage_scan_minutes * 60)
