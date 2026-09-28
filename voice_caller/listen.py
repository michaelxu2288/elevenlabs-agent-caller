"""Listen-in leg: a second call to your own phone that hears both sides of a live call.

The callee's inbound frames arrive every 20 ms and set the pace. Whatever the agent sends
to the callee is queued and mixed into those frames, so the listener hears the call the way
the callee does. The listener's own microphone is ignored.
"""
from __future__ import annotations

import logging
from array import array

from .audio import dtmf_ulaw, pcm_to_ulaw, ulaw_to_pcm

log = logging.getLogger("listen")


def mix_ulaw(a: bytes, b: bytes) -> bytes:
    pa, pb = ulaw_to_pcm(a), ulaw_to_pcm(b)
    out = array("h", pa)
    for i in range(min(len(pa), len(pb))):
        out[i] = max(-32768, min(32767, pa[i] + pb[i]))
    return pcm_to_ulaw(out)


class _PrefixedRecorder:
    def __init__(self, rec):
        self._rec = rec

    def event(self, kind: str, **fields) -> None:
        self._rec.event("listener_" + kind, **fields)


class ListenLeg:
    def __init__(self, rec):
        self.rec = _PrefixedRecorder(rec)
        self.sender = None
        self.agent = bytearray()

    async def on_start(self, start: dict, sender) -> None:
        self.sender = sender
        self.rec.event("connected")
        await self._send(dtmf_ulaw("1", tone_ms=120, gap_ms=0))

    async def on_media(self, ulaw: bytes) -> None:
        pass

    def on_mark(self, name: str) -> None:
        pass

    def on_dtmf(self, digit: str) -> None:
        pass

    async def on_stop(self, reason: str = "") -> None:
        if self.sender:
            self.rec.event("stopped", reason=reason)
        self.sender = None
        self.agent.clear()

    def agent_audio(self, ulaw: bytes) -> None:
        if self.sender:
            self.agent.extend(ulaw)

    def clear(self) -> None:
        self.agent.clear()

    async def callee_frame(self, ulaw: bytes) -> None:
        if not self.sender:
            return
        mine = bytes(self.agent[:len(ulaw)])
        del self.agent[:len(ulaw)]
        await self._send(mix_ulaw(ulaw, mine) if mine else ulaw)

    async def _send(self, ulaw: bytes) -> None:
        try:
            await self.sender.audio(ulaw)
        except (ConnectionError, RuntimeError) as e:
            log.warning("listen leg dropped: %s", e)
            self.sender = None


class TeeSender:
    """Stands in for the callee's MediaSender and copies the agent's audio to the listener."""

    def __init__(self, sender, leg: ListenLeg):
        self._sender = sender
        self._leg = leg
        self.stream_sid = sender.stream_sid

    @property
    def clears(self) -> int:
        return self._sender.clears

    @property
    def bytes_sent(self) -> int:
        return self._sender.bytes_sent

    async def audio(self, ulaw: bytes) -> None:
        self._leg.agent_audio(ulaw)
        await self._sender.audio(ulaw)

    async def mark(self, name: str) -> None:
        await self._sender.mark(name)

    async def clear(self) -> None:
        self._leg.clear()
        await self._sender.clear()
