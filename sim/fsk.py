"""A tiny text-over-audio modem so the simulated call carries real audio.

The fake ElevenLabs "speaks" text as 16-tone FSK in 8 kHz mu-law, and the fake bakery
employee "speaks" the same way. The fake speech-to-text and the employee's "ears"
demodulate it back. Every byte therefore really travels the pipeline (Twilio framing,
VAD, WAV upload, playback buffer, marks, clear), and anything dropped, reordered or
truncated shows up as a CRC failure instead of passing silently.

Packet: SYNC SYNC | len(2 bytes) | utf-8 payload | CRC-16/CCITT(2 bytes); one 4-bit
symbol per 20 ms (160 samples), so ~40 ms per character, roughly 2x speaking speed.
"""
from __future__ import annotations

import math
from array import array

from voice_caller.audio import DTMF_FREQS, pcm_to_ulaw

SR = 8000
SYM = 160
DATA_FREQS = [700 + 150 * k for k in range(16)]
SYNC_FREQ = 450
ALL_FREQS = DATA_FREQS + [SYNC_FREQ]
SYNC_IDX = 16
AMP = 0.3 * 32767
ONSET = AMP * 0.25
_WIN = slice(16, 144)                     # central 128 samples: tolerant to +-2 sample misalignment
_COEF = [2 * math.cos(2 * math.pi * f / SR) for f in ALL_FREQS]
_SYMBOLS = [array("h", [int(AMP * math.sin(2 * math.pi * f * i / SR)) for i in range(SYM)]) for f in ALL_FREQS]
_GAP = array("h", [0] * (SYM * 2))


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def encode_packet_pcm(text: str) -> array:
    payload = text.encode("utf-8")
    frame = len(payload).to_bytes(2, "big") + payload + crc16(payload).to_bytes(2, "big")
    out = array("h")
    out.extend(_SYMBOLS[SYNC_IDX])
    out.extend(_SYMBOLS[SYNC_IDX])
    for b in frame:
        out.extend(_SYMBOLS[b >> 4])
        out.extend(_SYMBOLS[b & 0x0F])
    out.extend(_GAP)
    return out


def encode_ulaw(segments) -> bytes:
    """segments: text strings (one packet each) and ints (pause in ms)."""
    out = bytearray()
    for seg in segments:
        if isinstance(seg, (int, float)):
            out += bytes([0xFF]) * int(SR * seg / 1000)
        elif seg.strip():
            out += pcm_to_ulaw(encode_packet_pcm(seg.strip()))
    return bytes(out)


def _goertzel(x, coef: float) -> float:
    s1 = s2 = 0.0
    for v in x:
        s0 = v + coef * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - coef * s1 * s2


def detect_symbol(window) -> tuple[int, float]:
    x = window[_WIN]
    powers = [_goertzel(x, c) for c in _COEF]
    best = max(range(len(powers)), key=powers.__getitem__)
    energy = sum(v * v for v in x) / len(x)
    return best, energy


class FskDecoder:
    """Streaming demodulator. feed() PCM16; collects .packets (text) and .errors."""

    MIN_ENERGY = (AMP * 0.2) ** 2 / 2

    def __init__(self, on_packet=None, on_error=None):
        self.buf = array("h")
        self.packets: list[str] = []
        self.errors: list[str] = []
        self.on_packet = on_packet
        self.on_error = on_error

    def feed(self, pcm) -> None:
        self.buf.extend(pcm)
        while self._step():
            pass

    def _step(self) -> bool:
        buf = self.buf
        # find onset
        i = 0
        n = len(buf)
        while i < n and abs(buf[i]) < ONSET:
            i += 1
        if i >= n:
            del buf[:max(0, n - 4)]
            return False
        start = max(0, i - 1)
        if start:
            del buf[:start]
        if len(buf) < 2 * SYM:
            return False
        s1, _ = detect_symbol(buf[0:SYM])
        s2, _ = detect_symbol(buf[SYM:2 * SYM])
        if s1 != SYNC_IDX or s2 != SYNC_IDX:
            # not a packet start (noise, DTMF, a truncated tail): skip one symbol
            del buf[:SYM]
            return True
        need_hdr = 2 * SYM + 4 * SYM
        if len(buf) < need_hdr:
            return False
        nib, ok = self._read(2 * SYM, 4)
        if not ok:
            return self._fail("truncated", need_hdr)
        length = (nib[0] << 12) | (nib[1] << 8) | (nib[2] << 4) | nib[3]
        if length > 4000:
            return self._fail("bad length", need_hdr)
        total = need_hdr + (length + 2) * 2 * SYM
        if len(buf) < total:
            # wait for more audio unless the stream already went quiet (truncated by `clear`)
            tail = buf[len(buf) - SYM:] if len(buf) >= need_hdr + SYM else None
            if tail is not None and detect_symbol(tail)[1] < self.MIN_ENERGY:
                return self._fail("truncated", len(buf))
            return False
        nib, ok = self._read(need_hdr, (length + 2) * 2)
        if not ok:
            return self._fail("truncated", total)
        data = bytes((nib[k] << 4) | nib[k + 1] for k in range(0, len(nib), 2))
        payload, crc = data[:-2], int.from_bytes(data[-2:], "big")
        if crc16(payload) != crc:
            return self._fail("crc mismatch", total)
        text = payload.decode("utf-8", "replace")
        self.packets.append(text)
        if self.on_packet:
            self.on_packet(text)
        del buf[:total]
        return True

    def _read(self, offset: int, count: int) -> tuple[list[int], bool]:
        out = []
        for k in range(count):
            a = offset + k * SYM
            sym, energy = detect_symbol(self.buf[a:a + SYM])
            if energy < self.MIN_ENERGY or sym == SYNC_IDX:
                return out, False
            out.append(sym)
        return out, True

    def _fail(self, why: str, consumed: int) -> bool:
        self.errors.append(why)
        if self.on_error:
            self.on_error(why)
        del self.buf[:consumed]
        return True


def decode_all(pcm) -> tuple[list[str], list[str]]:
    d = FskDecoder()
    d.feed(pcm)
    d.feed(array("h", [0] * (SYM * 4)))
    return d.packets, d.errors


# ---- DTMF detection for the fake phone menu

_ROWS = [697, 770, 852, 941]
_COLS = [1209, 1336, 1477, 1633]
_DTMF_COEF = {f: 2 * math.cos(2 * math.pi * f / SR) for f in _ROWS + _COLS}
_DIGIT_BY_PAIR = {v: k for k, v in DTMF_FREQS.items()}


def detect_dtmf(frame) -> str | None:
    """One 20 ms frame -> digit if it clearly holds a row+column tone pair."""
    energy = sum(v * v for v in frame) / len(frame)
    if energy < (0.1 * 32767) ** 2:
        return None
    p = {f: _goertzel(frame, c) for f, c in _DTMF_COEF.items()}
    row = max(_ROWS, key=p.get)
    col = max(_COLS, key=p.get)
    others = sorted((v for f, v in p.items() if f not in (row, col)), reverse=True)
    if p[row] > 10 * others[0] and p[col] > 10 * others[0]:
        return _DIGIT_BY_PAIR.get((row, col))
    return None
