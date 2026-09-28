"""One live call: callee audio -> turn detection -> STT -> brain -> TTS -> audio back.

Turn-taking rules
  * Their utterance ends (endpoint silence) -> transcribe -> ask the brain -> speak the
    reply sentence by sentence as it streams.
  * They talk while a reply is still being generated (nothing audible yet): they weren't
    done, so the reply is dropped and the brain is told it was never spoken.
  * They talk over audible agent speech for barge_in_min_ms: stop (Twilio `clear`),
    cancel the reply, and tell the brain exactly how much of it they heard.
  * Shorter sounds while the agent talks ("mm-hm") are backchannels: passed along as a
    note with the next turn instead of cutting the agent off.
  * Silence: nudge the brain after agent_speaks_first_after_s (nobody greeted) and after
    silence_nudge_s of dead air; it decides whether to speak or [[WAIT]].
Clocks: turn logic uses inbound audio time (Twilio streams 50 frames/s, silence
included), so behaviour is identical in real calls and in faster-than-real-time tests.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from array import array
from collections import deque
from dataclasses import dataclass, field

from . import prompts
from .audio import FRAME_SAMPLES, dtmf_ulaw, ulaw_to_pcm
from .brain import BrainError
from .config import Config
from .elevenlabs import ElevenLabsError
from .listen import TeeSender
from .recorder import CallRecorder
from .speech_text import Control, Speak, SpeechChunker
from .task import CallTask
from .vad import TurnDetector, VadEvent

log = logging.getLogger("session")


class MediaSender:
    """Writes Twilio Media Stream messages (media / mark / clear) for one stream."""

    def __init__(self, ws, stream_sid: str, frame_bytes: int = 160):
        self._ws = ws
        self.stream_sid = stream_sid
        self.frame = frame_bytes
        self._lock = asyncio.Lock()
        self.bytes_sent = 0
        self.clears = 0

    async def _send(self, obj: dict) -> None:
        await self._ws.send_str(json.dumps(obj))

    async def audio(self, ulaw: bytes) -> None:
        async with self._lock:
            for i in range(0, len(ulaw), self.frame):
                part = ulaw[i:i + self.frame]
                await self._send({"event": "media", "streamSid": self.stream_sid,
                                  "media": {"payload": base64.b64encode(part).decode("ascii")}})
                self.bytes_sent += len(part)

    async def mark(self, name: str) -> None:
        async with self._lock:
            await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def clear(self) -> None:
        async with self._lock:
            self.clears += 1
            await self._send({"event": "clear", "streamSid": self.stream_sid})


@dataclass
class Chunk:
    name: str
    text: str
    reply_id: int
    ms: float = 0.0


class Playout:
    """What we've sent to Twilio's playback buffer and how far it has played (via marks)."""

    def __init__(self):
        self.pending: deque[Chunk] = deque()
        self.played: list[Chunk] = []
        self.head_started: float | None = None
        self.idle = asyncio.Event()
        self.idle.set()

    def begin(self, chunk: Chunk) -> None:
        if not self.pending:
            self.head_started = time.monotonic()
        self.pending.append(chunk)
        self.idle.clear()

    def mark_received(self, name: str) -> Chunk | None:
        if not any(c.name == name for c in self.pending):
            return None  # a mark for audio we already cleared
        while self.pending:
            c = self.pending.popleft()
            self.played.append(c)
            if c.name == name:
                break
        self.head_started = time.monotonic() if self.pending else None
        if not self.pending:
            self.idle.set()
        return c

    def heard_text(self, reply_id: int) -> str:
        """Everything of this reply that has played, plus the played share of the current chunk."""
        words = [c.text for c in self.played if c.reply_id == reply_id]
        if self.pending and self.head_started is not None:
            head = self.pending[0]
            if head.reply_id == reply_id and head.ms > 0:
                frac = min(1.0, (time.monotonic() - self.head_started) * 1000 / head.ms)
                w = head.text.split()
                words.append(" ".join(w[:int(len(w) * frac)]))
        return " ".join(x for x in words if x).strip()

    def clear(self) -> None:
        self.pending.clear()
        self.head_started = None
        self.idle.set()


@dataclass
class Utterance:
    stt: asyncio.Task
    speech_end_ms: int          # inbound audio time of their last voiced frame
    voiced_ms: int
    t_start: float              # recorder time they started talking (for transcript order)
    t_wall: float = field(default_factory=time.monotonic)


class CallSession:
    def __init__(self, *, task: CallTask, cfg: Config, brain, voice, recorder: CallRecorder, hangup):
        self.task = task
        self.cfg = cfg
        self.brain = brain
        self.voice = voice                  # ElevenLabs (or the simulator's stand-in)
        self.rec = recorder
        self._hangup_cb = hangup            # async () -> None, asks Twilio to end the call
        self.detector = TurnDetector(cfg.turns)
        self.playout = Playout()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.notes: list[tuple[str, object]] = []
        self.sender: MediaSender | None = None
        self.listener = None
        self.call_sid = ""
        self.stream_sid = ""
        self.reply_task: asyncio.Task | None = None
        self.reply_id = 0
        self.reply_audio_sent = False
        self.spec_stt: asyncio.Task | None = None
        self.audio_ms = 0
        self._pcm_buf = array("h")
        self.last_activity_ms = 0
        self.anyone_spoke = False
        self._nudged_first = False
        self._next_nudge_ms = int(cfg.turns.silence_nudge_s * 1000)
        self._time_warned = False
        self.turns = 0
        self._filler_audio = b""
        self._reply_started_t = 0.0
        self._metric: dict = {}
        self.stt_failures = 0
        self.metrics: list[dict] = []
        self.started = asyncio.Event()
        self.ended = asyncio.Event()
        self.ending = False
        self.end_reason = ""
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ stream events

    async def on_start(self, start: dict, sender: MediaSender) -> None:
        self.sender = TeeSender(sender, self.listener) if self.listener else sender
        self.call_sid = start.get("callSid", "")
        self.stream_sid = sender.stream_sid
        fmt = start.get("mediaFormat") or {}
        if fmt and (fmt.get("encoding") != "audio/x-mulaw" or int(fmt.get("sampleRate", 8000)) != 8000):
            log.warning("unexpected media format %s", fmt)
        self.rec.event("stream_started", call_sid=self.call_sid, media_format=fmt)
        self.started.set()
        self._tasks.append(asyncio.create_task(self._turn_loop(), name="turn-loop"))

    async def on_media(self, ulaw: bytes) -> None:
        if self.ended.is_set():
            return
        if self.listener:
            await self.listener.callee_frame(ulaw)
        self._pcm_buf.extend(ulaw_to_pcm(ulaw))
        while len(self._pcm_buf) >= FRAME_SAMPLES:
            frame = self._pcm_buf[:FRAME_SAMPLES]
            del self._pcm_buf[:FRAME_SAMPLES]
            self.audio_ms += 20
            self.detector.threshold_scale = self.cfg.turns.echo_guard_ratio if self.playout.pending else 1.0
            for ev in self.detector.process(frame):
                await self._on_vad(ev)
            if self.audio_ms % 500 == 0:
                self._check_timers()

    def on_mark(self, name: str) -> None:
        self.playout.mark_received(name)

    def on_dtmf(self, digit: str) -> None:
        self.rec.event("callee_pressed_key", digit=digit)

    async def on_stop(self, reason: str = "stream stopped") -> None:
        if self.ended.is_set():
            return
        self.ended.set()
        self.end_reason = self.end_reason or reason
        self.rec.event("stream_ended", reason=reason)
        for t in [self.reply_task, self.spec_stt, *self._tasks]:
            if t and not t.done():
                t.cancel()

    # ------------------------------------------------------------ turn detection

    def _reply_active(self) -> bool:
        return self.reply_task is not None and not self.reply_task.done()

    async def _on_vad(self, ev: VadEvent) -> None:
        if self.ending:
            return
        tc = self.cfg.turns
        if ev.kind == "start":
            self.anyone_spoke = True
            self.last_activity_ms = self.audio_ms
        elif ev.kind == "voiced":
            self.last_activity_ms = self.audio_ms
            if self._reply_active():
                audible = bool(self.playout.pending) or self.reply_audio_sent
                if ev.voiced_ms >= (tc.barge_in_min_ms if audible else tc.resume_cancel_ms):
                    await self._interrupt()
        elif ev.kind == "speculate":
            self.spec_stt = asyncio.create_task(self._transcribe(ev.audio))
        elif ev.kind == "resume":
            if self.spec_stt:
                self.spec_stt.cancel()
                self.spec_stt = None
        elif ev.kind == "end":
            stt = self.spec_stt or asyncio.create_task(self._transcribe(ev.audio))
            self.spec_stt = None
            utt = Utterance(stt, ev.speech_end_ms, ev.voiced_ms,
                            t_start=max(0.0, self.rec.now() - (self.audio_ms - ev.start_ms) / 1000))
            if self._reply_active() and (self.playout.pending or self.reply_audio_sent):
                self.notes.append(("backchannel", utt))
            else:
                self.inbox.put_nowait(utt)

    async def _transcribe(self, pcm) -> str:
        try:
            text = await self.voice.transcribe(pcm)
            self.stt_failures = 0
            return text
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one bad utterance must not end the call
            self.stt_failures += 1
            log.warning("speech-to-text failed (%d in a row): %s", self.stt_failures, e)
            self.rec.event("stt_error", error=str(e))
            if self.stt_failures >= 3 and not self.ending:
                self.inbox.put_nowait(RuntimeError(f"speech-to-text keeps failing: {e}"))
            return ""

    def _check_timers(self) -> None:
        tc = self.cfg.turns
        limit_ms = self.cfg.twilio.time_limit_s * 1000
        if not self._time_warned and self.audio_ms >= 0.8 * limit_ms:
            self._time_warned = True
            self.notes.append(("event", f"The call has lasted {self.audio_ms // 60000} minutes and will be "
                                        f"cut off at {limit_ms // 60000}. Wrap up now."))
        busy = (self.ending or self._reply_active() or self.playout.pending or self.detector.in_speech
                or not self.inbox.empty())
        if busy:
            self.last_activity_ms = self.audio_ms
            self._next_nudge_ms = int(tc.silence_nudge_s * 1000)
            return
        idle = self.audio_ms - self.last_activity_ms
        if not self.anyone_spoke:
            if not self._nudged_first and idle >= tc.agent_speaks_first_after_s * 1000:
                self._nudged_first = True
                self.anyone_spoke = True
                self.inbox.put_nowait(f"The call was answered {idle // 1000} seconds ago and nobody has "
                                      "spoken yet.")
        elif idle >= self._next_nudge_ms:
            self.inbox.put_nowait(f"No one has said anything for {idle // 1000} seconds.")
            self._next_nudge_ms = idle + max(int(tc.silence_nudge_s * 1000), idle)

    # ------------------------------------------------------------ replies

    async def _interrupt(self) -> None:
        task = self.reply_task
        if not task or task.done():
            return
        audible = bool(self.playout.pending) or self.reply_audio_sent
        heard = self.playout.heard_text(self.reply_id)
        task.cancel()
        if self.playout.pending:
            await self.sender.clear()
            self.playout.clear()
        if audible:
            self.rec.say("agent", (heard + " --").strip(), interrupted=True, t=self._reply_started_t)
            self.notes.append(("interrupted", heard))
            self.rec.event("barge_in", heard=heard)
        else:
            self.notes.append(("unsent", ""))
            self.rec.event("reply_dropped", why="they kept talking")

    async def _format_notes(self) -> list[str]:
        out = []
        notes, self.notes = self.notes, []
        for kind, val in notes:
            if kind == "interrupted":
                out.append(prompts.event(
                    "They started talking over you, so you stopped. They only heard: \"" + val + "\"."
                    if val else "They started talking just as you began, so they heard none of your reply."))
            elif kind == "unsent":
                out.append(prompts.event("They kept talking, so your previous reply was not said. "
                                         "Respond to everything they've said."))
            elif kind == "backchannel":
                try:
                    text = await asyncio.wait_for(asyncio.shield(val.stt), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    text = ""
                if text:
                    self.rec.say("them", text, backchannel=True, t=val.t_start)
                    out.append(prompts.event(f"While you were speaking they said: \"{text}\"."))
            elif kind == "event":
                out.append(prompts.event(str(val)))
        return out

    async def _turn_loop(self) -> None:
        try:
            await self._turn_loop_body()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a bug here must end the call, not leave it mute
            log.exception("turn loop crashed")
            await self._fail_gracefully(e)

    async def _turn_loop_body(self) -> None:
        while not self.ended.is_set():
            items = [await self.inbox.get()]
            while not self.inbox.empty():
                items.append(self.inbox.get_nowait())
            parts, last_utt, fatal = [], None, None
            for it in items:
                if isinstance(it, Utterance):
                    try:
                        text = await it.stt
                    except asyncio.CancelledError:
                        if not it.stt.cancelled():
                            raise              # we are being cancelled, not the transcription
                        text = ""
                    if text:
                        self.rec.say("them", text, t=it.t_start)
                        parts.append(prompts.heard(text))
                        last_utt = it
                elif isinstance(it, Exception):
                    fatal = it
                else:
                    self.rec.event("nudge", text=it)
                    parts.append(prompts.event(it))
            if fatal:
                await self._fail_gracefully(fatal)
                return
            if not parts:
                if not any(kind == "interrupted" for kind, _ in self.notes):
                    continue  # nothing new was said; pending notes ride along with the next turn
                parts.append(prompts.event("Whatever interrupted you was not speech; nothing was said."))
            notes = await self._format_notes()
            self.turns += 1
            if self.turns >= self.cfg.safety.max_turns:
                parts.append(prompts.event("This call has gone on too long. Politely wrap up and end it now."))
            message = "\n".join(notes + parts)
            self.reply_task = asyncio.create_task(self._reply(message, last_utt), name=f"reply-{self.turns}")
            await asyncio.wait([self.reply_task])
            if self.reply_task.cancelled() or self.ended.is_set():
                continue
            exc = self.reply_task.exception()
            if exc:
                await self._fail_gracefully(exc)
                return

    async def _reply(self, message: str, utt: Utterance | None) -> None:
        self.reply_id += 1
        rid = self.reply_id
        self.reply_audio_sent = False
        self._reply_started_t = self.rec.now()
        m = {"reply": rid, "trigger": "speech" if utt else "event"}
        if utt:
            m["stt_ms"] = int((time.monotonic() - utt.t_wall) * 1000)
        self._metric = m
        ask = asyncio.ensure_future(self.brain.ask(message))
        try:
            turn = await asyncio.shield(ask)
        except asyncio.CancelledError:
            # cancelled while the message was being handed over: cancel the turn once it exists
            ask.add_done_callback(lambda f: f.cancelled() or f.exception() or self.brain.cancel(f.result()))
            raise
        chunker = SpeechChunker(early_first=self.cfg.turns.early_first_chunk)
        q: asyncio.Queue = asyncio.Queue()

        async def produce():
            try:
                async for delta in turn.deltas():
                    for item in chunker.feed(delta):
                        q.put_nowait(item)
                for item in chunker.finish():
                    q.put_nowait(item)
            finally:
                q.put_nowait(None)

        prod = asyncio.create_task(produce())
        filler = (asyncio.create_task(self._stall_filler(rid, turn))
                  if utt and self._filler_audio and self.cfg.turns.stall_filler_s > 0 else None)
        spoken: list[str] = []
        end_call = False
        pressed = False
        try:
            n = 0
            while (item := await q.get()) is not None:
                n += 1
                if filler:
                    filler.cancel()
                    filler = None
                if isinstance(item, Speak):
                    await self._speak(rid, n, item.text, " ".join(spoken), utt)
                    spoken.append(item.text)
                elif isinstance(item, Control) and item.action == "dtmf":
                    self.rec.event("dtmf_sent", digits=item.arg)
                    pressed = True
                    await self._play(rid, n, dtmf_ulaw(item.arg), f"[pressed {item.arg}]", utt)
                elif isinstance(item, Control) and item.action == "end_call":
                    end_call = True
            await prod
            await self.playout.idle.wait()
        except asyncio.CancelledError:
            prod.cancel()
            self.brain.cancel(turn)
            raise
        finally:
            if filler:
                filler.cancel()
        m["brain_ttft_ms"] = turn.ttft_ms
        self.metrics.append(m)
        self.rec.event("turn_metrics", **m)
        if spoken:
            self.rec.say("agent", " ".join(spoken), t=self._reply_started_t)
        elif not end_call and not pressed:
            self.rec.event("agent_waits")
        if end_call:
            await self._hang_up("agent ended the call")

    async def prepare(self) -> None:
        """Before dialing: render the stall filler (proves the TTS key, scope and voice) and
        transcribe it back (proves speech-to-text). Raises if either is broken, so a bad
        setup never costs a phone call."""
        text = self.cfg.turns.stall_filler_text or "One moment."
        audio = b"".join([c async for c in self.voice.tts_stream(text)])
        if len(audio) < 800:
            raise ElevenLabsError("text-to-speech preflight", 200, f"only {len(audio)} bytes of audio")
        heard = await self.voice.transcribe(ulaw_to_pcm(audio))
        self.rec.event("voice_preflight", tts_ms=int(len(audio) / 8), stt_heard=heard)
        log.info("voice preflight ok: TTS %.1f s of audio, STT heard %r", len(audio) / 8000, heard)
        if self.cfg.turns.stall_filler_s > 0 and self.cfg.turns.stall_filler_text:
            self._filler_audio = audio

    async def _stall_filler(self, rid: int, turn) -> None:
        """If the brain hasn't produced a word after stall_filler_s, say the filler so the
        line isn't dead. It never overlaps the real reply: the first real item cancels it."""
        await asyncio.sleep(self.cfg.turns.stall_filler_s)
        if turn.t_first is None and not self.playout.pending and self.sender:
            self.rec.say("agent", self.cfg.turns.stall_filler_text, filler=True, t=self.rec.now())
            self.rec.event("stall_filler", after_s=self.cfg.turns.stall_filler_s)
            # shielded: once started it must finish and send its mark, or playout never drains
            await asyncio.shield(self._play(rid, 0, self._filler_audio, self.cfg.turns.stall_filler_text, None))

    async def _speak(self, rid: int, n: int, text: str, previous: str, utt: Utterance | None) -> None:
        chunk = Chunk(f"r{rid}.{n}", text, rid)
        t0 = time.monotonic()
        for attempt in (1, 2):
            started = False
            try:
                async for audio in self.voice.tts_stream(text, previous_text=previous):
                    if not started:
                        started = True
                        self._first_audio(chunk, t0, utt)
                    chunk.ms += len(audio) / 8
                    await self.sender.audio(audio)
                break
            except ElevenLabsError as e:
                if started or attempt == 2 or e.status not in (429, 500, 502, 503, 504):
                    raise
                log.warning("TTS retry after: %s", e)
                await asyncio.sleep(0.3)
        if started:
            await self.sender.mark(chunk.name)

    async def _play(self, rid: int, n: int, ulaw: bytes, label: str, utt) -> None:
        chunk = Chunk(f"r{rid}.{n}", label, rid, ms=len(ulaw) / 8)
        self._first_audio(chunk, time.monotonic(), utt)
        await self.sender.audio(ulaw)
        await self.sender.mark(chunk.name)

    def _first_audio(self, chunk: Chunk, t0: float, utt: Utterance | None) -> None:
        self.playout.begin(chunk)
        if not self.reply_audio_sent:
            self.reply_audio_sent = True
            self._reply_started_t = self.rec.now()
            self._metric["tts_first_audio_ms"] = int((time.monotonic() - t0) * 1000)
            if utt:
                self._metric["response_gap_ms"] = self.audio_ms - utt.speech_end_ms

    async def _hang_up(self, reason: str) -> None:
        if self.ending:
            return
        self.ending = True
        self.end_reason = reason
        self.rec.event("hangup_requested", reason=reason)
        try:
            await self._hangup_cb()
        except Exception as e:  # noqa: BLE001
            log.error("hang-up request failed: %s", e)
            self.rec.event("hangup_failed", error=str(e))

    async def _fail_gracefully(self, exc: BaseException) -> None:
        log.error("call failing: %s", exc)
        self.rec.event("error", error=str(exc), error_type=type(exc).__name__)
        if self.playout.pending and self.sender:
            try:
                await self.sender.clear()
            except Exception:  # noqa: BLE001
                pass
            self.playout.clear()
        if not isinstance(exc, ElevenLabsError) and self.sender and not self.ending:
            try:
                sorry = (f"Sorry, I'm having a technical problem on my end. {self.task.on_behalf_of} "
                         "will call you back. Goodbye.")
                self.reply_id += 1
                self.reply_audio_sent = False
                self._reply_started_t = self.rec.now()
                self._metric = {}
                await asyncio.wait_for(self._speak(self.reply_id, 1, sorry, "", None), timeout=15)
                await asyncio.wait_for(self.playout.idle.wait(), timeout=15)
                self.rec.say("agent", sorry, t=self._reply_started_t)
            except Exception as e:  # noqa: BLE001
                log.error("could not say goodbye: %s", e)
        await self._hang_up(f"error: {type(exc).__name__}")

    def stats(self) -> dict:
        gaps = sorted(m["response_gap_ms"] for m in self.metrics if "response_gap_ms" in m)
        ttft = sorted(m["brain_ttft_ms"] for m in self.metrics if m.get("brain_ttft_ms"))

        def pct(xs, p):
            return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None
        return {
            "turns": self.turns, "audio_seconds": round(self.audio_ms / 1000, 1),
            "response_gap_ms": {"p50": pct(gaps, 0.5), "p90": pct(gaps, 0.9), "max": gaps[-1] if gaps else None},
            "brain_ttft_ms": {"p50": pct(ttft, 0.5), "max": ttft[-1] if ttft else None},
            "barge_ins": sum(1 for e in self.rec.entries if e.get("kind") == "barge_in"),
            "clears_sent": self.sender.clears if self.sender else 0,
            "vad": self.detector.stats.as_dict(),
            "end_reason": self.end_reason,
        }
