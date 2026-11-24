"""
Lớp AI: dùng Gemini (Google AI) để tư vấn & chốt sale cho Trung tâm Vật lý siêu nhẩm.

"Bộ não" = sales_playbook.md (sinh từ 3 file trong thongtin/ bằng build_knowledge.py).
Kịch bản đã nhúng sẵn URL ảnh và ký hiệu [BREAK] ở đúng chỗ, nên AI chỉ cần
TRẢ LỜI BÁM MẪU: chèn URL ảnh + [BREAK] y như trong kịch bản.
orchestrator.py sẽ tách [BREAK] và gửi ảnh thành các tin riêng.

Output JSON:
  - reply: câu trả lời (có thể chứa [BREAK] và URL ảnh). Để RỖNG nếu phải im lặng.
  - tags: tag nghiệp vụ gắn vào hội thoại
  - handoff: True nếu cần chuyển nhân viên thật
"""
from __future__ import annotations

import json
import logging
import os
import re

from google import genai
from google.genai import types

from config import settings

log = logging.getLogger("ai_service")

# ==== Cấu hình theo TỪNG TRANG (category_tags.json + business_name qua .env) — xem config.py. ====
# Mỗi trang (Vật Lý Định Hướng, Vật Lý News, Đánh Giá Tư Duy...) tự khai danh mục/tag/nhóm học phí riêng
# qua file JSON riêng (settings.category_tags_path), KHÔNG hardcode cứng trong code -> dùng chung 1 bộ
# code cho nhiều doanh nghiệp/trang khác nhau mà nội dung tách biệt hoàn toàn.
def _load_category_tags(path: str) -> dict:
    default = {"categories": [], "format_values": ["Online"]}
    if not os.path.exists(path):
        log.warning("Thiếu file '%s' — danh mục/tag sẽ trống, bot khó hoạt động đúng.", path)
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_CATEGORY_DATA = _load_category_tags(settings.category_tags_path)
_CATEGORIES: list[dict] = _CATEGORY_DATA.get("categories", [])

# Tag NGHIỆP VỤ THẬT của trang (text khớp Pancake -> orchestrator map ra ID số).
# Tag theo khóa/năm sinh của con (hoặc danh mục tương đương) + trạng thái khách.
ALLOWED_TAGS = (
    ["Từ chối"]
    + [c["tag"] for c in _CATEGORIES]
    + ["Bot không rep", settings.tag_reengage_tag_name]
)

# Gợi ý map lớp/danh mục -> tag (đưa vào prompt cho AI gắn đúng).
_GRADE_TAG_HINT = "; ".join(f"{c['hint']}->'{c['tag']}'" for c in _CATEGORIES) + "."

# Nhóm theo bộ ảnh học phí (vd Vật Lý: "Lý" cấp 3 vs "KHTN" cấp 2) — key phải khớp đúng với info_image_map
# (dạng "{Online/Offline}|Học phí {fee_group}"). Danh mục không khai fee_group thì bỏ qua bước bù ảnh này.
_FEE_GROUP_BY_TAG: dict[str, str] = {c["tag"]: c["fee_group"] for c in _CATEGORIES if c.get("fee_group")}


def _read(path: str, fallback: str = "") -> str:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    log.warning("Thiếu file '%s' — chạy build_knowledge.py.", path)
    return fallback


SALES_PLAYBOOK = _read(settings.sales_playbook_path, "(Chưa có kịch bản sale.)")

# Whitelist URL ảnh hợp lệ — bot chỉ được gửi ảnh nằm trong đây (chống bịa link).
KNOWN_IMAGE_URLS: set[str] = {
    u.strip() for u in _read(settings.known_image_urls_path).splitlines() if u.strip()
}

# Ảnh LỊCH HỌC — map "{tag_lớp}|{CS1/CS2/CS3/CS4/Online}" -> URL, xác minh từ quy_trinh_sale.xlsx.
# AI CHỈ CẦN chọn đúng khóa (structured field "schedule_image_key"), KHÔNG cần tự dán URL vào reply
# nữa -> code (orchestrator) tự động đính kèm đúng ảnh, loại bỏ hẳn lỗi "nói gửi ảnh nhưng quên dán URL".
def _load_schedule_image_map(path: str) -> dict[str, str]:
    import json as _json

    if not os.path.exists(path):
        log.warning("Thiếu file '%s' — ảnh lịch học sẽ không được tự động gửi.", path)
        return {}
    raw: dict[str, str] = _json.load(open(path, encoding="utf-8"))
    # CS3 dùng chung ảnh với CS1 (theo kịch bản gốc) — chỉ áp dụng cho nhóm nhiều cơ sở (cấp 3).
    aliased = dict(raw)
    for key, url in raw.items():
        grade, loc = key.split("|", 1)
        if loc == "CS1" and f"{grade}|CS2" in raw:  # có CS2 -> là nhóm cấp 3 nhiều cơ sở
            aliased[f"{grade}|CS3"] = url
    return aliased


SCHEDULE_IMAGE_MAP: dict[str, str] = _load_schedule_image_map(settings.schedule_image_map_path)

# Ảnh "THÔNG TIN LỚP HỌC" (Thông tin/Nội quy/Học phí) — map "{Online/Offline}|{Thông tin/Nội quy/Học phí
# KHTN/Học phí Lý}" -> URL. Trước đây AI phải tự dán đủ 3 URL trong "reply" theo mẫu kịch bản -> hay bị
# THIẾU 1 ảnh khi AI quên/rút gọn câu trả lời. Nay code (orchestrator) tự bù nốt ảnh còn thiếu trong bộ,
# miễn AI đã dán ÍT NHẤT 1 URL của bộ này (tức đã có ý định gửi bộ ảnh, không tự ý gửi khi chưa được hỏi).
def _load_info_image_map(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        log.warning("Thiếu file '%s' — ảnh thông tin/học phí sẽ không được tự động bù.", path)
        return {}
    return json.load(open(path, encoding="utf-8"))


INFO_IMAGE_MAP: dict[str, str] = _load_info_image_map(settings.info_image_map_path)


def info_bundle_urls(known_grade: str, known_format: str) -> list[str]:
    """3 URL bắt buộc của bộ ảnh thông tin/nội quy/học phí ứng với lớp+hình thức đã biết. [] nếu chưa đủ."""
    grade = (known_grade or "").strip()
    fmt = (known_format or "").strip()
    if fmt == "Online":
        fmt_key = "Online"
    elif fmt in {"CS1", "CS2", "CS3", "CS4"}:
        fmt_key = "Offline"
    else:
        return []
    fee_group = _FEE_GROUP_BY_TAG.get(grade)
    if not fee_group:
        return []
    keys = [f"{fmt_key}|Thông tin", f"{fmt_key}|Nội quy", f"{fmt_key}|Học phí {fee_group}"]
    urls = [INFO_IMAGE_MAP.get(k) for k in keys]
    return [u for u in urls if u]

# 2 field trạng thái "hiện đang biết gì" — hỏi CỐ ĐỊNH mỗi lượt (không phải quyết định "có nên gửi lúc
# này không"), nên đơn giản/ổn định hơn nhiều so với để AI tự quyết "lúc này có phải gửi ảnh không".
# Code (orchestrator) sẽ TỰ tính khoá + TỰ động gửi ảnh lịch học ngay khi cả 2 đều đã biết và ảnh đó
# CHƯA từng được gửi trong hội thoại (đọc thẳng từ lịch sử Pancake) — không phụ thuộc AI có "nhớ" gửi
# đúng lượt hay không; nếu 1 lượt bị sót, lượt sau code vẫn tự bù vì trạng thái vẫn còn nguyên.
KNOWN_GRADE_VALUES: list[str] = [c["tag"] for c in _CATEGORIES] + ["chưa biết"]
KNOWN_FORMAT_VALUES: list[str] = list(_CATEGORY_DATA.get("format_values", ["Online"])) + ["chưa biết"]

_SYSTEM_PROMPT_TEMPLATE = _read(
    settings.system_prompt_path,
    '''Bạn là nhân viên tư vấn & chốt sale của Trung tâm "{business_name}".
Trả lời khách qua chat (Pancake) bằng tiếng Việt.
(CHƯA CÓ file system_prompt — chỉ có khung tối thiểu này, cần điền {system_prompt_path} trước khi dùng thật.)

===== KỊCH BẢN =====
{sales_playbook}
===== HẾT KỊCH BẢN =====

Gắn tag phù hợp trong: {allowed_tags}. {grade_tag_hint}''',
)
SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.format(
    business_name=settings.business_name,
    known_grade_values=KNOWN_GRADE_VALUES,
    known_format_values=KNOWN_FORMAT_VALUES,
    sales_playbook=SALES_PLAYBOOK,
    allowed_tags=ALLOWED_TAGS,
    grade_tag_hint=_GRADE_TAG_HINT,
    system_prompt_path=settings.system_prompt_path,
)

TAG_REENGAGE_PROMPT = f"""Bạn là nhân viên tư vấn của Trung tâm "{settings.business_name}", xưng "em", gọi khách "chị".
Khách đã dừng phản hồi sau khi hẹn sẽ trả lời sau/để hỏi người nhà/để suy nghĩ thêm về việc học. Đọc TOÀN BỘ
lịch sử hội thoại bên dưới, soạn 1 tin nhắn TÁI CHĂM (hỏi thăm lại) NGẮN GỌN (tối đa 3-4 câu), yêu cầu:
1. LỄ PHÉP, thân thiện, KHÔNG thúc ép/tạo áp lực, KHÔNG tỏ ra sốt ruột.
2. Nhắc lại ĐÚNG 1 câu TÓM TẮT ngắn gọn CHỈ VỀ lớp của con / hình thức học ĐÃ BIẾT trong lịch sử (nếu có) để
   khách dễ nhớ lại ngữ cảnh — TUYỆT ĐỐI KHÔNG được tự thêm/suy đoán bất kỳ chi tiết nào KHÁC không có sẵn
   NGUYÊN VĂN trong lịch sử bên dưới, đặc biệt CẤM: số tiền/học phí (VNĐ, triệu...), % giảm giá/ưu đãi/khuyến
   mãi, lịch học cụ thể (thứ mấy, giờ nào) nếu con số đó KHÔNG xuất hiện y hệt trong lịch sử, cam kết/bảo
   hành/hoàn tiền kết quả học tập dưới mọi hình thức. Nếu không chắc 1 chi tiết có thật trong lịch sử hay
   không thì KHÔNG nhắc tới chi tiết đó, chỉ nói chung chung.
3. Kết thúc bằng ĐÚNG 1 câu hỏi RÕ RÀNG, CÓ ĐẦU ĐUÔI — khách đọc xong biết ngay cần trả lời gì (vd hỏi đã
   trao đổi/suy nghĩ xong chưa, có cần trung tâm hỗ trợ thêm thông tin gì để quyết định không).
4. KHÔNG lặp nguyên văn bất kỳ câu nào bot đã từng nói trong lịch sử bên dưới.
5. CHỈ trả về ĐÚNG nội dung tin nhắn cần gửi — không thêm giải thích, không thêm tiêu đề, không thêm dấu
   ngoặc kép bao quanh."""

# Dùng cho generate_reengage_continue(): khách IM LẶNG, không có tin mới -> bot CHỦ ĐỘNG viết tiếp ĐÚNG BƯỚC
# KẾ TIẾP của quy trình sale (dựa trên chính SYSTEM_PROMPT/kịch bản chính ở trên, không phải prompt riêng) —
# tránh việc tái chăm cứ nhảy thẳng sang "xin SĐT" dù hội thoại thực tế còn chưa tới bước đó (vd khách mới chỉ
# chào rồi im lặng, chưa hề được hỏi hình thức/gửi lịch học/hỏi tình hình — nhắn "xin SĐT" ngay lúc này là SAI
# bước, bỏ qua các bước tư vấn ở giữa).
_REENGAGE_CONTINUE_SUFFIX = """

⚠️ LƯU Ý ĐẶC BIỆT CHO LƯỢT NÀY: khách đã IM LẶNG một thời gian, KHÔNG có tin nhắn mới nào của khách để trả
lời — đây là lượt bot CHỦ ĐỘNG viết tiếp (không phải trả lời câu hỏi nào). Đọc kỹ TOÀN BỘ lịch sử phía trên,
xác định CHÍNH XÁC đang ở bước nào trong "MỤC TIÊU & LUỒNG CHỐT" (đúng 5 bước) rồi viết ĐÚNG nội dung của bước
TIẾP THEO — TUYỆT ĐỐI KHÔNG được nhảy cóc thẳng sang bước xin SĐT (bước 5) nếu quy trình thực tế CHƯA đi hết
các bước 1-3 (đã hỏi hình thức chưa, đã gửi lịch học + hỏi tình hình chưa) — TRỪ NGOẠI LỆ riêng cho bước 3 nêu
ngay bên dưới. Nếu bước tiếp theo đúng ra là chờ khách trả lời 1 câu hỏi cụ thể bot đã hỏi (khách chưa trả
lời, KHÔNG phải câu hỏi tình hình học tập ở bước 3) — nhắc lại câu hỏi đó bằng cách diễn đạt KHÁC (không lặp
nguyên văn) để gợi lại.

⚠️ NGOẠI LỆ RIÊNG CHO BƯỚC 3 "HỎI TÌNH HÌNH HỌC TẬP": nếu tin cuối cùng của bot trong lịch sử là tin đã gửi
lịch học kèm câu hỏi về tình hình học tập của con (bước 3) mà khách CHƯA trả lời câu hỏi đó — KHÔNG hỏi lại/
nhắc lại câu hỏi tình hình học tập này dưới bất kỳ hình thức nào, và KHÔNG chờ để nhận xét (bước 4, vì bước
này cần dựa vào câu trả lời của khách mà khách chưa cho). Bỏ qua thẳng bước 4, chuyển ngay sang xin SĐT (bước
5) luôn ở lượt này (áp dụng đầy đủ mọi quy tắc xin SĐT đã nêu ở trên, gồm cả quy tắc "chỉ xin SĐT 1 lần" và
kiểm tra bên dưới).

⛔ KIỂM TRA BẮT BUỘC TRƯỚC KHI VIẾT — ĐÃ TỪNG XIN SĐT CHƯA: quét TOÀN BỘ tin của bot (assistant) trong lịch
sử phía trên (kể cả tin nằm NGAY TRƯỚC lượt này) — nếu BẤT KỲ tin nào đã chứa nội dung xin SĐT (các cụm như
"cho em xin SĐT", "xin số điện thoại"...), thì hội thoại này ĐÃ DÙNG HẾT lượt xin SĐT, bất kể khách đã trả
lời hay chưa, bất kể tin đó cách đây bao lâu hay nằm ở cuối lịch sử. Trường hợp này:
- TUYỆT ĐỐI CẤM viết lại bất kỳ câu xin SĐT nào nữa, dù chỉ đổi vài từ hay giữ nguyên văn — đây là lỗi
  NGHIÊM TRỌNG NHẤT cần tránh ở lượt này.
- Kiểm tra tiếp: tin nhắn dẫn link website ("Em gửi chị tham khảo thông tin các lớp học bên em ạ" kèm link)
  đã từng xuất hiện trong lịch sử CHƯA? Nếu CHƯA từng gửi -> gửi đúng nguyên văn tin đó (xem quy tắc ở mục
  "CHỈ CẦN ĐÚNG 1 SỐ ĐIỆN THOẠI" phía trên). Nếu ĐÃ từng gửi rồi -> để "reply" RỖNG (""), không viết gì thêm.
Chỉ khi lịch sử CHƯA hề có tin xin SĐT nào thì mới được áp dụng luồng bình thường (tiếp tục đúng bước, có thể
là xin SĐT nếu đã tới bước 5)."""

# Lớp chặn CUỐI CÙNG trước MỌI tin gửi cho khách (cả luồng tư vấn chính lẫn tái chăm) — đảm bảo dù API/
# model có trục trặc kiểu gì, khách TUYỆT ĐỐI KHÔNG BAO GIỜ nhận được nội dung rò rỉ lỗi kỹ thuật/thông báo
# lỗi API thay vì câu trả lời thật. Việc generate() ném exception đã được try/except chặn từ trước (không
# gửi gì) — bộ lọc này xử lý trường hợp KHÁC: API trả về "thành công" (không exception) nhưng nội dung do
# model soạn lại lỡ chứa văn bản kiểu lỗi (model bị nhiễu/lặp lại ngữ cảnh lỗi nó thấy đâu đó).
_ERROR_LEAK_RE = re.compile(
    r"\btraceback\b|\bexception\b|internal server error|\bapi key\b|rate limit exceeded|quota exceeded|"
    r"\btimed? ?out\b|\bundefined\b|null pointer|status code \d{3}|http/1\.[01]|"
    r"\b(4\d{2}|5\d{2})\s*(error|bad request|unauthorized|forbidden|not found|too many requests|"
    r"internal|server error)\b|\berror:\s|\bstacktrace\b",
    re.IGNORECASE,
)


def has_error_leak(text: str) -> bool:
    """True nếu tin (dự định gửi cho khách) có dấu hiệu lẫn nội dung lỗi kỹ thuật/thông báo lỗi API."""
    return bool(_ERROR_LEAK_RE.search(text or ""))


# Lớp bảo vệ CỨNG cho các tin AI TỰ DO soạn (không qua response_schema có cấu trúc như generate() chính) —
# tái chăm SĐT lần 2, tái chăm tag. Đã gặp thực tế: dù prompt đã dặn "không bịa", AI vẫn có lúc tự bịa học
# phí cụ thể + cam kết hoàn tiền không có thật (ca Mai Tran, 2026-08-04). KHÔNG tin tưởng hoàn toàn prompt
# tuân thủ 100% — luôn quét lại bằng regex trước khi gửi, nếu nghi ngờ thì dùng câu mẫu an toàn thay thế.
_UNSAFE_AI_MESSAGE_RE = re.compile(
    r"VNĐ|VND|triệu\b|nghìn\b|đ/tháng|%\s*(giảm|ưu đãi)|cam kết|hoàn (lại|tiền|học phí)|"
    r"ưu đãi|khuyến mãi|giảm giá|\d[.,]?\d{3}[.,]\d{3}|thứ\s*\d.{0,15}\d{1,2}h",
    re.IGNORECASE,
)


_MIN_AI_MESSAGE_LEN = 15  # ký tự chữ/số thật (bỏ khoảng trắng/dấu câu) — chặn tin suy biến kiểu chỉ có "."


def is_ai_message_safe(text: str) -> bool:
    """True nếu tin nhắn AI tự soạn KHÔNG chứa dấu hiệu bịa số tiền/lịch học cụ thể/cam kết, KHÔNG rò rỉ
    nội dung lỗi kỹ thuật, VÀ đủ dài để là 1 câu có nghĩa (đã gặp thực tế: với hội thoại chưa biết lớp/hình
    thức, AI tái chăm lần 2 trả về đúng 1 dấu "." lặp lại 100% các lần thử — không bịa số liệu nên lọt qua
    bộ lọc cũ, nhưng vô nghĩa)."""
    text = text or ""
    meaningful_len = len(re.sub(r"[\s.,!?;:…\-]+", "", text))
    if meaningful_len < _MIN_AI_MESSAGE_LEN:
        return False
    if has_error_leak(text):
        return False
    return not _UNSAFE_AI_MESSAGE_RE.search(text)


RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    properties={
        "reply": types.Schema(type=types.Type.STRING, description="Trả lời cho khách (có thể chứa [BREAK] và URL ảnh học phí); rỗng nếu phải im lặng"),
        "known_grade": types.Schema(
            type=types.Type.STRING, enum=KNOWN_GRADE_VALUES,
            description="Lớp của con đã biết chắc tính đến giờ (đọc toàn bộ lịch sử); 'chưa biết' nếu chưa rõ",
        ),
        "known_format": types.Schema(
            type=types.Type.STRING, enum=KNOWN_FORMAT_VALUES,
            description="Hình thức/cơ sở học đã biết chắc tính đến giờ; 'chưa biết' nếu chưa rõ",
        ),
        "tags": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING, enum=ALLOWED_TAGS)),
        "handoff": types.Schema(type=types.Type.BOOLEAN),
        "resend_images": types.Schema(
            type=types.Type.BOOLEAN,
            description=(
                "True CHỈ KHI khách rõ ràng đang hỏi lại/chưa nắm được ảnh lịch học hoặc ảnh thông tin lớp "
                "học ĐÃ gửi trước đó trong lịch sử (vd khách lặp lại đúng câu đã hỏi, nói chưa thấy ảnh, xin "
                "gửi lại...) — cho phép hệ thống gửi lại đúng ảnh đó dù đã gửi rồi. False cho mọi trường hợp "
                "khác (mặc định)."
            ),
        ),
    },
    required=["reply", "known_grade", "known_format", "tags", "handoff", "resend_images"],
    property_ordering=["reply", "known_grade", "known_format", "tags", "handoff", "resend_images"],
)


class AIService:
    def __init__(self) -> None:
        # QUAN TRỌNG: PHẢI có timeout — nếu không, khi API Gemini bị treo/mạng chập chờn, tác vụ xử lý hội
        # thoại đó sẽ chờ VĨNH VIỄN (không lỗi, không log, không bao giờ tự phục hồi, chỉ hết khi restart
        # bot) vì generate_content() không có giới hạn thời gian mặc định. Đã gặp thực tế: 2 hội thoại
        # (07-27 và 08-04) bị "treo" đúng ngay sau bước đọc lịch sử, không có dấu vết gì tiếp theo trong log.
        # retry_options: TỰ ĐỘNG thử lại khi API Gemini quá tải/lỗi tạm thời (429 rate-limit, 5xx server) —
        # dùng cơ chế có sẵn của SDK (exponential backoff + jitter), không cần tự viết vòng lặp retry.
        # timeout: PHẢI có — nếu không, khi API bị treo/mạng chập chờn, tác vụ xử lý hội thoại đó sẽ chờ
        # VĨNH VIỄN (không lỗi, không log, không bao giờ tự phục hồi, chỉ hết khi restart bot) vì
        # generate_content() không có giới hạn thời gian mặc định. Đã gặp thực tế: 2 hội thoại (07-27 và
        # 08-04) bị "treo" đúng ngay sau bước đọc lịch sử, không có dấu vết gì tiếp theo trong log.
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=45_000,  # ms — mỗi lần thử
                retry_options=types.HttpRetryOptions(
                    attempts=4,  # thử tối đa 4 lần (gồm lần đầu) trước khi báo lỗi hẳn
                    initial_delay=1.0,
                    max_delay=15.0,
                    exp_base=2.0,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
            ),
        )

    async def generate(self, conversation_history: list[dict], customer_message: str) -> dict:
        contents = self._to_gemini_contents(conversation_history, customer_message)
        resp = await self._client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=settings.gemini_temperature,
                top_p=settings.gemini_top_p,
            ),
        )
        return json.loads(resp.text)

    async def generate_tag_reengage_message(self, conversation_history: list[dict]) -> str:
        """Soạn tin tái chăm cho hội thoại có tag 'Cần tái chăm' (khách hẹn trả lời sau) — không phải mẫu
        tĩnh, AI đọc lịch sử và tự viết tin nhắc lại + hỏi thăm phù hợp ngữ cảnh từng khách."""
        contents = self._history_to_contents(conversation_history)
        resp = await self._client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=TAG_REENGAGE_PROMPT,
                temperature=settings.gemini_temperature,
                top_p=settings.gemini_top_p,
            ),
        )
        return (resp.text or "").strip()

    async def generate_reengage_continue(self, conversation_history: list[dict]) -> dict:
        """Khách IM LẶNG (không có tin mới) -> bot CHỦ ĐỘNG viết tiếp ĐÚNG BƯỚC KẾ TIẾP của quy trình sale,
        dùng lại chính SYSTEM_PROMPT + RESPONSE_SCHEMA của luồng chính (không phải prompt "xin SĐT" riêng) —
        để không nhảy cóc sang xin SĐT nếu hội thoại thực tế chưa đi hết các bước tư vấn ở giữa. Trả về dict
        cùng cấu trúc generate() (reply/known_grade/known_format/tags/handoff) để orchestrator xử lý y hệt
        (tự động chèn ảnh lịch học/học phí, gắn tag...)."""
        contents = self._history_to_contents(conversation_history)
        resp = await self._client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT + _REENGAGE_CONTINUE_SUFFIX,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=settings.gemini_temperature,
                top_p=settings.gemini_top_p,
            ),
        )
        return json.loads(resp.text)

    @staticmethod
    def _history_to_contents(history: list[dict]) -> list[types.Content]:
        return [
            types.Content(role=("model" if m["role"] == "assistant" else "user"), parts=[types.Part(text=m["content"])])
            for m in history
        ]

    @classmethod
    def _to_gemini_contents(cls, history: list[dict], customer_message: str) -> list[types.Content]:
        contents = cls._history_to_contents(history)
        contents.append(types.Content(role="user", parts=[types.Part(text=customer_message)]))
        return contents


ai = AIService()
