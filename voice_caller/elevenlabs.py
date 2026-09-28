"""ElevenLabs text-to-speech (streamed as 8 kHz mu-law, ready for Twilio) and speech-to-text."""
from __future__ import annotations

import logging
import time
from typing import AsyncIterator

import aiohttp

from .audio import pcm_to_wav
from .config import ElevenLabsConfig

log = logging.getLogger("elevenlabs")


class ElevenLabsError(RuntimeError):
    def __init__(self, what: str, status: int, detail: str):
        super().__init__(f"ElevenLabs {what} failed: HTTP {status}: {detail[:300]}")
        self.status = status


async def _error_detail(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
        d = body.get("detail", body)
        if isinstance(d, dict):
            return f"{d.get('status') or d.get('code') or ''} {d.get('message') or d}".strip()
        return str(d)
    except Exception:
        return (await resp.text())[:300]


class ElevenLabs:
    def __init__(self, http: aiohttp.ClientSession, api_key: str, cfg: ElevenLabsConfig):
        self._http = http
        self._headers = {"xi-api-key": api_key}
        self.cfg = cfg
        self.base = cfg.api_base.rstrip("/")
        self.usage = {"tts_requests": 0, "tts_chars": 0, "stt_requests": 0, "stt_audio_s": 0.0}

    async def tts_stream(self, text: str, previous_text: str = "",
                         voice_id: str | None = None) -> AsyncIterator[bytes]:
        """Yield raw 8 kHz mu-law bytes as they are generated."""
        cfg = self.cfg
        body = {
            "text": text,
            "model_id": cfg.tts_model,
            # speaker boost and style add latency; neither matters on an 8 kHz phone line
            "voice_settings": {"stability": cfg.stability, "similarity_boost": cfg.similarity_boost,
                               "speed": cfg.speed, "style": 0, "use_speaker_boost": False},
        }
        if previous_text:
            body["previous_text"] = previous_text[-500:]
        url = f"{self.base}/v1/text-to-speech/{voice_id or cfg.voice_id}/stream"
        self.usage["tts_requests"] += 1
        self.usage["tts_chars"] += len(text)
        async with self._http.post(url, params={"output_format": "ulaw_8000"}, json=body,
                                   headers=self._headers,
                                   timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                                                 sock_read=cfg.request_timeout_s)) as resp:
            if resp.status != 200:
                raise ElevenLabsError("text-to-speech", resp.status, await _error_detail(resp))
            async for chunk in resp.content.iter_any():
                if chunk:
                    yield chunk

    async def transcribe(self, pcm16_8k) -> str:
        """Batch speech-to-text of one utterance (PCM16 at 8 kHz, sent as WAV)."""
        cfg = self.cfg
        form = aiohttp.FormData()
        form.add_field("model_id", cfg.stt_model)
        if cfg.language_code:
            form.add_field("language_code", cfg.language_code)
        form.add_field("tag_audio_events", "true")
        form.add_field("file", pcm_to_wav(pcm16_8k), filename="utterance.wav", content_type="audio/wav")
        self.usage["stt_requests"] += 1
        self.usage["stt_audio_s"] += len(pcm16_8k) / 8000
        t0 = time.monotonic()
        async with self._http.post(f"{self.base}/v1/speech-to-text", data=form, headers=self._headers,
                                   timeout=aiohttp.ClientTimeout(total=cfg.request_timeout_s)) as resp:
            if resp.status != 200:
                raise ElevenLabsError("speech-to-text", resp.status, await _error_detail(resp))
            body = await resp.json(content_type=None)
        log.debug("stt %.0f ms for %.1f s audio", (time.monotonic() - t0) * 1000, len(pcm16_8k) / 8000)
        return (body.get("text") or "").strip()

    # ---- used by `vc doctor --online` and `vc voices`
    async def get_json(self, path: str) -> dict:
        async with self._http.get(self.base + path, headers=self._headers,
                                  timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise ElevenLabsError(f"GET {path}", resp.status, await _error_detail(resp))
            return await resp.json(content_type=None)
