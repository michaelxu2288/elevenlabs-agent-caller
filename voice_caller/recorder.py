"""Per-call record: calls/<timestamp>-<business>/ with events.jsonl (written live, so a
crash still leaves a trail), transcript.md, outcome.json and call.json. Everything
written passes through secret redaction."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .logs import redact


class CallRecorder:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        self.entries: list[dict] = []
        self._fh = open(self.dir / "events.jsonl", "a", encoding="utf-8")

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def _append(self, entry: dict) -> None:
        self.entries.append(entry)
        if not self._fh.closed:
            self._fh.write(redact(json.dumps(entry, ensure_ascii=False)) + "\n")
            self._fh.flush()

    def event(self, kind: str, **data) -> None:
        self._append({"t": self.now(), "kind": kind, **data})

    def say(self, who: str, text: str, t: float | None = None, **meta) -> None:
        self._append({"t": round(self.now() if t is None else t, 2), "kind": "say", "who": who,
                      "text": text, **meta})

    def lines(self) -> list[dict]:
        return sorted((e for e in self.entries if e["kind"] == "say"), key=lambda e: e["t"])

    def transcript_text(self) -> str:
        out = []
        for e in self.lines():
            mm, ss = divmod(int(e["t"]), 60)
            tag = " (interrupted)" if e.get("interrupted") else " (while agent spoke)" if e.get("backchannel") else ""
            out.append(f"[{mm:02d}:{ss:02d}] {'AGENT' if e['who'] == 'agent' else 'THEM'}{tag}: {e['text']}")
        return "\n".join(out)

    def write_json(self, name: str, obj) -> Path:
        path = self.dir / name
        path.write_text(redact(json.dumps(obj, indent=2, ensure_ascii=False, default=str)) + "\n",
                        encoding="utf-8")
        return path

    def write_transcript(self, title: str, facts: dict, outcome: dict | None) -> Path:
        md = [f"# {title}", ""]
        md += [f"- **{k}**: {v}" for k, v in facts.items()]
        if outcome:
            md += ["", "## Outcome", "", "```json", json.dumps(outcome, indent=2, ensure_ascii=False), "```"]
        md += ["", "## Transcript", "", "```", self.transcript_text() or "(nothing was said)", "```", ""]
        path = self.dir / "transcript.md"
        path.write_text(redact("\n".join(md)), encoding="utf-8")
        return path

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
