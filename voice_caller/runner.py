"""Places one call end to end: server + tunnel + warm brain, dial, converse, record, summarize.

The dry run uses exactly this code path with the Twilio and ElevenLabs base URLs
pointed at local fakes and no tunnel.
"""
from __future__ import annotations

import asyncio
import logging
import secrets as pysecrets
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import aiohttp

from . import prompts
from .brain import BrainError, ClaudeCliBrain, one_shot_json
from .config import Config, Secrets
from .elevenlabs import ElevenLabs
from .listen import ListenLeg
from .logs import register_secret
from .recorder import CallRecorder
from .server import CallContext, CallRegistry, build_app, start_server
from .session import CallSession
from .task import CallTask
from .tunnel import Tunnel, make_tunnel
from .twilio_api import TwilioClient, TwilioError, stream_twiml

log = logging.getLogger("runner")

TWILIO_PER_MIN = 0.0140 + 0.0044   # US outbound voice + Media Streams (Aug 2026 list prices)


def estimate_max_twilio_cost(cfg: Config) -> float:
    return round((cfg.twilio.time_limit_s + 59) // 60 * TWILIO_PER_MIN, 2)


@dataclass
class CallResult:
    directory: Path
    call_sid: str = ""
    final_status: str = ""
    outcome: dict | None = None
    stats: dict = field(default_factory=dict)
    error: str = ""

    @property
    def spoke(self) -> bool:
        return bool(self.stats.get("turns"))

    @property
    def unreached(self) -> bool:
        if self.error:
            return False
        return (self.final_status in ("no-answer", "busy")
                or (self.outcome or {}).get("outcome") in ("voicemail", "no_answer"))

    @property
    def hung_up_early(self) -> bool:
        if self.error or self.unreached:
            return False
        agent_ended = self.stats.get("end_reason") == "agent ended the call"
        outcome = self.outcome or {}
        return (not agent_ended and outcome.get("outcome") in ("partial", "failed")
                and outcome.get("hung_up_mid_conversation") is True)


def callback_task(task: CallTask, previous: CallResult) -> CallTask:
    text = (previous.directory / "transcript.md").read_text(encoding="utf-8")
    last_call = text.split("## Transcript", 1)[-1].strip().strip("`").strip()
    details = "\n".join(line for line in task.details.splitlines()
                        if not line.lstrip("- ").startswith("Open with exactly"))
    return replace(
        task,
        details=("This is a callback: they hung up partway through the last call. Open with exactly: "
                 "\"Hey, did you mean to hang up?\" Then pick up where the last call stopped: skip anything "
                 "they already answered and finish the rest.\n" + details),
        extra_context=((task.extra_context + "\n\n") if task.extra_context else "") + "The last call:\n" + last_call)


async def run_call(task: CallTask, cfg: Config, secrets: Secrets, *, brain=None, tunnel: Tunnel | None = None,
                   summarize: bool = True, label: str = "", on_session=None,
                   listen: tuple[str, str] | None = None) -> CallResult:
    for v in secrets.redaction_values():
        register_secret(v)
    call_id = pysecrets.token_urlsafe(9)
    stream_token = pysecrets.token_urlsafe(24)
    register_secret(stream_token)
    tz = task.timezone or cfg.calls.timezone
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rec = CallRecorder(cfg.output_dir / f"{stamp}-{task.slug}{label}")
    result = CallResult(rec.dir)
    rec.event("call_planned", to=task.to, business=task.business_name, model=cfg.brain.model,
              voice=cfg.elevenlabs.voice_id, tts_model=cfg.elevenlabs.tts_model, tunnel=cfg.tunnel.mode)
    t_start = time.monotonic()

    async with aiohttp.ClientSession() as http:
        twilio = TwilioClient(http, secrets.get("TWILIO_ACCOUNT_SID"), secrets.get("TWILIO_AUTH_TOKEN"),
                              cfg.twilio.api_base)
        voice = ElevenLabs(http, secrets.get("ELEVENLABS_API_KEY"), cfg.elevenlabs)
        brain = brain or ClaudeCliBrain(cfg.brain, prompts.build_system_prompt(task, tz))
        registry = CallRegistry()
        tunnel = tunnel or make_tunnel(cfg, cfg.server.port)
        app = build_app(registry, auth_token=secrets.get("TWILIO_AUTH_TOKEN"),
                        public_base=lambda: tunnel.public_url, validate=cfg.twilio.validate_signatures)
        server = await start_server(app, cfg.server.host, cfg.server.port)
        ctx: CallContext | None = None
        warm: asyncio.Task | None = None
        prep: asyncio.Task | None = None
        session: CallSession | None = None
        leg_ctx: CallContext | None = None

        async def hangup():
            if ctx and ctx.call_sid:
                await twilio.hangup(ctx.call_sid)

        try:
            await brain.start()
            warm = asyncio.create_task(brain.warmup(prompts.warmup_message(task)))
            session = CallSession(task=task, cfg=cfg, brain=brain, voice=voice, recorder=rec, hangup=hangup)
            if on_session:
                on_session(session)
            # preflight, all before dialing: tunnel up, voice works both ways, brain answers
            prep = asyncio.create_task(session.prepare())
            public = await tunnel.start()
            rec.event("tunnel_ready", public_url=public)
            await prep
            warm_turn = await warm
            if warm_turn.error or warm_turn.cancelled:
                raise BrainError(f"brain preflight failed: {warm_turn.error or 'no reply within 60 s'}")
            ctx = CallContext(call_id, stream_token, session, frame_bytes=cfg.twilio.outbound_frame_bytes)
            registry.add(ctx)
            if listen:
                leg_ctx = CallContext(pysecrets.token_urlsafe(9), pysecrets.token_urlsafe(24), ListenLeg(rec))
                register_secret(leg_ctx.token)
                registry.add(leg_ctx)
                try:
                    leg_call = await twilio.create_call(
                        to=listen[0], from_=listen[1],
                        twiml=stream_twiml(f"{tunnel.ws_base}/twilio/media",
                                           {"call_id": leg_ctx.call_id, "token": leg_ctx.token}),
                        status_callback=f"{public}/twilio/status?call_id={leg_ctx.call_id}",
                        ring_timeout_s=cfg.twilio.ring_timeout_s, time_limit_s=cfg.twilio.time_limit_s + 60)
                    leg_ctx.call_sid = leg_call.get("sid", "")
                    session.listener = leg_ctx.session
                    rec.event("listener_dialed", to=listen[0])
                except (TwilioError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                    rec.event("listener_failed", error=str(e))
            twiml = stream_twiml(f"{tunnel.ws_base}/twilio/media", {"call_id": call_id, "token": stream_token})
            call = await twilio.create_call(
                to=task.to, from_=secrets.get("TWILIO_FROM_NUMBER"), twiml=twiml,
                status_callback=f"{public}/twilio/status?call_id={call_id}",
                ring_timeout_s=cfg.twilio.ring_timeout_s, time_limit_s=cfg.twilio.time_limit_s,
                send_digits=task.send_digits)
            ctx.call_sid = result.call_sid = call.get("sid", "")
            rec.event("call_created", call_sid=ctx.call_sid, status=call.get("status"))
            log.info("dialing %s (%s), call %s", task.business_name, task.to, ctx.call_sid)

            hard_stop = cfg.twilio.ring_timeout_s + cfg.twilio.time_limit_s + 90
            ended = asyncio.create_task(session.ended.wait())
            final = asyncio.create_task(ctx.final.wait())
            await asyncio.wait({ended, final}, timeout=hard_stop, return_when=asyncio.FIRST_COMPLETED)
            if not session.ended.is_set() and ctx.final.is_set():
                # Twilio says the call is over (busy/no-answer/failed, or hung up before the stream stopped)
                await asyncio.wait({ended}, timeout=5)
                await session.on_stop(f"call {ctx.final_status}")
            if session.ended.is_set() and not ctx.final.is_set():
                await asyncio.wait({final}, timeout=15)   # final status carries the duration
            for t in (ended, final):
                t.cancel()
            if not session.ended.is_set():
                rec.event("watchdog", note=f"no end after {hard_stop}s; hanging up")
                await session.on_stop("watchdog")
        except asyncio.CancelledError:
            rec.event("aborted", note="interrupted locally")
            result.error = "aborted"
            raise
        except Exception as e:  # noqa: BLE001 - record every failure in the call folder
            log.error("call failed: %s", e)
            rec.event("error", error=str(e), error_type=type(e).__name__)
            result.error = str(e)
        finally:
            if ctx and ctx.call_sid and not ctx.final.is_set():
                try:  # never leave a billable call running behind us
                    await twilio.hangup(ctx.call_sid)
                except Exception as e:  # noqa: BLE001
                    log.debug("final hang-up: %s", e)
            if leg_ctx and leg_ctx.call_sid and not leg_ctx.final.is_set():
                try:
                    await twilio.hangup(leg_ctx.call_sid)
                except (TwilioError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.debug("listener hang-up: %s", e)
            for t in (warm, prep):
                if t and not t.done():
                    t.cancel()
            await brain.close()
            await tunnel.stop()
            await server.cleanup()
        result.final_status = ctx.final_status if ctx else ""
        if session:
            result.stats = session.stats()
        result.stats["wall_seconds"] = round(time.monotonic() - t_start, 1)
        result.stats["elevenlabs_usage"] = voice.usage
        result.stats["brain_turns"] = getattr(brain, "turn_log", [])

        facts = {"business": task.business_name, "to": task.to, "call_sid": result.call_sid,
                 "twilio_final_status": result.final_status or "unknown",
                 "ended_because": result.stats.get("end_reason", ""),
                 "call_duration_s": next((s.get("duration") for s in reversed(ctx.statuses)
                                          if s.get("duration")), None) if ctx else None}
        if summarize and rec.lines():
            try:
                result.outcome = await one_shot_json(
                    cfg.brain, prompts.summary_prompt(task, rec.transcript_text(), str(facts)),
                    prompts.SUMMARY_SCHEMA)
            except Exception as e:  # noqa: BLE001
                log.warning("post-call summary failed: %s", e)
                result.outcome = {"summary_error": str(e)}
        elif not rec.lines():
            result.outcome = {"outcome": "no_answer" if result.final_status in ("busy", "no-answer", "failed",
                                                                              "canceled", "") else "failed",
                              "summary": "Nothing was said on the call.", "order_placed": False,
                              "hung_up_mid_conversation": result.final_status == "completed"}
        rec.write_json("outcome.json", result.outcome or {})
        rec.write_json("call.json", {"facts": facts, "statuses": ctx.statuses if ctx else [],
                                     "stats": result.stats, "error": result.error,
                                     "task_file": str(task.source) if task.source else None,
                                     "models": {"brain": cfg.brain.model, "effort": cfg.brain.effort,
                                                "tts": cfg.elevenlabs.tts_model, "stt": cfg.elevenlabs.stt_model,
                                                "voice": cfg.elevenlabs.voice_id}})
        rec.write_transcript(f"Call to {task.business_name}", facts, result.outcome)
        rec.close()
    return result
