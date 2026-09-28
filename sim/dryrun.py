"""End-to-end simulated call: the real voice_caller pipeline against fake Twilio + fake
ElevenLabs + a scripted bakery employee. No real keys, no network except the Claude CLI
(which is already logged in); `--brain scripted` makes it fully offline.

Throwaway credentials are generated for the fakes, signature checks stay on, and at the
end every file written for the call is scanned to prove none of those values leaked.
"""
from __future__ import annotations

import asyncio
import json
import secrets as pysecrets
import socket
from pathlib import Path

from aiohttp import web

from voice_caller.config import Config, Secrets, SecretsStatus
from voice_caller.console import attach_console
from voice_caller.runner import run_call
from voice_caller.task import check_dialable, load_task
from voice_caller.tunnel import NoTunnel

from .bakery import BakeryEmployee
from .fake_elevenlabs import FakeElevenLabs
from .fake_twilio import FakeTwilio
from .scripted_brain import ScriptedBrain

FAKE_BAKERY_NUMBER = "+15125550142"
FAKE_FROM_NUMBER = "+15125550100"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def run_dry_run(cfg: Config, task_path: Path, *, brain="claude", speed: float = 1.0,
                      echo=print) -> tuple[bool, dict]:
    """brain: "claude" (the real CLI), "scripted" (offline), or a brain object (tests)."""
    fake_values = {
        "TWILIO_ACCOUNT_SID": "AC" + pysecrets.token_hex(16),
        "TWILIO_AUTH_TOKEN": pysecrets.token_hex(16),
        "TWILIO_FROM_NUMBER": FAKE_FROM_NUMBER,
        "ELEVENLABS_API_KEY": "sk_" + pysecrets.token_hex(24),
    }
    secrets = Secrets(fake_values, SecretsStatus(Path("(dry run: generated throwaway keys)"), "ok"))
    server_port, twilio_port, el_port = free_port(), free_port(), free_port()
    cfg.server.port = server_port
    cfg.twilio.api_base = f"http://127.0.0.1:{twilio_port}"
    cfg.elevenlabs.api_base = f"http://127.0.0.1:{el_port}"
    cfg.elevenlabs.voice_id = cfg.elevenlabs.voice_id or "DryRunVoice00000000"
    cfg.tunnel.mode = "none"
    cfg.calls.output_dir = str(cfg.output_dir / "dry-runs")

    employees: list[BakeryEmployee] = []

    def callee(phone):
        emp = BakeryEmployee(phone, log=echo)
        employees.append(emp)
        return emp

    fake_twilio = FakeTwilio(fake_values["TWILIO_ACCOUNT_SID"], fake_values["TWILIO_AUTH_TOKEN"],
                             {FAKE_FROM_NUMBER}, callee, speed=speed)
    fake_el = FakeElevenLabs(fake_values["ELEVENLABS_API_KEY"], cfg.elevenlabs.voice_id)
    runners = [await _serve(fake_twilio.app, twilio_port), await _serve(fake_el.app, el_port)]

    task = load_task(task_path, to_override=FAKE_BAKERY_NUMBER)
    task.simulated = True
    task.to = check_dialable(task.to, simulated=True, own_number=FAKE_FROM_NUMBER)
    brain_obj = ScriptedBrain() if brain == "scripted" else None if brain == "claude" else brain
    echo(f"dry run: {task.business_name} (simulated), brain = "
         f"{cfg.brain.model + ' via claude CLI' if brain == 'claude' else 'scripted (offline)'}, speed x{speed}")
    try:
        result = await run_call(task, cfg, secrets, brain=brain_obj,
                                tunnel=NoTunnel(f"http://127.0.0.1:{server_port}"),
                                summarize=(brain == "claude"), label="-dryrun",
                                on_session=lambda s: attach_console(s, echo))
    finally:
        await asyncio.sleep(0.5)
        for r in runners:
            await r.cleanup()

    st = fake_twilio.stats
    emp = employees[0] if employees else None
    verdict = emp.verdict() if emp else {"checks": {}, "passed": 0, "total": 0}
    leaked = _scan_for_leaks(result.directory, [v for k, v in fake_values.items() if k != "TWILIO_FROM_NUMBER"])
    decode_errs = st.agent_decode_errors
    pipeline = {
        "call placed via Twilio REST with inline <Connect><Stream> TwiML":
            any(p.endswith("/Calls.json") for m, p in fake_twilio.requests if m == "POST"),
        "all signed status callbacks accepted (initiated/ringing/answered/completed)":
            len(st.status_callbacks) == 4 and all(code == 204 for _, code in st.status_callbacks),
        "media stream carried audio both ways": st.inbound_frames > 100 and st.outbound_bytes > 8000,
        "every mark echoed back": st.marks_received > 0 and st.marks_echoed == st.marks_received,
        "barge-in stopped playback with a Twilio clear": st.clears >= 1,
        "agent audio arrived intact (only barge-in truncations)":
            all(e == "truncated" for e in decode_errs) and len(decode_errs) <= st.clears,
        "callee speech transcribed intact": fake_el.usage["stt_decode_errors"] == 0 and fake_el.usage["stt_requests"] > 0,
        "DTMF tones generated and recognised by the phone menu": "2" in st.dtmf_heard,
        "agent hung up through the REST API": any(m == "POST" and "/Calls/CA" in p for m, p in fake_twilio.requests),
        "no protocol errors on the media stream": not st.protocol_errors,
        "no API keys or tokens in the call records": not leaked,
    }
    ok = all(pipeline.values()) and verdict["passed"] == verdict["total"]
    report = {
        "ok": ok, "call_dir": str(result.directory), "pipeline": pipeline, "conversation": verdict,
        "stats": result.stats, "outcome": result.outcome, "fake_twilio": {
            "inbound_frames": st.inbound_frames, "outbound_bytes": st.outbound_bytes,
            "outbound_messages": st.outbound_messages, "marks": st.marks_received, "clears": st.clears,
            "status_callbacks": st.status_callbacks, "dtmf_heard": st.dtmf_heard,
            "protocol_errors": st.protocol_errors, "agent_decode_errors": decode_errs},
        "fake_elevenlabs": fake_el.usage, "leaks": leaked, "error": result.error,
    }
    (result.directory / "dryrun-report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    _print_scorecard(report, echo)
    return ok, report


def _scan_for_leaks(directory: Path, values: list[str]) -> list[str]:
    hits = []
    for f in directory.rglob("*"):
        if f.is_file():
            text = f.read_text(errors="replace")
            hits += [f"{f.name}" for v in values if v in text]
    return hits


def _print_scorecard(r: dict, echo) -> None:
    def line(ok, label):
        echo(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    echo("\n=== pipeline ===")
    for label, ok in r["pipeline"].items():
        line(ok, label)
    v = r["conversation"]
    echo(f"\n=== conversation (scripted bakery employee's view): {v['passed']}/{v['total']} ===")
    for label, ok in v["checks"].items():
        line(ok, label)
    s = r["stats"]
    gap = s.get("response_gap_ms", {})
    echo(f"\nturns {s.get('turns')}, call audio {s.get('audio_seconds')} s, barge-ins {s.get('barge_ins')}; "
         f"response gap (their last word -> agent audio) p50 {gap.get('p50')} ms, p90 {gap.get('p90')} ms, "
         f"max {gap.get('max')} ms; brain first token p50 {s.get('brain_ttft_ms', {}).get('p50')} ms")
    u = r["fake_elevenlabs"]
    echo(f"ElevenLabs usage this call: {u['tts_chars']} TTS chars in {u['tts_requests']} requests, "
         f"{u['stt_audio_s']} s of STT audio in {u['stt_requests']} requests")
    if r.get("outcome"):
        echo("outcome: " + json.dumps(r["outcome"])[:600])
    echo(f"records: {r['call_dir']}")
    echo("RESULT: " + ("PASS" if r["ok"] else "FAIL"))
