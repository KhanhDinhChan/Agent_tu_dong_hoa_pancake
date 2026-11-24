"""
TRÌNH TEST LOCAL — chat thử với bot ngay trên máy, KHÔNG đụng Pancake.
Gõ tin như khách, xem bot trả lời (text + ảnh render thật + tag).

Chạy:  uvicorn local_test_app:app --port 8000
Mở:    http://localhost:8000
"""
from __future__ import annotations

import csv
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ai_service import ai
from config import settings
from orchestrator import customer_phones, inject_info_bundle, inject_schedule_image, parse_reply, resolve_schedule_url

app = FastAPI(title="Bot Test Local")

IMAGES_DIR = "images"


def _load_manifest() -> dict[str, str]:
    """url -> tên file local (chỉ ảnh tải thành công)."""
    path = os.path.join(IMAGES_DIR, "manifest.csv")
    mapping: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "ok":
                    mapping[row["url"]] = row["file"]
    return mapping


URL_TO_LOCAL = _load_manifest()


def _img_src(url: str) -> str:
    """Ảnh nào tải được thì phục vụ từ local cho chắc; còn lại dùng URL gốc."""
    fname = URL_TO_LOCAL.get(url)
    return f"/img/{fname}" if fname else url


@app.get("/img/{name}")
async def img(name: str):
    path = os.path.join(IMAGES_DIR, name)
    if os.path.exists(path):
        return FileResponse(path)
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    history = body.get("history", [])
    message = body.get("message", "")

    # CHỐT CHẶN: chỉ dừng khi đã đủ SĐT (phụ huynh + học sinh). Có 1 số thì vẫn hỏi tiếp.
    if settings.stop_when_has_phone and len(customer_phones(history)) >= settings.required_phones:
        return {"stopped": True, "reply_raw": "", "actions": [], "tags": []}

    result = await ai.generate(history, message)
    reply = (result.get("reply") or "").strip()

    actions = []
    if reply:  # y hệt orchestrator.py: reply rỗng -> im lặng hoàn toàn, KHÔNG injext ảnh gì cả
        parsed = parse_reply(reply)
        known_grade = result.get("known_grade", "")
        known_format = result.get("known_format", "")
        schedule_url = resolve_schedule_url(known_grade, known_format)
        parsed = inject_schedule_image(parsed, schedule_url, already_sent=set())  # test local: không track lịch sử ảnh
        parsed = inject_info_bundle(parsed, known_grade, known_format, already_sent=set())
        for kind, payload in parsed:
            if kind == "text":
                actions.append({"type": "text", "content": payload})
            else:
                actions.append({"type": "image", "urls": [_img_src(u) for u in payload]})

    tags = list(result.get("tags", []))

    return {
        "reply_raw": reply,
        "actions": actions,
        "tags": tags,
        "handoff": result.get("handoff", False),
        "known_grade": result.get("known_grade", ""),
        "known_format": result.get("known_format", ""),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


HTML = """
<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Bot — Vật Lý Siêu Nhẩm</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--bot:#334155;--me:#2563eb;--txt:#e2e8f0;--mut:#94a3b8}
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);height:100vh;display:flex;flex-direction:column}
  header{padding:12px 16px;background:var(--card);border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px}
  header b{font-size:15px}
  header span{color:var(--mut);font-size:12px}
  #chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;max-width:760px;width:100%;margin:0 auto}
  .row{display:flex;flex-direction:column;max-width:78%}
  .row.me{align-self:flex-end;align-items:flex-end}
  .row.bot{align-self:flex-start;align-items:flex-start}
  .bubble{padding:9px 13px;border-radius:14px;white-space:pre-wrap;line-height:1.4;font-size:14px}
  .me .bubble{background:var(--me);color:#fff;border-bottom-right-radius:4px}
  .bot .bubble{background:var(--bot);border-bottom-left-radius:4px}
  .bot img{max-width:260px;border-radius:12px;margin-top:2px;display:block}
  .tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
  .tag{background:#0ea5e9;color:#fff;font-size:11px;padding:2px 8px;border-radius:20px}
  .tag.bot{background:#a855f7}
  .meta{color:var(--mut);font-size:11px;margin-top:3px}
  .typing{color:var(--mut);font-size:13px;font-style:italic}
  footer{padding:12px;background:var(--card);border-top:1px solid #334155}
  .inp{display:flex;gap:8px;max-width:760px;margin:0 auto}
  input{flex:1;padding:11px 14px;border-radius:22px;border:1px solid #475569;background:#0f172a;color:var(--txt);font-size:14px;outline:none}
  button{padding:0 20px;border:0;border-radius:22px;background:var(--me);color:#fff;font-weight:600;cursor:pointer}
  button:disabled{opacity:.5;cursor:default}
  .hint{max-width:760px;margin:6px auto 0;color:var(--mut);font-size:11px;text-align:center}
</style></head><body>
<header><b>🤖 Test Bot — Vật Lý Siêu Nhẩm</b><span>chạy local · không gửi lên Pancake</span>
  <button style="margin-left:auto;padding:6px 12px;font-size:12px" onclick="reset()">Hội thoại mới</button>
</header>
<div id="chat"></div>
<footer>
  <div class="inp">
    <input id="msg" placeholder="Nhập tin nhắn như khách hàng… (vd: con mình lớp 11, học ở Tạ Quang Bửu)" autofocus>
    <button id="send" onclick="send()">Gửi</button>
  </div>
  <div class="hint">Gợi ý: "học phí lớp 10" · "con lớp 8 học online" · "alo có ai không" · "cho xin địa chỉ cơ sở"</div>
</footer>
<script>
let history=[];
const chat=document.getElementById('chat'), inp=document.getElementById('msg'), btn=document.getElementById('send');
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function addMe(t){const r=document.createElement('div');r.className='row me';r.innerHTML=`<div class="bubble">${esc(t)}</div>`;chat.appendChild(r);scroll();}
function addTyping(){const r=document.createElement('div');r.className='row bot';r.id='typing';r.innerHTML='<div class="typing">Bot đang soạn…</div>';chat.appendChild(r);scroll();}
function rmTyping(){const t=document.getElementById('typing');if(t)t.remove();}
function addBot(data){
  if(data.stopped){
    const r=document.createElement('div');r.className='row bot';
    r.innerHTML='<div class="bubble" style="background:#7c2d12;color:#fed7aa">⛔ Bot đã DỪNG — hội thoại đã có SĐT của khách (nhường nhân viên xử lý).</div>';
    chat.appendChild(r);scroll();return;
  }
  data.actions.forEach(a=>{
    const r=document.createElement('div');r.className='row bot';
    if(a.type==='text'){r.innerHTML=`<div class="bubble">${esc(a.content)}</div>`;}
    else{r.innerHTML=a.urls.map(u=>`<img src="${u}" onerror="this.style.opacity=.3;this.alt='(ảnh lỗi/link hỏng)'">`).join('');}
    chat.appendChild(r);
  });
  const info=document.createElement('div');info.className='row bot';
  let tg=(data.tags||[]).map(t=>`<span class="tag${t==='Bot'?' bot':''}">${esc(t)}</span>`).join('');
  info.innerHTML=`<div class="tags">${tg}</div>`+(data.handoff?'<div class="meta">⚠️ handoff = chuyển người thật</div>':'');
  chat.appendChild(info);scroll();
}
function scroll(){chat.scrollTop=chat.scrollHeight;}
function reset(){history=[];chat.innerHTML='';inp.focus();}
async function send(){
  const t=inp.value.trim();if(!t)return;
  addMe(t);inp.value='';btn.disabled=true;addTyping();
  try{
    const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history,message:t})});
    const data=await res.json();rmTyping();addBot(data);
    history.push({role:'user',content:t});
    if(data.reply_raw)history.push({role:'assistant',content:data.reply_raw});
  }catch(e){rmTyping();const r=document.createElement('div');r.className='row bot';r.innerHTML=`<div class="bubble">Lỗi: ${esc(String(e))}</div>`;chat.appendChild(r);}
  btn.disabled=false;inp.focus();
}
inp.addEventListener('keydown',e=>{if(e.key==='Enter')send();});
</script></body></html>
"""
