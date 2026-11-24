"""
Điều phối luồng xử lý một tin nhắn khách hàng:

  webhook -> debounce (gộp tin gõ liên tiếp) -> AI -> delay + throttle
          -> tách [BREAK] + ảnh -> gửi nhiều tin qua Pancake -> gắn tag -> handoff

Kịch bản trả về reply có thể chứa [BREAK] và URL ảnh. Ta tách thành các "hành động"
(text / ảnh) và gửi lần lượt, mỗi tin có độ trễ tự nhiên riêng.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ai_service import KNOWN_IMAGE_URLS, SCHEDULE_IMAGE_MAP, ai, has_error_leak, info_bundle_urls
from config import settings
from pancake_client import has_human_reply, pancake
from rate_controller import human_delay, rate_limiter

log = logging.getLogger("orchestrator")

# Tag gắn vào MỌI hội thoại bot đã thực sự trả lời (khác "Bot không rep" — tag đó dành riêng cho lượt AI
# CHỦ ĐỘNG im lặng vì câu hỏi ngoài phạm vi kịch bản) — giúp nhân viên nhìn Pancake biết ngay hội thoại nào
# đã có bot xử lý. Gắn thêm, KHÔNG thay thế các tag khác AI tự chọn.
BOT_TAG = "Bot"

URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+\.(?:jpg|jpeg|png|gif|webp)", re.IGNORECASE)

# Nhận diện SĐT Việt Nam (0xxxxxxxxx hoặc +84/84xxxxxxxxx), cho phép cách nhau bởi space/./-
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?84|0)\d{9}(?!\d)")


def contains_phone(text: str) -> bool:
    cleaned = re.sub(r"[\s.\-()]+", "", text or "")
    return bool(_PHONE_RE.search(cleaned))


def history_has_customer_phone(history: list[dict]) -> bool:
    """True nếu trong lịch sử có tin của KHÁCH chứa SĐT (bỏ qua tin bot -> tránh nhầm hotline)."""
    return any(m.get("role") == "user" and contains_phone(m.get("content", "")) for m in history)


def customer_phones(history: list[dict]) -> set[str]:
    """Tập các SĐT (chuẩn hóa) khách đã cung cấp trong lịch sử."""
    phones: set[str] = set()
    for m in history:
        if m.get("role") == "user":
            cleaned = re.sub(r"[\s.\-()]+", "", m.get("content", ""))
            phones.update(_PHONE_RE.findall(cleaned))
    return phones


def parse_reply(reply: str) -> list[tuple[str, object]]:
    """
    Chuyển reply -> danh sách hành động theo thứ tự:
      ("text", "nội dung")  hoặc  ("image", ["url1", "url2"])
    - Tách theo [BREAK].
    - Chỉ giữ URL ảnh nằm trong whitelist (KNOWN_IMAGE_URLS), bỏ URL bịa.
    """
    actions: list[tuple[str, object]] = []
    for segment in reply.split("[BREAK]"):
        urls_in_order = [u for u in URL_RE.findall(segment) if u in KNOWN_IMAGE_URLS]
        text = URL_RE.sub("", segment)  # bỏ URL khỏi phần chữ
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()
        if text:
            actions.append(("text", text))
        if urls_in_order:
            actions.append(("image", urls_in_order))
    return actions


# Đã gặp thực tế (News, 2026-08-25, khách Đặng Thắm): kịch bản Bước 4 quy định 1 câu mẫu CỐ ĐỊNH gồm 2 đoạn
# [BREAK] — "Dạ em nắm được vấn đề rồi ạ" (nhận xét) rồi MỚI ĐẾN câu xin SĐT — nhưng model viết đúng nội
# dung 2 đoạn lại ĐẢO NGƯỢC thứ tự gửi (xin SĐT trước, "nắm được vấn đề" sau), đọc lên bị ngược logic tự
# nhiên. Quét lại thứ tự bằng code, không tin model luôn giữ đúng thứ tự đã quy định.
_ACK_PREFIX_RE = re.compile(r"^dạ\s*em\s*n[ắă]m\s*được", re.IGNORECASE)
_PHONE_ASK_RE = re.compile(r"xin\s*(sđt|số điện thoại)", re.IGNORECASE)


def reorder_ack_before_phone_ask(actions: list[tuple[str, object]]) -> list[tuple[str, object]]:
    """Nếu reply có cả đoạn 'nhận xét' (bắt đầu "Dạ em nắm được...") và đoạn 'xin SĐT', đảm bảo đoạn nhận
    xét luôn đứng TRƯỚC đoạn xin SĐT (đúng thứ tự kịch bản) — tự hoán đổi nếu model viết ngược."""
    ack_idx = phone_idx = None
    for i, (kind, payload) in enumerate(actions):
        if kind != "text":
            continue
        if ack_idx is None and _ACK_PREFIX_RE.search(payload):
            ack_idx = i
        if phone_idx is None and _PHONE_ASK_RE.search(payload):
            phone_idx = i
    if ack_idx is not None and phone_idx is not None and phone_idx < ack_idx:
        actions = list(actions)
        actions[phone_idx], actions[ack_idx] = actions[ack_idx], actions[phone_idx]
    return actions


# Đã gặp thực tế (trang News, 2026-08-20): AI viết "reply" khẳng định "Online" nhưng trường "known_format"
# lại vẫn để "chưa biết" trong CÙNG 1 lần gọi (model không đảm bảo 100% nhất quán giữa lời văn tự do và
# trường dữ liệu có cấu trúc, dù test lại nhiều lần không tái hiện được — nhiều khả năng là sai lệch ngẫu
# nhiên hiếm gặp) -> khiến known_format="chưa biết" làm resolve_schedule_url() trả None, ảnh lịch học không
# được tự động chèn dù văn bản đã nói "gửi lịch học Online". KHÔNG dựa vào AI luôn nhất quán — đối chiếu lại
# bằng code: nếu "reply" đã khẳng định rõ 1 hình thức mà "known_format" lại "chưa biết" thì tự sửa lại.
_ONLINE_MENTION_RE = re.compile(r"lịch học\s*(ạ\s*)?online|online\s*qua\s*zoom", re.IGNORECASE)

# Đã gặp thực tế (News, 2026-08-24, khách Hien Truong): khách nói rõ "muốn tìm hiểu lớp học ONLINE cho con"
# ngay tin đầu tiên, nhưng vài lượt sau model "quên" tín hiệu này, trả known_format="chưa biết" -> gửi nhầm
# địa chỉ 4 cơ sở offline cho khách chỉ muốn học online. KHÔNG dựa vào model luôn nhớ đúng suốt hội thoại —
# quét lại CHÍNH LỊCH SỬ KHÁCH (không phải bot) bằng code để tự sửa, giống cách reconcile_known_format đã
# làm với lời văn "reply" của bot.
_CUSTOMER_ONLINE_STATEMENT_RE = re.compile(r"h[ọo]c\s*online|online\s*cho\s*con|t[ìi]m\s*hi[ểe]u.{0,15}online", re.IGNORECASE)
_OFFLINE_HINT_RE = re.compile(r"trực tiếp|offline|tại (trung tâm|cơ sở)", re.IGNORECASE)


def _customer_confirmed_online(history: list[dict], customer_message: str) -> bool:
    """True nếu có tin CỦA KHÁCH (không phải bot) khẳng định rõ muốn học Online — bỏ qua câu hỏi (chứa "?")
    hoặc câu nêu cả 2 lựa chọn cùng lúc (vừa "online" vừa "trực tiếp"/"offline" -> không rõ ràng)."""
    texts = [m.get("content", "") for m in history if m.get("role") == "user"]
    texts.append(customer_message or "")
    for text in texts:
        if not text or "?" in text or _OFFLINE_HINT_RE.search(text):
            continue
        if _CUSTOMER_ONLINE_STATEMENT_RE.search(text):
            return True
    return False


def reconcile_known_format(
    reply: str, known_format: str, history: list[dict] | None = None, customer_message: str = "",
) -> str:
    """Đối chiếu "known_format" với (1) chính nội dung "reply" và (2) lịch sử khách — nếu AI đã viết rõ đang
    gửi lịch học Online trong lời văn, HOẶC khách đã từng khẳng định rõ muốn học Online, mà known_format vẫn
    "chưa biết", tự sửa thành "Online" (chỉ áp dụng chiều Online, vì Offline cần biết đúng cơ sở cụ thể
    (CS1-CS4) mà không thể suy đoán an toàn từ văn bản chung chung)."""
    fmt = (known_format or "").strip()
    if fmt and fmt != "chưa biết":
        return known_format
    text = reply or ""
    m = _ONLINE_MENTION_RE.search(text)
    if m:
        # Chỉ tính là ĐÃ XÁC NHẬN Online nếu đoạn câu chứa cụm từ khóa là câu KHẲNG ĐỊNH — nếu dấu kết câu
        # gần nhất ngay sau đó là "?" thì đây vẫn là câu HỎI (vd "...trực tiếp hay học online qua Zoom ạ?"),
        # CHƯA xác nhận gì, không được tự sửa thành "Online" (đã gặp thực tế: gây gửi nhầm ảnh lịch học khi
        # bot mới đang hỏi hình thức, chưa biết khách chọn gì).
        next_term = re.search(r"[.!?]|\[BREAK\]", text[m.end():])
        if not (next_term and next_term.group() == "?"):
            return "Online"
    if history is not None and _customer_confirmed_online(history, customer_message):
        return "Online"
    return known_format


# Đã gặp thực tế (Định Hướng, 2026-08-26, khách Minh Hương): khách gõ "Lý 11" rất rõ ràng, bot trả lời ĐÚNG
# "lớp 11" trong lời văn (bám sát kịch bản), nhưng known_grade vẫn "chưa biết" ở NHIỀU lượt liên tiếp sau
# đó -> resolve_schedule_url() không tính ra được URL dù bot nói "em gửi chị tham khảo lịch học..." -> ảnh
# không bao giờ được gửi kèm dù bot đã hứa gửi. Cùng lớp lỗi với known_format ở trên (lời văn đúng, trường
# cấu trúc sai) — quét lại CHÍNH LỊCH SỬ KHÁCH bằng từ khóa tất định để tự sửa, không tin AI luôn nhất quán.
_GRADE_DETECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"lớp\s*12\b|lý\s*12\b|11\s*lên\s*12|2k9\b|2009\b", re.IGNORECASE), "2009"),
    (re.compile(r"lớp\s*11\b|lý\s*11\b|10\s*lên\s*11|2k10\b|2010\b", re.IGNORECASE), "2010"),
    (re.compile(r"lớp\s*10\b|lý\s*10\b|9\s*lên\s*10|2k11\b|2011\b", re.IGNORECASE), "2011"),
    (re.compile(r"lớp\s*9\b|8\s*lên\s*9|2k12\b|2012\b", re.IGNORECASE), "2012"),
    (re.compile(r"lớp\s*8\b|7\s*lên\s*8|2k13\b", re.IGNORECASE), "2k13"),
    (re.compile(r"lớp\s*7\b|6\s*lên\s*7|2k14\b", re.IGNORECASE), "2k14"),
    (re.compile(r"lớp\s*6\b|5\s*lên\s*6|2k15\b|2015\b", re.IGNORECASE), "2k15"),
]


def reconcile_known_grade(known_grade: str, history: list[dict] | None, customer_message: str = "") -> str:
    """Đối chiếu known_grade với chính lịch sử khách — nếu AI trả "chưa biết" nhưng khách đã từng nói rõ
    lớp/khóa (dò bằng từ khóa tất định), tự sửa lại. Ưu tiên tin GẦN NHẤT nếu khách nhắc lớp nhiều lần
    (trường hợp đổi ý/nói lại)."""
    grade = (known_grade or "").strip()
    if grade and grade != "chưa biết":
        return known_grade
    texts = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    texts.append(customer_message or "")
    for text in reversed(texts):
        for pattern, tag in _GRADE_DETECT_PATTERNS:
            if pattern.search(text or ""):
                return tag
    return known_grade


def resolve_schedule_url(known_grade: str, known_format: str) -> str | None:
    """Tính URL ảnh lịch học từ 2 trạng thái "đã biết" — trả None nếu chưa đủ thông tin."""
    grade = (known_grade or "").strip()
    fmt = (known_format or "").strip()
    if grade == "chưa biết" or fmt == "chưa biết" or not grade or not fmt:
        return None
    return SCHEDULE_IMAGE_MAP.get(f"{grade}|{fmt}")


def inject_schedule_image(
    actions: list[tuple[str, object]], url: str | None, already_sent: set[str]
) -> list[tuple[str, object]]:
    """
    Đảm bảo ảnh LỊCH HỌC luôn được gửi ngay khi đủ thông tin (known_grade + known_format) — KHÔNG phụ
    thuộc việc AI có "nhớ" đúng lúc phải gửi hay tự dán URL vào reply hay không. Trạng thái đọc thẳng từ
    lịch sử Pancake (already_sent) nên dù 1 lượt bị AI báo thiếu, lượt sau vẫn tự bù được.
    - Nếu ảnh này ĐÃ từng gửi trong hội thoại (already_sent) hoặc đã có sẵn trong actions lượt này -> bỏ qua.
    - Ngược lại -> chèn action ảnh ngay sau hành động đầu tiên (thường là câu dẫn "em gửi chị lịch học...").
    """
    if not url or url in already_sent:
        return actions
    already_in_actions = any(kind == "image" and url in payload for kind, payload in actions)
    if already_in_actions:
        return actions
    insert_at = 1 if actions else 0
    return actions[:insert_at] + [("image", [url])] + actions[insert_at:]


def inject_info_bundle(
    actions: list[tuple[str, object]], known_grade: str, known_format: str, already_sent: set[str]
) -> list[tuple[str, object]]:
    """
    Đảm bảo bộ 3 ảnh THÔNG TIN/NỘI QUY/HỌC PHÍ luôn đủ bộ khi AI đã có ý định gửi — KHÔNG phụ thuộc AI tự
    dán đủ cả 3 URL trong "reply" (đã gặp thực tế: AI dán thiếu 1 URL, khách chỉ nhận 2/3 ảnh).
    - Chỉ bù khi actions lượt này ĐÃ có ít nhất 1 URL thuộc bộ này (AI đã chủ động gửi, đúng quy tắc "chỉ
      gửi khi khách hỏi học phí") — KHÔNG tự ý gửi cả bộ khi AI chưa hề nhắc tới.
    - URL nào đã gửi trong lịch sử (already_sent) hoặc đã có trong actions lượt này thì bỏ qua, tránh trùng.
    """
    required = info_bundle_urls(known_grade, known_format)
    if not required:
        return actions
    urls_in_actions = {u for kind, payload in actions if kind == "image" for u in payload}  # type: ignore[union-attr]
    intent_to_send = bool(urls_in_actions & set(required))
    if not intent_to_send:
        return actions
    missing = [u for u in required if u not in urls_in_actions and u not in already_sent]
    if not missing:
        return actions
    return actions + [("image", missing)]


class Conversation:
    def __init__(self) -> None:
        self.pending_messages: list[str] = []
        self.customer_id: str = ""
        self.task: asyncio.Task | None = None
        # Tăng mỗi khi có tin khách mới (on_customer_message) — dùng để phát hiện lượt xử lý này đã bị MỘT
        # lượt MỚI HƠN "vượt mặt" hay chưa (xem _debounce_and_reply). task.cancel() không đảm bảo dừng kịp
        # nếu lượt cũ đã qua khỏi asyncio.sleep() và đang gọi AI/API (CancelledError không raise ngay lập
        # tức) — đã gặp thực tế: 2 lượt xử lý cùng 1 hội thoại chạy chồng nhau, gửi trùng/gần trùng tin.
        self.generation: int = 0


class Orchestrator:
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._tag_map: dict[str, int] = {}

    async def load_tags(self) -> None:
        try:
            for tag in await pancake.get_tags():
                self._tag_map[tag["text"].strip()] = tag["id"]
            log.info("Đã nạp %d tag", len(self._tag_map))
        except Exception as e:  # noqa: BLE001
            log.warning("Không nạp được tag (kiểm tra API/token): %s", e)

    async def on_customer_message(self, conversation_id: str, customer_id: str, text: str) -> None:
        # Bình luận (comment) trên bài viết -> KHÔNG trả lời/gửi tin gì cả (chỉ xử lý tin nhắn inbox).
        # conversation_id inbox có dạng "{page_id}_{psid}"; bình luận có dạng "{post_id}_{...}".
        if not conversation_id.startswith(f"{pancake.page_id}_"):
            log.info("Hội thoại %s: là bình luận (comment) -> bot không trả lời.", conversation_id)
            return

        conv = self._conversations.setdefault(conversation_id, Conversation())
        conv.customer_id = customer_id
        conv.pending_messages.append(text)
        conv.generation += 1
        if conv.task and not conv.task.done():
            conv.task.cancel()
        conv.task = asyncio.create_task(self._debounce_and_reply(conversation_id))

    async def _debounce_and_reply(self, conversation_id: str) -> None:
        conv = self._conversations.get(conversation_id)
        if not conv:
            return
        my_generation = conv.generation  # chốt mốc NGAY LÚC BẮT ĐẦU — xem giải thích ở Conversation.generation

        try:
            await asyncio.sleep(settings.debounce_seconds)
        except asyncio.CancelledError:
            return

        if conv.generation != my_generation:
            return  # đã có lượt MỚI HƠN vượt mặt trong lúc chờ debounce -> để lượt đó xử lý, tránh trùng
        if not conv.pending_messages:
            return
        pending = conv.pending_messages
        conv.pending_messages = []
        merged = "\n".join(pending)

        try:
            # LUÔN đọc lại toàn bộ hội thoại trước khi trả lời (tránh lặp/hỏi lại/sai bước).
            history, human_handled, sent_image_urls = await self._build_history(
                conversation_id, conv.customer_id, exclude=pending
            )
            log.info("Hội thoại %s: đọc %d tin lịch sử trước khi trả lời.", conversation_id, len(history))

            # CHỐT CHẶN: nhân viên đã nhắn tay trong hội thoại này -> bot DỪNG hẳn, không can thiệp nữa.
            if human_handled:
                log.info("Hội thoại %s: phát hiện NHÂN VIÊN đã nhắn tay -> bot dừng xử lý.", conversation_id)
                return

            # CHỐT CHẶN: chỉ dừng khi ĐÃ ĐỦ SĐT (mặc định 2: phụ huynh + học sinh).
            # Nếu mới có 1 số thì KHÔNG dừng -> để bot hỏi tiếp số còn thiếu.
            if settings.stop_when_has_phone and len(customer_phones(history)) >= settings.required_phones:
                log.info("Hội thoại %s: đã đủ %d SĐT -> KHÔNG trả lời thêm.", conversation_id, settings.required_phones)
                return

            # Xem giải thích đầy đủ trong meta_orchestrator.py: generate() đôi lúc TREO VĨNH VIỄN dù SDK đã
            # có timeout riêng — đặt thêm trần cứng bên ngoài để không bao giờ treo quá 90s.
            result = await asyncio.wait_for(ai.generate(history, merged), timeout=90)

            # CHỐT CHẶN LẦN 2: AI.generate() có thể mất vài giây -> nhân viên có thể vừa nhắn tay
            # NGAY TRONG lúc AI đang soạn (đã gặp thực tế: bot gửi tin 5s sau khi nhân viên đã vào).
            # Đọc lại tin nhắn ngay trước khi gửi để bắt kịp trường hợp này.
            recheck = (await pancake.get_messages(conversation_id, conv.customer_id)).get("messages", [])
            if has_human_reply(recheck, str(pancake.page_id)):
                log.info("Hội thoại %s: nhân viên vừa nhắn tay trong lúc AI soạn -> hủy gửi.", conversation_id)
                return

            # CHỐT CHẶN LẦN 3: tương tự — có thể đã có 1 lượt xử lý MỚI HƠN được tạo ra (khách nhắn thêm)
            # trong lúc AI đang soạn cho lượt NÀY, mà task.cancel() không dừng kịp (đã qua khỏi sleep()).
            # Nếu vậy, lượt mới hơn đó sẽ tự đọc đủ lịch sử + xử lý đúng -> lượt cũ này phải HỦY, không gửi,
            # tránh gửi trùng/gần trùng 2 câu trả lời cho cùng 1 hội thoại (đã gặp thực tế).
            if conv.generation != my_generation:
                log.info("Hội thoại %s: đã có lượt mới hơn trong lúc AI soạn -> hủy gửi lượt này.", conversation_id)
                return
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi AI: %s", e)
            return

        # Gửi trả lời: tách [BREAK] + ảnh thành nhiều tin
        reply = (result.get("reply") or "").strip()

        # CHỐT CHẶN CUỐI: dù ai.generate() không ném lỗi (không rơi vào except phía trên), nội dung "reply"
        # do model soạn vẫn có thể lỡ rò rỉ văn bản kiểu thông báo lỗi kỹ thuật (vd model bị nhiễu) — TUYỆT
        # ĐỐI KHÔNG được gửi trường hợp này cho khách. An toàn nhất là coi như AI im lặng (không gửi gì),
        # KHÔNG cố "sửa" hay gửi một phần, giống hệt cách xử lý khi reply rỗng.
        if reply and has_error_leak(reply):
            log.warning("Hội thoại %s: reply có dấu hiệu rò rỉ lỗi kỹ thuật -> KHÔNG gửi. Nội dung bị chặn: %s",
                        conversation_id, reply[:300])
            reply = ""

        if reply:
            actions = reorder_ack_before_phone_ask(parse_reply(reply))
            known_grade = reconcile_known_grade(result.get("known_grade", ""), history, merged)
            known_format = reconcile_known_format(reply, result.get("known_format", ""), history, merged)
            # "resend_images": khách rõ ràng đang hỏi lại/chưa nắm được ảnh đã gửi trước đó -> cho phép gửi
            # lại (bỏ qua chốt chặn "đã gửi rồi thì không gửi lại") thay vì chỉ gửi câu dẫn suông không ảnh.
            dedup_urls = set() if result.get("resend_images") else sent_image_urls
            schedule_url = resolve_schedule_url(known_grade, known_format)
            actions = inject_schedule_image(actions, schedule_url, dedup_urls)
            actions = inject_info_bundle(actions, known_grade, known_format, dedup_urls)
            for kind, payload in actions:
                if kind == "text":
                    await self._deliver_text(conversation_id, payload)  # type: ignore[arg-type]
                else:
                    await self._deliver_images(conversation_id, payload)  # type: ignore[arg-type]
            if actions:
                await self._apply_tag(conversation_id, BOT_TAG)
        else:
            log.info("Hội thoại %s: AI chọn im lặng (đúng kịch bản).", conversation_id)

        # (Bình luận đã bị chặn từ đầu on_customer_message() -> tới đây chắc chắn là hội thoại inbox.)
        for tag_name in result.get("tags", []):
            await self._apply_tag(conversation_id, tag_name)

        if result.get("handoff"):
            # Trang chưa có tag "cần người thật" -> chỉ log để nhân viên vào tiếp quản.
            log.warning("Hội thoại %s: AI đề nghị chuyển NGƯỜI THẬT xử lý.", conversation_id)

    async def _build_history(
        self, conversation_id: str, customer_id: str, exclude: list[str] | None = None
    ) -> tuple[list[dict], bool, set[str]]:
        """
        Đọc lại toàn bộ hội thoại (tối đa settings.history_messages tin gần nhất),
        theo đúng thứ tự thời gian, để AI nắm đủ ngữ cảnh trước khi trả lời.
        - Xác định người gửi qua from.id == page_id.
        - Bỏ HTML trong nội dung.
        - Loại các tin ở cuối trùng với tin khách vừa gửi (exclude) để không lặp.
        Trả về (history, human_handled, sent_image_urls):
        - human_handled=True nếu có tin do NHÂN VIÊN gõ tay.
        - sent_image_urls = tập URL ảnh BOT đã từng gửi trong hội thoại (để không gửi trùng ảnh lịch học).
        (Cả 2 đều quét TOÀN BỘ tin lấy về, không chỉ cửa sổ history_messages, để không bỏ sót.)
        """
        try:
            data = await pancake.get_messages(conversation_id, customer_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Không lấy được lịch sử: %s", e)
            return [], False, set()

        all_messages = data.get("messages", [])
        page_id = str(pancake.page_id)
        human_handled = has_human_reply(all_messages, page_id)

        sent_image_urls: set[str] = set()
        for m in all_messages:
            sender = str((m.get("from") or {}).get("id", ""))
            if sender != page_id:
                continue
            for att in m.get("attachments") or []:
                url = att.get("url")
                if url:
                    sent_image_urls.add(url)

        history: list[dict] = []
        for m in all_messages[-settings.history_messages:]:
            sender = str((m.get("from") or {}).get("id", ""))
            role = "assistant" if sender == page_id else "user"
            raw = m.get("message") or m.get("original_message") or ""
            content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()  # bỏ HTML
            if content:
                history.append({"role": role, "content": content})

        # Bỏ các tin khách vừa gửi (đã tách ra làm câu hỏi hiện tại) khỏi cuối lịch sử.
        pending = {p.strip() for p in (exclude or [])}
        while history and history[-1]["role"] == "user" and history[-1]["content"] in pending:
            history.pop()
        return history, human_handled, sent_image_urls

    async def _deliver_text(self, conversation_id: str, text: str) -> None:
        await human_delay()
        await rate_limiter.acquire()
        try:
            await pancake.send_text(conversation_id, text)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi gửi text: %s", e)

    async def _deliver_images(self, conversation_id: str, image_urls: list[str]) -> None:
        await human_delay()
        await rate_limiter.acquire()
        try:
            await pancake.send_image(conversation_id, image_urls)
        except Exception as e:  # noqa: BLE001
            log.exception("Lỗi gửi ảnh %s: %s", image_urls, e)

    async def _apply_tag(self, conversation_id: str, tag_name: str) -> None:
        tag_id = self._tag_map.get(tag_name.strip())
        if tag_id is None:
            log.warning("Chưa map được tag '%s' (tạo tag này trong Pancake trước)", tag_name)
            return
        try:
            await pancake.toggle_tag(conversation_id, tag_id, add=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Lỗi gắn tag %s: %s", tag_name, e)


orchestrator = Orchestrator()
