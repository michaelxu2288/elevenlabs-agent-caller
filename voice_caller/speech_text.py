"""Turning the brain's streamed text into speakable chunks and control actions.

The brain writes plain spoken words plus control markers:
  [[WAIT]]          say nothing this turn (on hold, music, half a sentence)
  [[END_CALL]]      hang up once everything before it has been heard
  [[DTMF:12#]]      press keys on a phone menu (w = half-second pause)

Text is cut into sentence-sized chunks as it streams so the first sentence can go to
TTS while the rest is still being generated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MARKER_RE = re.compile(r"\[\[\s*(END_CALL|WAIT|DTMF\s*:\s*[0-9A-Da-d*#wW]+)\s*\]\]")
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "approx", "no", "ave"}
# a sentence ends at . ! ? (plus closing quotes) followed by whitespace and a capital/quote/digit
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s+(?=[A-Z0-9\"'(])")
_FORCE_SPLIT_CHARS = 160


@dataclass
class Speak:
    text: str


@dataclass
class Control:
    action: str          # "wait", "end_call", "dtmf"
    arg: str = ""


def parse_marker(body: str) -> Control:
    body = re.sub(r"\s+", "", body).upper()
    if body == "WAIT":
        return Control("wait")
    if body == "END_CALL":
        return Control("end_call")
    return Control("dtmf", body.split(":", 1)[1])


def clean_for_tts(text: str) -> str:
    """Remove things a phone voice should not read out: markdown, stage directions, stray brackets."""
    text = re.sub(r"<(thinking|heard|event)>.*?</\1>", " ", text, flags=re.S)
    text = re.sub(r"</?[A-Za-z_][^<>]{0,40}>", " ", text)    # stray tags
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)             # **bold** -> bold
    text = re.sub(r"(?<!\w)\*[^*\n]{1,40}\*(?!\w)", " ", text)  # *laughs*
    text = re.sub(r"[*_`#>|~]", " ", text)
    text = re.sub(r"\[\[[^\]]*\]\]", " ", text)            # malformed markers
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _split_sentences(buf: str, final: bool) -> tuple[list[str], str]:
    out = []
    start = 0
    for m in _SENTENCE_END.finditer(buf):
        end = m.end()
        candidate = buf[start:end]
        last_word = re.findall(r"([A-Za-z]+)[.!?][\"')\]]*\s*$", candidate)
        if last_word and last_word[0].lower() in _ABBREV:
            continue
        # skip single-letter initials / "a.m." style abbreviations
        if re.search(r"\b[A-Za-z]\.[\"')\]]*\s*$", candidate):
            continue
        out.append(candidate.strip())
        start = end
    rest = buf[start:]
    if len(rest) > _FORCE_SPLIT_CHARS:
        cut = max(rest.rfind(", ", 0, _FORCE_SPLIT_CHARS), rest.rfind("; ", 0, _FORCE_SPLIT_CHARS))
        if cut > 40:
            out.append(rest[:cut + 1].strip())
            rest = rest[cut + 2:]
    if final and rest.strip():
        out.append(rest.strip())
        rest = ""
    return [s for s in out if s], rest


_CLAUSE = re.compile(r"[,;:\u2014]\s+")


class SpeechChunker:
    """feed() streamed deltas, get back Speak/Control items in order; finish() at end of turn.

    With early_first=True the reply's first clause ("Hi Maria,") is released as soon as
    it is complete, so the voice starts while the rest of the sentence is generated."""

    def __init__(self, early_first: bool = False):
        self._buf = ""
        self.full_text = ""
        self._early = early_first
        self._emitted = False

    def feed(self, delta: str) -> list:
        self._buf += delta
        self.full_text += delta
        items = self._drain(final=False)
        if self._early and not self._emitted and "[" not in self._buf and "<" not in self._buf:
            m = next((c for c in _CLAUSE.finditer(self._buf) if len(self._buf[:c.start()].strip()) >= 8), None)
            if m:
                head = clean_for_tts(self._buf[:m.end()])
                if head:
                    items.append(Speak(head))
                self._buf = self._buf[m.end():]
        self._emitted = self._emitted or any(isinstance(i, Speak) for i in items)
        return items

    def finish(self) -> list:
        return self._drain(final=True)

    def _drain(self, final: bool) -> list:
        items: list = []
        while True:
            m = MARKER_RE.search(self._buf)
            if not m:
                break
            items += self._text_items(self._buf[:m.start()], final=True)
            items.append(parse_marker(m.group(1)))
            self._buf = self._buf[m.end():]
        # never speak a marker that is still arriving: hold back from an unclosed "[["
        hold = self._buf.find("[[")
        if hold == -1 and self._buf.endswith("["):
            hold = len(self._buf) - 1
        if final:
            if hold != -1:
                self._buf = self._buf[:hold]  # a truncated marker is dropped, not spoken
            items += self._text_items(self._buf, final=True)
            self._buf = ""
        elif hold != -1:
            head, tail = self._buf[:hold], self._buf[hold:]
            sentences, rest = _split_sentences(head, final=False)
            items += [Speak(t) for s in sentences if (t := clean_for_tts(s))]
            self._buf = rest + tail
        else:
            sentences, self._buf = _split_sentences(self._buf, final=False)
            items += [Speak(t) for s in sentences if (t := clean_for_tts(s))]
        return items

    @staticmethod
    def _text_items(text: str, final: bool) -> list:
        sentences, rest = _split_sentences(text, final=final)
        return [Speak(t) for s in sentences if (t := clean_for_tts(s))]


def strip_markers(text: str) -> str:
    return re.sub(r"\s+", " ", MARKER_RE.sub(" ", text)).strip()
