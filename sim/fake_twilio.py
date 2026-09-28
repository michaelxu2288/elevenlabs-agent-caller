"""Local stand-in for Twilio: the REST endpoints voice_caller uses, plus the phone network.

On a create-call request it behaves the way Twilio documents: signed status callbacks
(initiated, ringing, answered, completed), then a Media Streams WebSocket to the
<Stream> URL from the inline TwiML with connected/start/media/stop messages. Inbound
audio goes out as 20 ms mu-law frames in real time (silence included); outbound audio
is buffered and "played" at real-time speed into the callee's ears; marks are echoed
when playback reaches them; `clear` flushes the buffer and returns pending marks.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets as pysecrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from voice_caller.audio import rms, ulaw_to_pcm
from voice_caller.twilio_api import compute_signature

from . import fsk

log = logging.getLogger("fake-twilio")
SILENCE_FRAME = bytes([0xFF]) * 160


@dataclass
class LineStats:
    inbound_frames: int = 0
    outbound_bytes: int = 0
    outbound_messages: int = 0
    marks_received: int = 0
    marks_echoed: int = 0
    clears: int = 0
    protocol_errors: list = field(default_factory=list)
    status_callbacks: list = field(default_factory=list)
    agent_decode_errors: list = field(default_factory=list)
    dtmf_heard: list = field(default_factory=list)


class Ears:
    """The callee's hearing: demodulates the agent's FSK speech, groups it into utterances."""

    def __init__(self, on_utterance, on_audio_start, on_dtmf, stats: LineStats):
        self.current: list[str] = []
        self.errors_in_utt: list[str] = []
        self.decoder = fsk.FskDecoder(on_packet=self.current.append, on_error=self._err)
        self.on_utterance, self.on_audio_start, self.on_dtmf = on_utterance, on_audio_start, on_dtmf
        self.stats = stats
        self.in_audio = False
        self.silent_frames = 0
        self._dtmf_run: tuple[str | None, int] = (None, 0)

    def _err(self, why: str) -> None:
        self.errors_in_utt.append(why)
        self.stats.agent_decode_errors.append(why)

    def feed(self, ulaw_frame: bytes) -> None:
        pcm = ulaw_to_pcm(ulaw_frame)
        loud = rms(pcm) > 1000
        if loud:
            self.silent_frames = 0
            if not self.in_audio:
                self.in_audio = True
                self.on_audio_start()
        else:
            self.silent_frames += 1
        digit = fsk.detect_dtmf(pcm)
        prev, run = self._dtmf_run
        run = run + 1 if digit and digit == prev else (1 if digit else 0)
        self._dtmf_run = (digit, run)
        if digit and run == 3:
            self.stats.dtmf_heard.append(digit)
            self.on_dtmf(digit)
        self.decoder.feed(pcm)
        if self.in_audio and self.silent_frames >= 30:        # 600 ms of quiet: they're done
            self.in_audio = False
            self.decoder.feed(ulaw_to_pcm(SILENCE_FRAME * 4))
            text, errs = " ".join(self.current), list(self.errors_in_utt)
            self.current.clear()
            self.errors_in_utt.clear()
            if text or errs:
                self.on_utterance(text, errs)


class Phone:
    """What the simulated callee can do on the line."""

    def __init__(self, call: "FakeCall"):
        self._call = call

    @property
    def speed(self) -> float:
        return self._call.fake.speed

    def say(self, segments) -> float:
        """Queue speech; returns its duration in (simulated) seconds."""
        audio = fsk.encode_ulaw(segments if isinstance(segments, list) else [segments])
        self._call.mouth.extend(audio)
        return len(audio) / 8000

    @property
    def speaking(self) -> bool:
        return len(self._call.mouth) > 0

    @property
    def agent_speaking(self) -> bool:
        return self._call.ears.in_audio if self._call.ears else False

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)

    async def until_quiet(self) -> None:
        while self.speaking and not self._call.ended.is_set():
            await asyncio.sleep(0.02 / self.speed)

    def now(self) -> float:
        return self._call.sim_time()

    def hang_up(self) -> None:
        self._call.end("callee hung up")

    @property
    def ended(self) -> asyncio.Event:
        return self._call.ended


class FakeCall:
    def __init__(self, fake: "FakeTwilio", sid: str, form, stream_url: str, params: dict):
        self.fake = fake
        self.sid = sid
        self.to = form["To"]
        self.from_ = form["From"]
        self.stream_url = stream_url
        self.params = params
        self.status_cb = form.get("StatusCallback", "")
        self.events = set(form.getall("StatusCallbackEvent", [])) or {"completed"}
        self.time_limit = int(form.get("TimeLimit", 14400))
        self.send_digits = form.get("SendDigits", "")
        self.status = "queued"
        self.stream_sid = "MZ" + pysecrets.token_hex(16)
        self.mouth = bytearray()
        self.playbuf = bytearray()
        self.received_total = 0
        self.played_total = 0
        self.marks: list[tuple[int, str]] = []
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.ears: Ears | None = None
        self.callee = None
        self.answered_at: float | None = None
        self.duration = 0
        self._seq = 0
        self._ws = None

    def sim_time(self) -> float:
        return 0.0 if self.answered_at is None else (time.monotonic() - self.answered_at) * self.fake.speed

    def end(self, reason: str) -> None:
        if not self.ended.is_set():
            self.end_reason = reason
            self.ended.set()

    async def _post_status(self, status: str, event: str, extra: dict | None = None) -> None:
        self.status = status
        if event not in self.events or not self.status_cb:
            return
        self._seq += 1
        params = {"CallSid": self.sid, "AccountSid": self.fake.account_sid, "From": self.from_, "To": self.to,
                  "CallStatus": status, "ApiVersion": "2010-04-01", "Direction": "outbound-api",
                  "CallbackSource": "call-progress-events", "SequenceNumber": str(self._seq - 1),
                  "Timestamp": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())}
        params.update(extra or {})
        sig = compute_signature(self.fake.auth_token, self.status_cb, params)
        try:
            async with self.fake.http.post(self.status_cb, data=params,
                                           headers={"X-Twilio-Signature": sig}) as resp:
                self.fake.stats.status_callbacks.append((status, resp.status))
        except Exception as e:  # noqa: BLE001
            self.fake.stats.status_callbacks.append((status, f"error {e}"))

    async def run(self) -> None:
        stats = self.fake.stats
        try:
            await self._post_status("initiated", "initiated")
            await asyncio.sleep(0.2)
            await self._post_status("ringing", "ringing")
            await asyncio.sleep(self.fake.ring_s / self.fake.speed)
            if self.ended.is_set():          # canceled while ringing
                await self._post_status("canceled", "completed", {"CallDuration": "0"})
                return
            self.answered_at = time.monotonic()
            await self._post_status("in-progress", "answered")
            headers = {"X-Twilio-Signature": compute_signature(self.fake.auth_token, self.stream_url, {})}
            async with self.fake.http.ws_connect(self.stream_url, headers=headers) as ws:
                self._ws = ws
                await ws.send_str(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
                await ws.send_str(json.dumps({
                    "event": "start", "sequenceNumber": "1", "streamSid": self.stream_sid,
                    "start": {"accountSid": self.fake.account_sid, "streamSid": self.stream_sid,
                              "callSid": self.sid, "tracks": ["inbound"], "customParameters": self.params,
                              "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))
                phone = Phone(self)
                self.callee = self.fake.callee_factory(phone)
                self.ears = Ears(self.callee.on_heard, self.callee.on_agent_audio_start, self.callee.on_dtmf, stats)
                tasks = [asyncio.create_task(self._receive(ws)), asyncio.create_task(self._tick(ws)),
                         asyncio.create_task(self.callee.run())]
                if self.send_digits:
                    tasks.append(asyncio.create_task(self._play_send_digits()))
                await self.ended.wait()
                for t in tasks:
                    t.cancel()
                if not ws.closed:
                    try:
                        await ws.send_str(json.dumps({"event": "stop", "sequenceNumber": "0",
                                                      "streamSid": self.stream_sid,
                                                      "stop": {"accountSid": self.fake.account_sid,
                                                               "callSid": self.sid}}))
                    except ConnectionResetError:
                        pass
                    await ws.close()
        except Exception as e:  # noqa: BLE001
            log.exception("fake call crashed")
            stats.protocol_errors.append(f"call crashed: {e}")
            self.end(f"error: {e}")
        finally:
            self.duration = int(self.sim_time() + 0.999)
            await self._post_status("completed", "completed", {"CallDuration": str(self.duration)})
            if self.callee and hasattr(self.callee, "on_call_end"):
                self.callee.on_call_end(self.end_reason)

    async def _play_send_digits(self) -> None:
        # SendDigits: Twilio plays these after the call connects; the callee's menu hears them
        from voice_caller.audio import dtmf_ulaw
        await asyncio.sleep(0.5 / self.fake.speed)
        audio = dtmf_ulaw(self.send_digits)
        for i in range(0, len(audio), 160):
            self.ears.feed(audio[i:i + 160].ljust(160, b"\xff"))

    async def _receive(self, ws) -> None:
        stats = self.fake.stats
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            ev = data.get("event")
            if data.get("streamSid") != self.stream_sid:
                stats.protocol_errors.append(f"{ev} with wrong streamSid")
                continue
            if ev == "media":
                payload = base64.b64decode(data["media"]["payload"])
                self.playbuf.extend(payload)
                self.received_total += len(payload)
                stats.outbound_bytes += len(payload)
                stats.outbound_messages += 1
            elif ev == "mark":
                stats.marks_received += 1
                self.marks.append((self.received_total, data["mark"]["name"]))
            elif ev == "clear":
                stats.clears += 1
                self.playbuf.clear()
                self.played_total = self.received_total
                await self._echo_marks(ws)
            else:
                stats.protocol_errors.append(f"unknown event {ev}")
        self.end("server closed the stream")

    async def _echo_marks(self, ws) -> None:
        while self.marks and self.marks[0][0] <= self.played_total:
            _, name = self.marks.pop(0)
            self.fake.stats.marks_echoed += 1
            await ws.send_str(json.dumps({"event": "mark", "sequenceNumber": "0", "streamSid": self.stream_sid,
                                          "mark": {"name": name}}))

    async def _tick(self, ws) -> None:
        loop = asyncio.get_running_loop()
        interval = 0.02 / self.fake.speed
        next_t = loop.time()
        seq, ts = 2, 0
        stats = self.fake.stats
        while not self.ended.is_set():
            frame = bytes(self.mouth[:160]).ljust(160, b"\xff")
            del self.mouth[:160]
            await ws.send_str(json.dumps({
                "event": "media", "sequenceNumber": str(seq), "streamSid": self.stream_sid,
                "media": {"track": "inbound", "chunk": str(seq - 1), "timestamp": str(ts),
                          "payload": base64.b64encode(frame).decode("ascii")}}))
            stats.inbound_frames += 1
            out = bytes(self.playbuf[:160])
            del self.playbuf[:160]
            self.played_total += len(out)
            self.ears.feed(out.ljust(160, b"\xff"))
            await self._echo_marks(ws)
            seq += 1
            ts += 20
            if self.sim_time() > self.time_limit:
                self.end("time limit reached")
            next_t += interval
            await asyncio.sleep(max(0.0, next_t - loop.time()))


class FakeTwilio:
    def __init__(self, account_sid: str, auth_token: str, numbers: set[str], callee_factory,
                 speed: float = 1.0, ring_s: float = 1.5):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.numbers = numbers
        self.callee_factory = callee_factory
        self.speed = speed
        self.ring_s = ring_s
        self.calls: dict[str, FakeCall] = {}
        self.stats = LineStats()
        self.requests: list[tuple[str, str]] = []
        self.http: aiohttp.ClientSession | None = None
        self.app = web.Application()
        base = "/2010-04-01/Accounts/{account}"
        self.app.router.add_post(base + "/Calls.json", self.create_call)
        self.app.router.add_post(base + "/Calls/{call}.json", self.update_call)
        self.app.router.add_get(base + "/Calls/{call}.json", self.get_call)
        self.app.router.add_get(base + ".json", self.get_account)
        self.app.router.add_get(base + "/IncomingPhoneNumbers.json", self.incoming_numbers)
        self.app.on_startup.append(self._startup)
        self.app.on_cleanup.append(self._cleanup)

    async def _startup(self, _app):
        self.http = aiohttp.ClientSession()

    async def _cleanup(self, _app):
        for call in self.calls.values():
            call.end("fake twilio shutting down")
        await self.http.close()

    def _auth_error(self, req: web.Request) -> web.Response | None:
        self.requests.append((req.method, req.path))
        auth = req.headers.get("Authorization", "")
        expected = aiohttp.BasicAuth(self.account_sid, self.auth_token).encode()
        if req.match_info["account"] != self.account_sid or auth != expected:
            return web.json_response({"code": 20003, "message": "Authenticate", "status": 401}, status=401)
        return None

    @staticmethod
    def _err(code: int, message: str, status: int = 400) -> web.Response:
        return web.json_response({"code": code, "message": message, "status": status}, status=status)

    def _call_json(self, c: FakeCall) -> dict:
        return {"sid": c.sid, "account_sid": self.account_sid, "to": c.to, "from": c.from_, "status": c.status,
                "direction": "outbound-api", "duration": str(c.duration) if c.ended.is_set() else None}

    async def create_call(self, req: web.Request):
        if (err := self._auth_error(req)):
            return err
        form = await req.post()
        for key in ("To", "From"):
            if not form.get(key):
                return self._err(21201, f"No '{key}' number is specified")
        if form["From"] not in self.numbers:
            return self._err(21210, f"The source phone number provided, {form['From']}, is not yet verified "
                                    "for your account")
        twiml = form.get("Twiml", "")
        if not twiml or len(twiml) > 4000:
            return self._err(21205, "Twiml is required by this fake and must be at most 4000 characters")
        try:
            root = ET.fromstring(twiml)
            stream = root.find("./Connect/Stream")
            url = stream.get("url")
            params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
        except Exception as e:  # noqa: BLE001
            return self._err(12100, f"Document parse failure: {e}")
        if "?" in url:
            return self._err(31920, "Stream URL must not contain a query string; use <Parameter>")
        sid = "CA" + pysecrets.token_hex(16)
        call = FakeCall(self, sid, form, url, params)
        self.calls[sid] = call
        asyncio.create_task(call.run())
        return web.json_response(self._call_json(call), status=201)

    async def update_call(self, req: web.Request):
        if (err := self._auth_error(req)):
            return err
        call = self.calls.get(req.match_info["call"])
        if not call:
            return self._err(20404, "The requested resource was not found", 404)
        form = await req.post()
        if form.get("Status") in ("completed", "canceled"):
            call.end("hung up via API")
        return web.json_response(self._call_json(call))

    async def get_call(self, req: web.Request):
        if (err := self._auth_error(req)):
            return err
        call = self.calls.get(req.match_info["call"])
        if not call:
            return self._err(20404, "The requested resource was not found", 404)
        return web.json_response(self._call_json(call))

    async def get_account(self, req: web.Request):
        if (err := self._auth_error(req)):
            return err
        return web.json_response({"sid": self.account_sid, "status": "active", "type": "Full",
                                  "friendly_name": "Fake account"})

    async def incoming_numbers(self, req: web.Request):
        if (err := self._auth_error(req)):
            return err
        n = req.query.get("PhoneNumber", "")
        return web.json_response({"incoming_phone_numbers": [{"phone_number": n}] if n in self.numbers else []})
