"""The conversation brain: one long-lived `claude` CLI process per call.

The CLI is already logged in on this machine, so no Anthropic API key is needed. It
runs headless with stream-json in and out, which keeps the process (and the prompt
cache) warm between turns: a warm turn starts speaking in about a second, versus
2-4 s for a fresh process.

The other party on the phone is untrusted input, so the brain gets no tools, no MCP
servers, no settings/CLAUDE.md/hooks (--safe-mode), an empty private working
directory, and an environment with the Twilio/ElevenLabs keys removed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path

from .config import BrainConfig

log = logging.getLogger("brain")

_SCRUB_PREFIXES = ("TWILIO_", "ELEVENLABS_", "VOICE_CALLER_", "NGROK_")


class BrainError(RuntimeError):
    pass


class BrainTurn:
    """One request/response. Iterate deltas() for streamed text; `text` has the whole reply."""

    def __init__(self, turn_id: int, message: str):
        self.id = turn_id
        self.message = message
        self.text = ""
        self.cancelled = False
        self.error: str | None = None
        self.result: dict | None = None
        self.t_sent = time.monotonic()
        self.t_first: float | None = None
        self.done = asyncio.Event()
        self._q: asyncio.Queue = asyncio.Queue()

    @property
    def ttft_ms(self) -> int | None:
        return None if self.t_first is None else int((self.t_first - self.t_sent) * 1000)

    def _delta(self, s: str) -> None:
        if self.t_first is None:
            self.t_first = time.monotonic()
        self.text += s
        if not self.cancelled:
            self._q.put_nowait(s)

    def _finish(self, result: dict | None = None, error: str | None = None) -> None:
        if self.done.is_set():
            return
        self.result, self.error = result, error
        self.done.set()
        self._q.put_nowait(None)

    async def deltas(self):
        while True:
            item = await self._q.get()
            if item is None:
                if self.error and not self.cancelled:
                    raise BrainError(self.error)
                return
            yield item


class ClaudeCliBrain:
    def __init__(self, cfg: BrainConfig, system_prompt: str):
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.proc: asyncio.subprocess.Process | None = None
        self.workdir: Path | None = None
        self._turns: deque[BrainTurn] = deque()
        self._next_id = 1
        self._idle = asyncio.Event()
        self._idle.set()
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._tasks: list[asyncio.Task] = []
        self._dead: str | None = None
        self._req = 0
        self.turn_log: list[dict] = []

    def command(self) -> list[str]:
        return [
            self.cfg.claude_bin, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",
            "--tools", "", "--strict-mcp-config", "--safe-mode", "--disable-slash-commands",
            "--no-session-persistence",
            "--model", self.cfg.model, "--effort", self.cfg.effort,
            "--system-prompt-file", str(self.workdir / "system-prompt.txt"),
        ]

    async def start(self) -> None:
        if not shutil.which(self.cfg.claude_bin):
            raise BrainError(f"claude CLI not found ({self.cfg.claude_bin!r})")
        self.workdir = Path(tempfile.mkdtemp(prefix="vc-brain-"))
        os.chmod(self.workdir, 0o700)
        (self.workdir / "system-prompt.txt").write_text(self.system_prompt, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
        if not self.cfg.thinking:
            env["MAX_THINKING_TOKENS"] = "0"
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(), cwd=self.workdir, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024)
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        log.info("brain started: %s (effort %s), pid %s", self.cfg.model, self.cfg.effort, self.proc.pid)

    async def warmup(self, message: str) -> BrainTurn:
        """Spend the ringing time on process start-up and the prompt cache; output is ignored."""
        turn = await self.ask(message)
        try:
            await asyncio.wait_for(turn.done.wait(), timeout=60)
        except asyncio.TimeoutError:
            self.cancel(turn)
            turn.error = "warm-up timed out"
        log.info("brain warm (first reply %s ms)", turn.ttft_ms)
        return turn

    async def ask(self, message: str) -> BrainTurn:
        if self._dead:
            raise BrainError(self._dead)
        # strictly one turn in flight; cancelled turns are interrupted so this wait is short
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=self.cfg.turn_timeout_s)
        except asyncio.TimeoutError:
            raise BrainError("previous brain turn never finished")
        turn = BrainTurn(self._next_id, message)
        self._next_id += 1
        self._turns.append(turn)
        self._idle.clear()
        turn.t_sent = time.monotonic()
        await self._write({"type": "user", "message": {"role": "user", "content": message}})
        return turn

    def cancel(self, turn: BrainTurn) -> None:
        """Stop generating: the reply is no longer wanted (they started talking)."""
        if turn.done.is_set() or turn.cancelled:
            turn.cancelled = True
            return
        turn.cancelled = True
        turn._q.put_nowait(None)
        if self._turns and self._turns[0] is turn:
            self._req += 1
            asyncio.ensure_future(self._write({"type": "control_request", "request_id": f"int_{self._req}",
                                               "request": {"subtype": "interrupt"}}))

    async def _write(self, obj: dict) -> None:
        if not self.proc or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise BrainError(self._dead or "brain process is not running")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._route(ev)
        finally:
            code = await self.proc.wait()
            self._dead = f"claude CLI exited (code {code}): {' | '.join(self._stderr_tail)[-400:]}"
            while self._turns:
                self._turns.popleft()._finish(error=self._dead)
            self._idle.set()

    def _route(self, ev: dict) -> None:
        kind = ev.get("type")
        turn = self._turns[0] if self._turns else None
        if kind == "stream_event" and turn:
            e = ev.get("event") or {}
            if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                turn._delta(e["delta"].get("text", ""))
        elif kind == "result" and turn:
            self._turns.popleft()
            err = None
            if ev.get("is_error") and not turn.cancelled:
                err = f"{ev.get('subtype')}: {str(ev.get('result'))[:300]}"
            turn._finish(result=ev, error=err)
            self.turn_log.append({"turn": turn.id, "ttft_ms": turn.ttft_ms, "cancelled": turn.cancelled,
                                  "api_ms": ev.get("duration_api_ms"), "cost_usd": ev.get("total_cost_usd"),
                                  "error": err})
            if not self._turns:
                self._idle.set()
        elif kind == "control_response":
            pass

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._stderr_tail.append(text)
                log.debug("claude stderr: %s", text)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                self.proc.kill()
                await self.proc.wait()
        for t in self._tasks:
            t.cancel()
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


async def one_shot_json(cfg: BrainConfig, prompt: str, schema: dict, model: str | None = None,
                        timeout_s: float = 180) -> dict:
    """A single structured-output call (used for the post-call summary)."""
    workdir = Path(tempfile.mkdtemp(prefix="vc-summary-"))
    os.chmod(workdir, 0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    cmd = [cfg.claude_bin, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--safe-mode", "--disable-slash-commands", "--no-session-persistence",
           "--model", model or cfg.summary_model, "--effort", "low", "--json-schema", json.dumps(schema)]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=workdir, env=env,
                                                    stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=timeout_s)
        data = json.loads(out.decode("utf-8") or "{}")
        if data.get("is_error") or "structured_output" not in data:
            raise BrainError(f"summary failed: {str(data.get('result'))[:300]} {err.decode()[-300:]}")
        return data["structured_output"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
