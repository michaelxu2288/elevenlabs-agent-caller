"""The HTTP/WebSocket server Twilio talks to (through the public tunnel).

  GET  /health               tunnel checks
  POST /twilio/status        call status callbacks (X-Twilio-Signature verified)
  GET  /twilio/media         Media Stream WebSocket; only accepted when the `start`
                             message carries a live call_id plus its one-time token
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from dataclasses import dataclass, field

from aiohttp import WSMsgType, web

from .twilio_api import validate_signature

log = logging.getLogger("server")
TERMINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


@dataclass
class CallContext:
    call_id: str
    token: str
    session: object                      # CallSession
    frame_bytes: int = 160
    call_sid: str = ""
    statuses: list[dict] = field(default_factory=list)
    final_status: str = ""
    final: asyncio.Event = field(default_factory=asyncio.Event)
    stream_seen: bool = False


class CallRegistry:
    def __init__(self):
        self.calls: dict[str, CallContext] = {}

    def add(self, ctx: CallContext) -> None:
        self.calls[ctx.call_id] = ctx

    def get(self, call_id: str) -> CallContext | None:
        return self.calls.get(call_id or "")


def build_app(registry: CallRegistry, *, auth_token: str, public_base: callable,
              validate: bool = True) -> web.Application:
    """public_base() returns the https URL Twilio uses (needed to check signatures)."""
    app = web.Application()

    async def health(_req):
        return web.json_response({"ok": True, "service": "voice-caller"})

    async def status(req: web.Request):
        form = await req.post()
        params = {k: form.getall(k) if len(form.getall(k)) > 1 else form.get(k) for k in form.keys()}
        if validate:
            url = public_base().rstrip("/") + req.path_qs
            if not validate_signature(auth_token, url, params, req.headers.get("X-Twilio-Signature", "")):
                log.warning("rejected status callback with a bad signature")
                return web.Response(status=403)
        ctx = registry.get(req.query.get("call_id", ""))
        if not ctx:
            return web.Response(status=404)
        st = str(params.get("CallStatus", ""))
        entry = {"status": st, "duration": params.get("CallDuration"), "sip": params.get("SipResponseCode"),
                 "answered_by": params.get("AnsweredBy")}
        ctx.statuses.append(entry)
        ctx.session.rec.event("twilio_status", **{k: v for k, v in entry.items() if v})
        log.info("call status: %s", st)
        if st in TERMINAL_STATUSES:
            ctx.final_status = st
            ctx.final.set()
        return web.Response(status=204)

    def upgrade_signature(req: web.Request) -> str:
        sig = req.headers.get("X-Twilio-Signature", "")
        if not sig:
            return "absent"
        base = public_base().rstrip("/")
        ws_base = "ws" + base[len("http"):] if base.startswith("http") else base   # https->wss, http->ws
        for b in (ws_base, base):
            for path in (req.path_qs, req.path_qs + "/"):
                if validate_signature(auth_token, b + path, {}, sig):
                    return "valid"
        return "unverified"

    async def media(req: web.Request):
        sig_state = upgrade_signature(req)
        # identity encoding on the 101: works around cloudflared rewriting Accept-Encoding
        # on Twilio's upgrade (cloudflared issue #1465); harmless everywhere else
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        ws.headers["Content-Encoding"] = "identity"
        await ws.prepare(req)
        ctx: CallContext | None = None
        session = None
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type == WSMsgType.ERROR:
                        log.warning("media websocket error: %s", ws.exception())
                    continue
                data = json.loads(msg.data)
                ev = data.get("event")
                if ev == "media":
                    if session is not None and (data.get("media") or {}).get("track", "inbound") == "inbound":
                        await session.on_media(base64.b64decode(data["media"]["payload"]))
                elif ev == "mark":
                    if session is not None:
                        session.on_mark((data.get("mark") or {}).get("name", ""))
                elif ev == "start":
                    start = data.get("start") or {}
                    custom = start.get("customParameters") or {}
                    cand = registry.get(custom.get("call_id", ""))
                    if (cand is None or cand.stream_seen
                            or not hmac.compare_digest(cand.token, str(custom.get("token", "")))):
                        log.warning("rejected a media stream that does not match a live call")
                        await ws.close(code=1008, message=b"unknown call")
                        break
                    ctx, session = cand, cand.session
                    ctx.stream_seen = True
                    ctx.call_sid = start.get("callSid", ctx.call_sid)
                    from .session import MediaSender
                    sender = MediaSender(ws, start.get("streamSid") or data.get("streamSid", ""), ctx.frame_bytes)
                    await session.on_start(start, sender)
                    # the per-call token is what admits a stream; the upgrade signature is
                    # recorded so a real call shows whether it can be enforced too
                    session.rec.event("stream_upgrade_signature", state=sig_state)
                elif ev == "stop":
                    if session is not None:
                        await session.on_stop("twilio sent stop")
                    break
                elif ev == "dtmf" and session is not None:
                    session.on_dtmf((data.get("dtmf") or {}).get("digit", ""))
                elif ev == "connected":
                    pass
        finally:
            if session is not None:
                await session.on_stop("media websocket closed")
            if not ws.closed:
                await ws.close()
        return ws

    app.router.add_get("/health", health)
    app.router.add_post("/twilio/status", status)
    app.router.add_get("/twilio/media", media)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
