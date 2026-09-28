"""G.711 mu-law codec and small audio helpers. Pure Python, no audioop (gone in 3.13).

Twilio Media Streams carry 8 kHz mono mu-law in 20 ms frames (160 bytes). ElevenLabs
can return exactly that (output_format=ulaw_8000), so the hot path never transcodes
outbound audio; inbound audio is decoded to PCM16 for the VAD and for STT uploads.
"""
from __future__ import annotations

import io
import math
import sys
import wave
from array import array

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
ULAW_SILENCE = 0xFF

_BIAS = 0x84
_CLIP = 32635


def _decode_one(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


def _encode_one(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = max(0, (sample >> 7).bit_length() - 1)
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


ULAW_DECODE = array("h", (_decode_one(i) for i in range(256)))
# indexed by the sample's 16-bit two's-complement pattern (sample & 0xFFFF)
ULAW_ENCODE = bytes(_encode_one(i - 65536 if i >= 32768 else i) for i in range(65536))


def ulaw_to_pcm(data: bytes) -> array:
    dec = ULAW_DECODE
    return array("h", [dec[b] for b in data])


def pcm_to_ulaw(samples) -> bytes:
    enc = ULAW_ENCODE
    return bytes(enc[s & 0xFFFF] for s in samples)


def rms(samples) -> float:
    n = len(samples)
    if not n:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / n)


def silence_ulaw(ms: int) -> bytes:
    return bytes([ULAW_SILENCE]) * (SAMPLE_RATE * ms // 1000)


def pcm_to_wav(samples: array, rate: int = SAMPLE_RATE) -> bytes:
    if sys.byteorder != "little":
        samples = array("h", samples)
        samples.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[array, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit WAV")
        rate = w.getframerate()
        samples = array("h", w.readframes(w.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, rate


def tone(freqs, ms: float, amplitude: float = 0.3, rate: int = SAMPLE_RATE) -> array:
    """Sum of sines, each at `amplitude` of full scale."""
    n = int(rate * ms / 1000)
    amp = amplitude * 32767
    w = [2 * math.pi * f / rate for f in freqs]
    return array("h", [int(sum(amp * math.sin(wi * i) for wi in w)) for i in range(n)])


DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}


def dtmf_ulaw(digits: str, tone_ms: int = 150, gap_ms: int = 100) -> bytes:
    """In-band DTMF (G.711 carries it cleanly). 'w' inserts a 500 ms pause, like Twilio's sendDigits."""
    out = bytearray()
    for d in digits.upper():
        if d == "W":
            out += silence_ulaw(500)
            continue
        if d not in DTMF_FREQS:
            raise ValueError(f"not a DTMF digit: {d!r}")
        out += pcm_to_ulaw(tone(DTMF_FREQS[d], tone_ms, amplitude=0.35))
        out += silence_ulaw(gap_ms)
    return bytes(out)
