"""Local stand-in for the ElevenLabs HTTP API (the endpoints voice_caller uses).

TTS returns FSK "speech" (see fsk.py) as raw ulaw_8000, streamed in chunks after a
short time-to-first-byte; STT demodulates the uploaded WAV. Both check the API key and
the request shape the real API expects, and count usage for the cost report.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from voice_caller.audio import wav_to_pcm

from . import fsk


class FakeElevenLabs:
    def __init__(self, api_key: str, voice_id: str, ttfb_s: float = 0.15, stt_latency_s: float = 0.25):
        self.api_key = api_key
        self.voice_id = voice_id
        self.ttfb_s = ttfb_s
        self.stt_latency_s = stt_latency_s
        self.usage = {"tts_requests": 0, "tts_chars": 0, "stt_requests": 0, "stt_audio_s": 0.0,
                      "stt_decode_errors": 0, "rejected": 0}
        self.tts_texts: list[str] = []
        self.stt_texts: list[str] = []
        self.app = web.Application()
        self.app.router.add_post("/v1/text-to-speech/{voice_id}/stream", self.tts)
        self.app.router.add_post("/v1/speech-to-text", self.stt)
        self.app.router.add_get("/v1/user/subscription", self.subscription)
        self.app.router.add_get("/v1/voices/{voice_id}", self.voice)

    def _authorized(self, req: web.Request) -> bool:
        ok = req.headers.get("xi-api-key") == self.api_key
        if not ok:
            self.usage["rejected"] += 1
        return ok

    @staticmethod
    def _error(status: int, code: str, message: str) -> web.Response:
        return web.json_response({"detail": {"status": code, "message": message}}, status=status)

    async def tts(self, req: web.Request):
        if not self._authorized(req):
            return self._error(401, "invalid_api_key", "Invalid API key")
        if req.match_info["voice_id"] != self.voice_id:
            return self._error(404, "voice_not_found", "voice not found")
        if req.query.get("output_format") != "ulaw_8000":
            return self._error(422, "invalid_output_format", "this fake only serves ulaw_8000 (what Twilio needs)")
        body = await req.json()
        text = body.get("text", "")
        if not text or not body.get("model_id"):
            return self._error(422, "invalid_request", "text and model_id are required")
        self.usage["tts_requests"] += 1
        self.usage["tts_chars"] += len(text)
        self.tts_texts.append(text)
        audio = fsk.encode_ulaw([text])
        resp = web.StreamResponse(headers={"Content-Type": "audio/basic"})
        await resp.prepare(req)
        await asyncio.sleep(self.ttfb_s)
        for i in range(0, len(audio), 1000):
            await resp.write(audio[i:i + 1000])
        await resp.write_eof()
        return resp

    async def stt(self, req: web.Request):
        if not self._authorized(req):
            return self._error(401, "invalid_api_key", "Invalid API key")
        form = await req.post()
        if not form.get("model_id") or "file" not in form:
            return self._error(422, "invalid_request", "model_id and file are required")
        pcm, rate = wav_to_pcm(form["file"].file.read())
        if rate != 8000:
            return self._error(422, "invalid_audio", "the fake expects 8 kHz WAV")
        self.usage["stt_requests"] += 1
        self.usage["stt_audio_s"] = round(self.usage["stt_audio_s"] + len(pcm) / rate, 2)
        packets, errors = fsk.decode_all(pcm)
        await asyncio.sleep(self.stt_latency_s)
        self.usage["stt_decode_errors"] += len(errors)
        text = " ".join(packets)
        if errors and not packets:
            text = "(inaudible)"
        self.stt_texts.append(text)
        return web.json_response({"language_code": "eng", "language_probability": 0.99, "text": text,
                                  "words": []})

    async def subscription(self, req: web.Request):
        if not self._authorized(req):
            return self._error(401, "invalid_api_key", "Invalid API key")
        return web.json_response({"tier": "free", "character_count": self.usage["tts_chars"],
                                  "character_limit": 10000, "status": "active"})

    async def voice(self, req: web.Request):
        if not self._authorized(req):
            return self._error(401, "invalid_api_key", "Invalid API key")
        if req.match_info["voice_id"] != self.voice_id:
            return self._error(404, "voice_not_found", "voice not found")
        return web.json_response({"voice_id": self.voice_id, "name": "Fake Voice", "category": "premade"})

    def report(self) -> str:
        return json.dumps(self.usage)
