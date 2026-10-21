# -*- coding: utf-8 -*-
"""
酒店预订 Web 入口 —— FastAPI + Jinja2。

启动：
  pip install fastapi 'uvicorn[standard]' jinja2 requests dotenv
  uvicorn web:app --reload --host 127.0.0.1 --port 8000

功能：
  - GET  /              聊天页面（Jinja2 模板）
  - POST /api/chat      对话接口（JSON 入 user_input，出 reply）
  - POST /api/chat/stream  对话接口 SSE 版（先推阶段事件，最后推完整回复）
  - GET  /orders        订单列表页面
  - GET  /api/orders    订单列表 JSON
  - POST /api/orders/{id}/pay     模拟支付
  - POST /api/orders/{id}/cancel  取消订单（道旅真实订单给出手动指引）

存储：订单走 storage 层（SQLite 自动回退内存，配 MySQL 可选），评分/酒店缓存与 CLI 版共用同一套磁盘文件。
会话：session agent 池——前端携带 session_id，同一会话复用同一 Agent 实例（多轮对话状态保持），
      30 分钟不活跃自动回收，池容量上限 100（超出踢最旧），不同会话实例隔离互不污染。
"""

import os, sys, time, uuid, json, queue, asyncio, threading
from pathlib import Path

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))
os.chdir(str(PROJ))

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import requests
from urllib.parse import urlparse

from agent import HotelBookingAgent
from models import BookingParams
import tools
import storage

app = FastAPI(title="酒店预订 Agent Web 版", version="0.2.0")
templates = Jinja2Templates(directory=str(PROJ / "templates"))


# ---------- 会话 agent 池（多轮对话状态保持 + 会话间隔离）----------
_SESSION_TTL = 30 * 60     # 会话 30 分钟不活跃即回收
_SESSION_MAX = 100         # 池容量上限，超出踢最旧会话
_agents: dict = {}         # session_id -> {"agent": HotelBookingAgent, "ts": float}
_agents_lock = threading.Lock()


def _get_or_create_agent(session_id: str) -> HotelBookingAgent:
    """按 session_id 取/建 agent 实例；顺手清理过期会话。线程安全。"""
    now = time.time()
    with _agents_lock:
        # 回收过期会话（避免依赖后台定时任务）
        for k in [k for k, v in _agents.items() if now - v["ts"] > _SESSION_TTL]:
            del _agents[k]
        # 超容踢最旧
        if len(_agents) >= _SESSION_MAX:
            del _agents[min(_agents, key=lambda k: _agents[k]["ts"])]
        entry = _agents.get(session_id)
        if entry is None:
            entry = {"agent": HotelBookingAgent(), "ts": now}
            _agents[session_id] = entry
        entry["ts"] = now
        return entry["agent"]


def session_count() -> int:
    """当前活跃会话数（/api/health 用）。"""
    return len(_agents)


# ---------- 请求/响应模型 ----------
class ChatRequest(BaseModel):
    user_input: str
    session_id: str = ""      # 前端生成的会话标识（UUID）；同一会话复用同一 Agent 实例


class ChatResponse(BaseModel):
    reply: str
    current_order: str = ""       # 当前订单摘要（若有）
    session_id: str = ""          # 回传会话标识（前端未带时由服务端生成）
    storage_backend: str          # memory / sqlite / mysql
    storage_enabled: bool          # True = 真实持久化


# ---------- 页面路由 ----------
# HTML 页面一律 no-cache：前端模板更新后浏览器立即拿到新版，
# 避免缓存旧 JS（如旧版图片正则不认 .jpg-t 导致图片渲染成链接）
@app.get("/", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
    }, headers={"Cache-Control": "no-cache"})


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, filter: str = "ALL"):
    return templates.TemplateResponse(request, "orders.html", {
        "current_filter": filter.upper(),
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
    }, headers={"Cache-Control": "no-cache"})


# ---------- API 路由 ----------
@app.post("/api/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    if not req.user_input.strip():
        raise HTTPException(status_code=400, detail="user_input 不能为空")
    # 会话池：同一 session_id 复用同一 Agent 实例，多轮对话状态（搜索结果/
    # 选中酒店/预订上下文）跨请求保持；未带 session_id 时由服务端生成
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    agent = _get_or_create_agent(session_id)
    reply = agent.run(req.user_input.strip()) or "(助手无回复)"
    current = ""
    if agent.state.current_order:
        o = agent.state.current_order
        current = f"当前订单 {o.order_id}：{o.hotel_name} · {o.room_type} · ¥{o.total_price:g}"
    return ChatResponse(
        reply=reply, current_order=current, session_id=session_id,
        storage_backend=storage.storage_backend(),
        storage_enabled=storage.storage_available(),
    )


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest):
    """
    SSE 流式对话：先推阶段事件（event: stage），最后推完整回复（event: result）。

    agent.run 是同步阻塞函数，放后台线程跑；on_stage 回调把阶段事件写进
    queue，async 生成器轮询 queue 转 SSE 帧。事件协议：
      event: stage   data: {"stage": "searching", "text": "正在搜索酒店…"}
      event: result  data: {"reply": "...", ...}   （最终完整回复，ChatResponse 结构）
      event: error   data: {"message": "..."}
    """
    if not req.user_input.strip():
        raise HTTPException(status_code=400, detail="user_input 不能为空")
    session_id = (req.session_id or "").strip() or str(uuid.uuid4())
    agent = _get_or_create_agent(session_id)
    q: "queue.Queue" = queue.Queue()

    def on_stage(key: str, text: str) -> None:
        q.put(("stage", {"stage": key, "text": text}))

    def run_agent() -> None:
        try:
            agent.on_stage = on_stage
            reply = agent.run(req.user_input.strip()) or "(助手无回复)"
            current = ""
            if agent.state.current_order:
                o = agent.state.current_order
                current = f"当前订单 {o.order_id}：{o.hotel_name} · {o.room_type} · ¥{o.total_price:g}"
            q.put(("result", {
                "reply": reply, "current_order": current,
                "session_id": session_id,
                "storage_backend": storage.storage_backend(),
                "storage_enabled": storage.storage_available(),
            }))
        except Exception as e:            # 后台线程异常也要送到前端
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            agent.on_stage = None
            q.put(("__end__", None))

    threading.Thread(target=run_agent, daemon=True).start()

    async def event_gen():
        while True:
            try:
                kind, data = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)   # 轮询间隔 100ms，阶段提示无感延迟
                continue
            if kind == "__end__":
                break
            yield (f"event: {kind}\n"
                   f"data: {json.dumps(data, ensure_ascii=False)}\n\n")

    return StreamingResponse(
        event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/orders")
async def api_orders(filter: str = "ALL"):
    """返回订单列表（JSON）。filter: ALL / PENDING / FINISHED / CANCELLED"""
    result = tools.list_history_orders(filter)
    rows = result["rows"]
    # 简化：只返回 agent 页面需要的字段
    trimmed = []
    for r in rows:
        trimmed.append({
            "order_no": r.get("order_no"),
            "hotel_name": r.get("hotel_name"),
            "room_name": r.get("room_name"),
            "check_in": r.get("check_in"),
            "check_out": r.get("check_out"),
            "total_price": r.get("total_price"),
            "currency": r.get("currency"),
            "status_text": r.get("status_text"),
            "payment_url": r.get("payment_url", ""),
            "source": r.get("source", "mock"),
        })
    return {"rows": trimmed, "logged_in": result["logged_in"],
            "filter": result["filter"], "source": result["source"],
            "remote_error": result.get("remote_error")}


@app.post("/api/orders/{order_id}/pay")
async def api_pay(order_id: str):
    ok = tools.pay_order(order_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    return {"ok": True, "order_id": order_id}


@app.post("/api/orders/{order_id}/cancel")
async def api_cancel(order_id: str):
    result = tools.cancel_order(order_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail=f"订单 {order_id} 不存在")
    if result == "remote_unsupported":
        # 道旅真实订单无取消接口，返回手动指引
        return {"ok": False, "order_id": order_id,
                "reason": "remote_unsupported",
                "guide": tools.real_order_cancel_guide(order_id)}
    return {"ok": True, "order_id": order_id}


# ---------- 图片代理 ----------
# 浏览器直连道旅 CDN 会被 Chrome 的 ORB（Opaque Response Blocking）拦成
# net::ERR_BLOCKED_BY_ORB（localhost 页面加载公网 CDN 的跨源子资源），页面里
# 图片只能退化成链接。改由服务端转发，前端同源取图，绕开浏览器跨源限制。
_IMG_ALLOW_HOSTS = ('image-cdn.rollinggo.cn', 'dimg04.c-ctrip.com', 'dimg02.c-ctrip.com')
_IMG_CACHE: dict = {}          # url -> (bytes, content_type)
_IMG_CACHE_MAX = 300
_IMG_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'),
    'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
}
# 注意：这里**不能**带 Referer。
# 实测道旅 CDN 的房型图（room-type 桶）带任何 Referer 都返回
# 403（OSS: denied by bucket referer policy），只有不带 Referer 才 200；
# 酒店主图（hotel 桶）不带 Referer 同样 200。所以统一用空 Referer 最稳。
_IMG_GONE: set = set()      # 确认 404 的 URL，不再重试
# 抓成功的图落盘，重启后仍可用，且不再消耗 CDN 配额
_IMG_DISK_DIR = os.path.join(str(PROJ), 'cache', 'img')
# 抓失败的 URL 冷却 5 秒：CDN 偶发抖动很常见，重试几乎都能成功，
# 所以只做很短的冷却，让前端重试能真正打到 CDN。
_IMG_FAIL: dict = {}
_IMG_FAIL_TTL = 5
# 失败时返回占位图（而不是裸 JSON 报错，用户点到链接不会看到异常信息）
_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="280" height="190">'
    '<rect width="100%" height="100%" fill="#f5f6f7"/>'
    '<text x="140" y="98" text-anchor="middle" fill="#9aa4b2" '
    'font-family="sans-serif" font-size="13">图片暂时不可用，请稍后再试</text>'
    '</svg>'
).encode('utf-8')


def _img_disk_path(url: str) -> str:
    import hashlib
    return os.path.join(_IMG_DISK_DIR, hashlib.sha1(url.encode()).hexdigest() + '.img')


_IMG_LOCK = threading.Lock()
_IMG_LAST = [0.0]
_IMG_MIN_INTERVAL = 2.0


def _img_throttle():
    """保证两次抓图之间至少间隔 _IMG_MIN_INTERVAL 秒。"""
    with _IMG_LOCK:
        wait = _IMG_MIN_INTERVAL - (time.time() - _IMG_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _IMG_LAST[0] = time.time()


def _fetch_image(url: str):
    """抓图：内存缓存 → 磁盘缓存 → 网络。失败返回占位图，不报错。"""
    if url in _IMG_CACHE:
        return _IMG_CACHE[url]

    disk = _img_disk_path(url)
    if os.path.exists(disk):
        try:
            with open(disk, 'rb') as f:
                data = f.read()
            if len(data) > 512:
                _IMG_CACHE[url] = (data, 'image/jpeg')
                return _IMG_CACHE[url]
        except OSError:
            pass

    if url in _IMG_GONE:            # 确认不存在，别浪费时间重试
        return _PLACEHOLDER, 'image/svg+xml'

    ts = _IMG_FAIL.get(url)
    if ts and time.time() - ts < _IMG_FAIL_TTL:
        return _PLACEHOLDER, 'image/svg+xml'

    for attempt in range(2):
        _img_throttle()
        try:
            r = requests.get(url, timeout=20, headers=_IMG_HEADERS)
        except requests.RequestException:
            time.sleep(2.0)
            continue
        ctype = r.headers.get('content-type', '')
        if r.status_code == 200 and ctype.startswith('image/'):
            if len(_IMG_CACHE) >= _IMG_CACHE_MAX:
                _IMG_CACHE.pop(next(iter(_IMG_CACHE)))
            _IMG_CACHE[url] = (r.content, ctype)
            try:
                os.makedirs(_IMG_DISK_DIR, exist_ok=True)
                with open(disk, 'wb') as f:
                    f.write(r.content)
            except OSError:
                pass
            _IMG_FAIL.pop(url, None)
            return _IMG_CACHE[url]
        if r.status_code == 404:
            _IMG_GONE.add(url)
            break
        time.sleep(2.0)

    if url not in _IMG_GONE:
        _IMG_FAIL[url] = time.time()
    return _PLACEHOLDER, 'image/svg+xml'


@app.get("/api/img")
def proxy_image(url: str = Query(..., description="图片直链")):
    """同步路由：FastAPI 会放进线程池，阻塞式抓图不会卡住事件循环。"""
    host = (urlparse(url).hostname or '').lower()
    if not any(host == h or host.endswith('.' + h) for h in _IMG_ALLOW_HOSTS):
        raise HTTPException(status_code=400, detail='host not allowed')
    content, ctype = _fetch_image(url)
    # 占位图不能缓存：CDN 恢复后要能立刻拿到真图
    cache = 'no-store' if ctype == 'image/svg+xml' else 'public, max-age=3600'
    headers = {'Cache-Control': cache}
    if url in _IMG_GONE:
        headers['X-Img-Gone'] = '1'      # 告诉前端"这张确实没有"，别再重试
    return Response(content=content, media_type=ctype, headers=headers)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "storage_backend": storage.storage_backend(),
        "storage_enabled": storage.storage_available(),
        "active_sessions": session_count(),
        "rollinggo_logged_in": tools.rollinggo_enabled() and False,  # 需实际 rlg.is_logged_in()
    }


if __name__ == "__main__":
    import threading
    import webbrowser

    import uvicorn
    print(f"storage_backend={storage.storage_backend()}  "
          f"(SQLite/MySQL 可用={storage.storage_available()})")
    print("启动 Web 服务… http://127.0.0.1:8000")
    # 服务就绪后自动打开浏览器
    threading.Timer(1.5, webbrowser.open, args=("http://127.0.0.1:8000/",)).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
