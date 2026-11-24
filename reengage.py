"""
Tái chăm (re-engage): hội thoại im lặng quá REENGAGE_AFTER_HOURS giờ mà CHƯA có SĐT
-> bot CHỦ ĐỘNG viết tiếp ĐÚNG BƯỚC KẾ TIẾP của quy trình sale (dùng lại chính luồng AI chính, xem
ai_service.generate_reengage_continue) — KHÔNG nhảy cóc thẳng sang "xin SĐT" nếu hội thoại thực tế chưa đi
hết các bước tư vấn ở giữa (vd khách mới chào rồi im lặng thì bước tiếp theo là hỏi hình thức/gửi lịch học,
không phải xin SĐT ngay). Nếu lần 1 khách vẫn không phản hồi, đợi thêm REENGAGE_RETRY_WAIT_HOURS giờ rồi thử
LẦN 2 bằng đúng cơ chế này (đọc lại lịch sử đã có thêm tin lần 1) — tối đa REENGAGE_MAX_ATTEMPTS lần rồi dừng
hẳn. Vì dùng chung SYSTEM_PROMPT với luồng chính nên tự động thừa hưởng toàn bộ ảnh lịch học/học phí, gắn tag,
và quy tắc "chỉ xin SĐT đúng 1 lần" đã có.

Quy tắc:
  - Chỉ gửi trong khung giờ [reengage_active_start .. reengage_active_end) (mặc định 6h..21h).
    Ngoài khung này thì KHÔNG gửi (đợi tới 6h sáng lần quét sau).
  - Bỏ qua hội thoại đã có SĐT (has_phone) hoặc nhân viên đã nhắn tay (has_human_reply).
  - Tính "im lặng bao lâu" dựa vào last_customer_interactive_at (thời điểm KHÁCH nhắn cuối) — KHÔNG dùng
    updated_at vì field đó bị cập nhật mỗi khi BOT tự gửi tin hoặc có sự kiện hệ thống (ad-click...),
    không phản ánh đúng khách đã im lặng bao lâu.
  - Nếu khách đã trả lời SAU lần tái chăm gần nhất -> coi như thành công, dừng tự động (để orchestrator
    xử lý tiếp bình thường), không gửi thêm.
  - reengage_dry_run=True (mặc định): chỉ LOG các hội thoại sẽ gửi, KHÔNG gửi thật.
  - Chỉ chạy trên page được phép (allowed_page_id_set) — an toàn.

Trạng thái lưu cục bộ (KHÔNG dùng tag Pancake), riêng theo từng page:
  reengage_sent_{page_id}.json -> {conv_id: {"attempts": int, "last_sent_at": iso}}
  (Tự nâng cấp từ định dạng cũ — list các conv_id đã gửi 1 lần — sang định dạng dict ở trên.)

Chạy thủ công 1 lần:  python reengage.py
Backfill (quét lại toàn bộ lịch sử, bỏ qua giới hạn 72h, chỉ áp dụng 1 lần):
  python reengage.py --backfill
Hoặc để scheduler trong main.py tự quét định kỳ.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import sys

import httpx

from ai_service import SCHEDULE_IMAGE_MAP, ai, has_error_leak, is_ai_message_safe
from config import settings
from orchestrator import inject_info_bundle, inject_schedule_image, orchestrator, parse_reply, resolve_schedule_url
from pancake_client import has_human_reply, pancake

log = logging.getLogger("reengage")

# Dùng khi AI soạn tin tái chăm bị chặn vì nghi bịa số liệu/rò rỉ lỗi (xem is_ai_message_safe/has_error_leak)
# — mẫu tĩnh an toàn tuyệt đối, không có số liệu nào, dùng được cho bất kỳ lượt tái chăm nào.
REENGAGE_SAFE_FALLBACK = "Chị cho em xin SĐT em tiện trao đổi về thông tin lớp và nắm bắt tình hình học của con để có hướng hỗ trợ cho bạn ạ"

# Tin dẫn link website thay thế khi ĐÃ xin SĐT 1 lần mà khách chưa cho — PHẢI khớp đúng nguyên văn với quy
# tắc trong system_prompt_template.md (mục "CHỈ CẦN ĐÚNG 1 SỐ ĐIỆN THOẠI").
WEBSITE_LINK_MESSAGE = "Em gửi chị tham khảo thông tin các lớp học bên em ạ\nhttps://www.vatlysieunham.edu.vn"

# QUAN TRỌNG: test thực tế cho thấy AI đôi khi vẫn lặp lại câu xin SĐT dù đã được dặn kỹ trong prompt (mất
# tác dụng khi không có lượt "user" mới để bám vào) -> KHÔNG dựa vào AI tự nhận biết "đã xin SĐT chưa", mà
# xác định bằng CODE (đáng tin cậy hơn) trước khi gọi AI, giống cách is_ai_message_safe/has_error_leak đã
# làm cho các trường hợp khác trong hệ thống này ("trust but verify").
_PHONE_ASK_DETECT_RE = re.compile(r"sđt|số điện thoại", re.IGNORECASE)
_WEBSITE_LINK_DETECT = "vatlysieunham.edu.vn"


def _bot_already_asked_phone(history: list[dict]) -> bool:
    return any(m["role"] == "assistant" and _PHONE_ASK_DETECT_RE.search(m["content"]) for m in history)


# Tương tự lý do trên: KHÔNG dựa vào AI tự nhận biết "khách chưa trả lời câu hỏi tình hình học tập ở bước 3
# nên bỏ bước 4 (nhận xét), xin SĐT ngay" — test thực tế cho thấy AI vẫn lặp lại y hệt tin gửi lịch học +
# hỏi tình hình dù đã dặn rõ trong prompt. Xác định bằng CODE: nếu ảnh lịch học ĐÃ được gửi trong hội thoại
# mà KHÔNG có tin nào của khách sau đó -> khách chưa trả lời câu hỏi tình hình -> áp dụng ngoại lệ.
def _schedule_sent_without_reply(messages: list[dict], page_id: str) -> bool:
    schedule_urls = set(SCHEDULE_IMAGE_MAP.values())
    sent_idx = None
    for i, m in enumerate(messages):
        if str((m.get("from") or {}).get("id", "")) != page_id:
            continue
        for att in m.get("attachments") or []:
            if att.get("url") in schedule_urls:
                sent_idx = i
    if sent_idx is None:
        return False
    return all(str((m.get("from") or {}).get("id", "")) == page_id for m in messages[sent_idx + 1:])


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_exact_repeat(reply: str, history: list[dict]) -> bool:
    """True nếu "reply" AI vừa soạn TRÙNG (gần như) nguyên văn 1 tin bot đã từng gửi trong lịch sử — đã gặp
    thực tế: AI lặp lại y hệt câu hỏi cũ (vd "không biết con lên lớp mấy ạ?") khi soạn tin tái chăm, dù prompt
    đã dặn không lặp. Không dựa vào AI tự tránh lặp (không đáng tin cậy ở lượt "chủ động" này) — kiểm tra
    bằng code trước khi gửi."""
    norm_reply = _normalize(reply)
    if not norm_reply:
        return False
    return any(m["role"] == "assistant" and _normalize(m["content"]) == norm_reply for m in history)


def _bot_already_sent_website_link(history: list[dict]) -> bool:
    return any(m["role"] == "assistant" and _WEBSITE_LINK_DETECT in m["content"] for m in history)


def _sent_store_path() -> str:
    """File lưu cục bộ trạng thái tái chăm (số lần đã gửi + lần cuối), riêng theo từng page."""
    return f"reengage_sent_{settings.pancake_page_id}.json"


def _load_state() -> dict:
    path = _sent_store_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(raw, list):
        # Định dạng cũ: list conv_id đã gửi đúng 1 lần. Nâng cấp lên dict, coi như vừa gửi (last_sent_at=now)
        # để bắt buộc chờ đủ reengage_retry_wait_hours trước khi thử lần 2, tránh dội tin ngay khi nâng cấp.
        now_iso = datetime.datetime.now().isoformat()
        return {cid: {"attempts": 1, "last_sent_at": now_iso} for cid in raw}
    return raw


def _save_state(state: dict) -> None:
    with open(_sent_store_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def in_active_window(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now()
    return settings.reengage_active_start <= now.hour < settings.reengage_active_end


def _parse_pancake_ts(raw: str) -> datetime.datetime | None:
    """Parse timestamp Pancake (UTC) -> giờ VN (đã bù offset)."""
    if not raw:
        return None
    try:
        ts = datetime.datetime.fromisoformat(raw.replace("Z", ""))
    except ValueError:
        return None
    return ts + datetime.timedelta(hours=settings.reengage_tz_offset_hours)


def _customer_silence_hours(c: dict, now: datetime.datetime) -> float | None:
    """Số giờ khách đã im lặng, dựa vào last_customer_interactive_at (KHÔNG dùng updated_at — field đó
    bị bot/tự động của Pancake cập nhật, không phản ánh đúng lần khách nhắn cuối)."""
    raw = c.get("last_customer_interactive_at") or c.get("updated_at") or ""
    ts = _parse_pancake_ts(raw)
    if ts is None:
        return None
    return (now - ts).total_seconds() / 3600.0


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


def _collect_sent_image_urls(messages: list[dict], page_id: str) -> set[str]:
    """Tập URL ảnh BOT đã từng gửi trong hội thoại (để inject_schedule_image/inject_info_bundle không gửi
    trùng) — y hệt logic trong orchestrator._build_history."""
    urls: set[str] = set()
    for m in messages:
        sender = str((m.get("from") or {}).get("id", ""))
        if sender != page_id:
            continue
        for att in m.get("attachments") or []:
            url = att.get("url")
            if url:
                urls.add(url)
    return urls


async def _find_tag_id(tag_name: str) -> int | None:
    for tag in await pancake.get_tags():
        if tag.get("text", "").strip() == tag_name:
            return tag.get("id")
    return None


async def find_candidates(
    now: datetime.datetime, state: dict, max_age_hours: float | None = None
) -> tuple[list[dict], int]:
    """Quét hội thoại, chọn hội thoại đủ điều kiện tái chăm (lần 1 hoặc lần 2).

    LƯU Ý: danh sách từ list_conversations_internal KHÔNG sắp xếp chặt theo updated_at (đã kiểm chứng thực
    tế: có hội thoại cũ hơn xen giữa các hội thoại mới) -> KHÔNG được dừng sớm khi gặp 1 hội thoại "quá cũ"
    như code cũ (gây bỏ sót ứng viên nằm sau đó). Quét đủ số trang cố định (chặn bởi cc>6000) rồi lọc.

    Phân trang qua current_count có thể trả TRÙNG 1 hội thoại ở 2 trang liên tiếp nếu danh sách bị xáo thứ
    tự giữa các lần gọi -> luôn loại trùng theo id trước khi trả về.
    """
    age_limit = settings.reengage_max_age_hours if max_age_hours is None else max_age_hours
    # "Bot không rep" = AI đã tự đánh giá câu hỏi của khách NGOÀI phạm vi kịch bản/dữ liệu và chủ động im
    # lặng, cần NHÂN VIÊN THẬT xử lý — tuyệt đối KHÔNG được tự động nhắn thêm gì (kể cả tái chăm) cho tới khi
    # tag này được gỡ (nhân viên đã xử lý xong).
    no_reply_tag_id = await _find_tag_id("Bot không rep")
    candidates: dict[str, dict] = {}
    scanned = 0
    cc = 0
    while True:
        try:
            convs = await pancake.list_conversations_internal(cc)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                log.warning("  429 khi phân trang (cc=%d), đợi 10s rồi thử lại.", cc)
                await asyncio.sleep(10)
                continue
            raise
        if not convs:
            break
        for c in convs:
            scanned += 1
            cid = c.get("id") or ""
            if not cid.startswith(f"{pancake.page_id}_"):
                continue  # bình luận (comment) trên bài viết -> KHÔNG tái chăm, chỉ hội thoại inbox
            if c.get("has_phone"):
                continue  # đã có SĐT -> không cần xin nữa
            if no_reply_tag_id is not None and no_reply_tag_id in (c.get("tags") or []):
                continue  # đang gắn 'Bot không rep' -> chờ nhân viên xử lý, KHÔNG tự động nhắn thêm
            silence_hours = _customer_silence_hours(c, now)
            if silence_hours is None or silence_hours > age_limit:
                continue
            entry = state.get(cid, {"attempts": 0, "last_sent_at": None})
            attempts = entry.get("attempts", 0)
            if attempts >= settings.reengage_max_attempts:
                continue  # đã hết lượt tái chăm cho hội thoại này
            if attempts == 0:
                if silence_hours < settings.reengage_after_hours:
                    continue  # còn quá mới, chưa tới lúc tái chăm lần đầu
            else:
                last_sent_at = entry.get("last_sent_at")
                if not last_sent_at:
                    continue
                last_sent_dt = datetime.datetime.fromisoformat(last_sent_at)
                hours_since_sent = (now - last_sent_dt).total_seconds() / 3600.0
                if hours_since_sent < settings.reengage_retry_wait_hours:
                    continue  # chưa đủ thời gian chờ giữa 2 lần
            candidates[cid] = c  # dict theo id -> tự loại trùng nếu trang sau lặp lại
        cc += len(convs)
        await asyncio.sleep(0.4)  # tránh 429: quét dồn dập không nghỉ từng gây lỗi liên tục nhiều giờ
        if cc > 6000:
            break
    return list(candidates.values()), scanned


async def reengage_scan(max_age_hours: float | None = None) -> dict:
    if not settings.reengage_enabled:
        return {"status": "disabled"}

    # An toàn: chỉ chạy trên page được phép
    if settings.pancake_page_id not in settings.allowed_page_id_set:
        return {"status": "page_not_allowed", "page": settings.pancake_page_id}

    now = datetime.datetime.now()
    if not in_active_window(now):
        log.info("Tái chăm: ngoài khung %dh-%dh, đợi tới sáng.", settings.reengage_active_start, settings.reengage_active_end)
        return {"status": "outside_window", "hour": now.hour}

    state = _load_state()
    candidates, scanned = await find_candidates(now, state, max_age_hours)
    log.info("Tái chăm: quét %d hội thoại, %d ứng viên. dry_run=%s", scanned, len(candidates), settings.reengage_dry_run)

    sent = 0
    skipped_window = 0
    for c in candidates:
        conv_id = c["id"]
        entry = state.get(conv_id, {"attempts": 0, "last_sent_at": None})
        attempts = entry.get("attempts", 0)

        customer_id = (c.get("customers") or [{}])[0].get("id", "")
        try:
            msgs = (await pancake.get_messages(conv_id, customer_id)).get("messages", [])
        except Exception as e:  # noqa: BLE001
            log.warning("  Không đọc được tin %s: %s", conv_id, e)
            continue
        await asyncio.sleep(0.3)

        # CHỐT CHẶN: nhân viên đã nhắn tay trong hội thoại này -> KHÔNG tái chăm tự động nữa.
        if has_human_reply(msgs, pancake.page_id):
            log.info("  Bỏ qua tái chăm %s: nhân viên đã nhắn tay.", conv_id)
            continue

        last_customer_ts = None
        for m in reversed(msgs):
            if str((m.get("from") or {}).get("id", "")) != pancake.page_id:
                last_customer_ts = _parse_pancake_ts(m.get("inserted_at", ""))
                break

        # Nếu đã từng gửi (attempts>=1) mà khách đã trả lời SAU lần gửi trước -> thành công, không gửi nữa.
        if attempts >= 1 and entry.get("last_sent_at"):
            last_sent_dt = datetime.datetime.fromisoformat(entry["last_sent_at"])
            if last_customer_ts and last_customer_ts > last_sent_dt:
                log.info("  Khách %s đã phản hồi sau lần tái chăm trước -> dừng tự động.", conv_id)
                continue

        # CHỐT CHẶN AN TOÀN CHÍNH SÁCH FACEBOOK: đọc lại NGAY TRƯỚC khi gửi (không dùng field cache) — nếu
        # đã quá lâu kể từ tin nhắn cuối của khách (ngoài khung 24h Messenger cho phép gửi tự do, không tag)
        # -> KHÔNG gửi, tránh bị Meta hạn chế quyền nhắn tin của Page.
        if last_customer_ts:
            hours_since_customer = (now - last_customer_ts).total_seconds() / 3600.0
            if hours_since_customer > settings.reengage_facebook_safe_window_hours:
                log.warning(
                    "  Bỏ qua tái chăm %s: đã %.1fh kể từ tin cuối của khách (>%.1fh) -> ngoài khung an toàn "
                    "24h của Facebook, KHÔNG gửi để tránh vi phạm chính sách.",
                    conv_id, hours_since_customer, settings.reengage_facebook_safe_window_hours,
                )
                skipped_window += 1
                continue

        history = _build_plain_history(msgs, pancake.page_id)

        # CHỐT CHẶN "ĐÃ XIN SĐT CHƯA" BẰNG CODE — không dựa vào AI tự nhận biết (test thực tế cho thấy AI vẫn
        # có thể lặp lại câu xin SĐT dù đã dặn kỹ trong prompt, do lượt này không có tin "user" mới để bám
        # vào). Xác định trước bằng regex trên chính lịch sử, đáng tin cậy hơn nhiều.
        if _bot_already_asked_phone(history):
            if _bot_already_sent_website_link(history):
                # Đã xin SĐT + đã gửi link web thay thế trước đó -> hết nội dung để nói thêm, dừng hẳn.
                log.info("  Tái chăm %s: đã xin SĐT + đã gửi link web trước đó -> không còn gì để gửi thêm.", conv_id)
                if not settings.reengage_dry_run:
                    state[conv_id] = {"attempts": attempts + 1, "last_sent_at": now.isoformat()}
                    _save_state(state)
                continue
            reply = WEBSITE_LINK_MESSAGE
            known_grade = ""
            known_format = ""
            result_tags: list[str] = []
        elif _schedule_sent_without_reply(msgs, pancake.page_id):
            # Đã gửi lịch học + hỏi tình hình học tập (bước 3) mà khách CHƯA trả lời -> bỏ qua bước 4 (nhận
            # xét, cần câu trả lời của khách), xin SĐT ngay ở lượt này.
            reply = REENGAGE_SAFE_FALLBACK
            known_grade = ""
            known_format = ""
            result_tags = []
        else:
            try:
                result = await ai.generate_reengage_continue(history)
            except Exception as e:  # noqa: BLE001
                log.warning("  Lỗi soạn tin tái chăm %s: %s", conv_id, e)
                continue

            known_grade = result.get("known_grade", "")
            known_format = result.get("known_format", "")
            result_tags = result.get("tags", [])

            reply = (result.get("reply") or "").strip()
            if reply and has_error_leak(reply):
                log.warning("  Tái chăm %s: reply có dấu hiệu rò rỉ lỗi kỹ thuật -> không gửi.", conv_id)
                reply = ""
            if reply and not is_ai_message_safe(reply):
                log.warning("  AI soạn tin tái chăm %s có dấu hiệu bịa số liệu -> dùng mẫu an toàn thay thế. Nội dung bị chặn: %s",
                            conv_id, reply[:200])
                reply = REENGAGE_SAFE_FALLBACK
            if reply and _is_exact_repeat(reply, history):
                log.warning("  Tái chăm %s: AI lặp lại nguyên văn tin đã gửi trước đó -> không gửi. Nội dung bị chặn: %s",
                            conv_id, reply[:200])
                reply = ""

            if not reply:
                # AI chọn im lặng đúng kịch bản (vd khách đã từ chối cho SĐT trước đó trong lịch sử) -> không
                # gửi gì, nhưng vẫn tính là đã xử lý lượt này để không quét lại ngay lần sau.
                if settings.reengage_dry_run:
                    log.info("  [DRY-RUN] tái chăm %s (lần %d): AI chọn im lặng (đúng kịch bản), không gửi gì.",
                             conv_id, attempts + 1)
                    continue
                state[conv_id] = {"attempts": attempts + 1, "last_sent_at": now.isoformat()}
                _save_state(state)
                continue

        actions = parse_reply(reply)
        sent_image_urls = _collect_sent_image_urls(msgs, pancake.page_id)
        schedule_url = resolve_schedule_url(known_grade, known_format)
        actions = inject_schedule_image(actions, schedule_url, sent_image_urls)
        actions = inject_info_bundle(actions, known_grade, known_format, sent_image_urls)

        if settings.reengage_dry_run:
            preview = " | ".join(f"{kind}:{payload if isinstance(payload, str) else len(payload)}" for kind, payload in actions)
            log.info("  [DRY-RUN] sẽ tái chăm %s (lần %d): %s", conv_id, attempts + 1, preview[:200])
            continue

        try:
            for kind, payload in actions:
                if kind == "text":
                    await pancake.send_text(conv_id, payload)
                else:
                    await pancake.send_image(conv_id, payload)
                await asyncio.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            log.warning("  Lỗi tái chăm %s: %s", conv_id, e)
            continue

        for tag_name in result_tags:
            await orchestrator._apply_tag(conv_id, tag_name)

        state[conv_id] = {"attempts": attempts + 1, "last_sent_at": now.isoformat()}
        _save_state(state)
        sent += 1
        await asyncio.sleep(1.0)  # rate limit

    return {
        "status": "ok",
        "scanned": scanned,
        "candidates": len(candidates),
        "sent": sent,
        "skipped_window": skipped_window,
        "dry_run": settings.reengage_dry_run,
    }


async def scheduler_loop() -> None:
    """Vòng lặp quét tái chăm định kỳ (dùng trong main.py).

    KHÔNG quét ngay lúc bật bot — đợi đủ 1 chu kỳ (reengage_scan_minutes) rồi mới quét lần đầu,
    tránh gửi hàng loạt tin tái chăm ngay khi khởi động/restart bot.
    """
    while True:
        await asyncio.sleep(settings.reengage_scan_minutes * 60)
        try:
            result = await reengage_scan()
            log.info("Tái chăm scan: %s", result)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi scheduler tái chăm: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backfill = "--backfill" in sys.argv
    age = 24 * 365 if backfill else None  # backfill: bỏ qua giới hạn 72h, quét lại từ lúc bật bot
    print(asyncio.run(reengage_scan(max_age_hours=age)))
