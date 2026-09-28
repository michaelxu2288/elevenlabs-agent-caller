"""Turn detection on the callee's audio: an energy VAD with an adaptive noise floor.

Works on 20 ms PCM16 frames and counts audio time in frames, never wall-clock, so
the same thresholds hold whether audio arrives in real time or faster (simulation).

Events, in the order they can occur within one utterance:
  start      60 ms of speech seen: someone started talking
  voiced     every speech frame; carries voiced_ms so far (drives barge-in)
  speculate  speculative_stt_ms of silence: transcribe now, the turn is probably over
  resume     speech came back after a speculate: throw that transcript away
  end        endpoint_silence_ms of silence (or max length): the utterance, as PCM16
  discard    it was too short to be speech (click, cough, line noise)
"""
from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass, field

from .audio import FRAME_MS, rms
from .config import TurnConfig


@dataclass
class VadEvent:
    kind: str
    t_ms: int                      # audio time of the frame that produced it
    voiced_ms: int = 0
    audio: array | None = None     # PCM16 for speculate/end
    speech_end_ms: int = 0         # audio time of the last voiced frame (end/speculate)
    start_ms: int = 0              # audio time of the utterance's first voiced frame


@dataclass
class VadStats:
    frames: int = 0
    speech_frames: int = 0
    max_rms: float = 0.0
    speech_rms_sum: float = 0.0
    noise_floor: float = 0.0
    utterances: int = 0
    discarded: int = 0

    def as_dict(self) -> dict:
        avg = self.speech_rms_sum / self.speech_frames if self.speech_frames else 0.0
        return {"frames": self.frames, "speech_frames": self.speech_frames,
                "avg_speech_rms": round(avg), "max_rms": round(self.max_rms),
                "final_noise_floor": round(self.noise_floor), "utterances": self.utterances,
                "discarded_blips": self.discarded}


class TurnDetector:
    TRAILING_KEEP_FRAMES = 10  # keep 200 ms of trailing silence in the audio sent to STT

    def __init__(self, cfg: TurnConfig):
        self.cfg = cfg
        self.noise_floor = 100.0
        self.threshold_scale = 1.0     # raised by the session while the agent talks (echo guard)
        self.stats = VadStats()
        self._frame_no = 0
        self._preroll: deque = deque(maxlen=max(1, cfg.preroll_ms // FRAME_MS))
        self._onset_run = 0
        self._reset_utterance()

    def _reset_utterance(self) -> None:
        self.in_speech = False
        self._frames: list[array] = []
        self._voiced = 0
        self._silence_run = 0
        self._last_voiced_idx = 0
        self._last_voiced_t = 0
        self._start_t = 0
        self._speculated = False

    @property
    def threshold(self) -> float:
        base = max(self.cfg.min_speech_rms, self.noise_floor * self.cfg.speech_to_noise_ratio)
        return base * self.threshold_scale

    def _utterance_audio(self) -> array:
        keep = self._last_voiced_idx + 1 + self.TRAILING_KEEP_FRAMES
        out = array("h")
        for f in self._frames[:keep]:
            out.extend(f)
        return out

    def process(self, pcm: array) -> list[VadEvent]:
        t = self._frame_no * FRAME_MS
        self._frame_no += 1
        level = rms(pcm)
        st = self.stats
        st.frames += 1
        st.max_rms = max(st.max_rms, level)
        is_speech = level >= self.threshold
        if is_speech:
            st.speech_frames += 1
            st.speech_rms_sum += level
        events: list[VadEvent] = []

        if not self.in_speech:
            self._preroll.append(pcm)
            if is_speech:
                self._onset_run += 1
                if self._onset_run >= self.cfg.start_frames:
                    self.in_speech = True
                    self._frames = list(self._preroll)
                    self._voiced = self._onset_run
                    self._last_voiced_idx = len(self._frames) - 1
                    self._last_voiced_t = t
                    self._start_t = t - (self._onset_run - 1) * FRAME_MS
                    self._onset_run = 0
                    self._preroll.clear()
                    events.append(VadEvent("start", t, self._voiced * FRAME_MS))
            else:
                self._onset_run = 0
                # track the line's background level only while nobody is talking
                self.noise_floor += (max(level, 10.0) - self.noise_floor) * 0.05
                st.noise_floor = self.noise_floor
            return events

        self._frames.append(pcm)
        if is_speech:
            self._voiced += 1
            self._last_voiced_idx = len(self._frames) - 1
            self._last_voiced_t = t
            if self._speculated:
                self._speculated = False
                events.append(VadEvent("resume", t, self._voiced * FRAME_MS))
            self._silence_run = 0
            events.append(VadEvent("voiced", t, self._voiced * FRAME_MS))
        else:
            self._silence_run += 1

        voiced_ms = self._voiced * FRAME_MS
        silence_ms = self._silence_run * FRAME_MS
        long_enough = voiced_ms >= self.cfg.min_utterance_ms
        forced = len(self._frames) * FRAME_MS >= self.cfg.max_utterance_ms

        if (not self._speculated and long_enough and not forced
                and silence_ms >= self.cfg.speculative_stt_ms and silence_ms < self.cfg.endpoint_silence_ms):
            self._speculated = True
            events.append(VadEvent("speculate", t, voiced_ms, self._utterance_audio(), self._last_voiced_t,
                                   self._start_t))

        if silence_ms >= self.cfg.endpoint_silence_ms or forced:
            if long_enough:
                st.utterances += 1
                events.append(VadEvent("end", t, voiced_ms, self._utterance_audio(), self._last_voiced_t,
                                       self._start_t))
            else:
                st.discarded += 1
                events.append(VadEvent("discard", t, voiced_ms))
            self._reset_utterance()
        return events
