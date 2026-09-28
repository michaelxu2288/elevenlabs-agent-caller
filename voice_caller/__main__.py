"""voice-caller command line. Run through ./vc (uses the project venv).

  ./vc doctor [--online] [--tunnel]   check setup; --online talks to Twilio/ElevenLabs (free calls)
  ./vc dry-run [--brain scripted]     simulated end-to-end call, no keys needed
  ./vc voices                         list ElevenLabs voices your key can use
  ./vc tts-sample "text"              render text in the configured voice to a phone-quality WAV
  ./vc tunnel-test                    prove Twilio can reach this machine over https + wss
  ./vc call TASK.toml [--yes]         place a real call (without --yes: print the plan only)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import (PROJECT_DIR, REQUIRED_SECRETS, ConfigError, load_config, load_secrets)
from .logs import register_secret, setup_logging
from .task import TaskError, check_dialable, load_task, normalize_number


def _reachable(url: str, timeout: float = 8.0) -> bool:
    """Does any HTTP answer come back (through a proxy, if one is configured)?"""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True          # the host answered, just not with 2xx
    except Exception:  # noqa: BLE001
        return False


def _ok(flag: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'ok' if flag else '!!'}] {label}" + (f": {detail}" if detail else ""))
    return flag


# ---------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    secrets = load_secrets()
    good = True
    print("local")
    good &= _ok(sys.version_info >= (3, 11), "python", platform.python_version())
    try:
        import aiohttp
        _ok(True, "aiohttp", aiohttp.__version__)
    except ImportError:
        good &= _ok(False, "aiohttp", "missing: run scripts/setup.sh")
    claude = shutil.which(cfg.brain.claude_bin)
    if claude:
        try:
            st = json.loads(subprocess.run([claude, "auth", "status", "--json"], capture_output=True,
                                           text=True, timeout=30).stdout or "{}")
        except Exception:  # noqa: BLE001
            st = {}
        good &= _ok(bool(st.get("loggedIn")), "claude CLI logged in",
                    f"{st.get('authMethod', '?')} via {claude}" if st.get("loggedIn") else "run `claude` and log in")
    else:
        good &= _ok(False, "claude CLI", "not on PATH")
    cf = shutil.which(cfg.tunnel.cloudflared_bin)
    if cfg.tunnel.mode == "cloudflared-quick":
        ver = subprocess.run([cf, "--version"], capture_output=True, text=True).stdout.strip() if cf else ""
        good &= _ok(bool(cf), "cloudflared (quick tunnel)", ver or "not found")
        reach = _reachable("https://api.trycloudflare.com/")
        good &= _ok(reach, "egress to api.trycloudflare.com",
                    "ok" if reach else "BLOCKED: this network does not pass it, so no quick tunnel "
                                       "can start (README 'Tunnel')")
    elif cfg.tunnel.mode == "static":
        good &= _ok(cfg.tunnel.public_url.startswith("https://"), "static tunnel URL", cfg.tunnel.public_url or "unset")
    print("secrets")
    s = secrets.status
    good &= _ok(s.state == "ok", f"secrets file {s.path}", s.detail or s.state)
    if s.state == "ok":
        _ok(not s.loose_permissions, "secrets file permissions", "chmod 600 it" if s.loose_permissions else "600")
    for key, what in REQUIRED_SECRETS.items():
        good &= _ok(bool(secrets.get(key)), key, "set" if secrets.get(key) else f"missing ({what})")
    print("config")
    good &= _ok(bool(cfg.elevenlabs.voice_id), "elevenlabs voice_id",
                cfg.elevenlabs.voice_id or "empty: pick one with `./vc voices`, put it in config.toml")
    _ok(True, "brain", f"{cfg.brain.model} (effort {cfg.brain.effort}); TTS {cfg.elevenlabs.tts_model}; "
                       f"STT {cfg.elevenlabs.stt_model}")
    for task_file in sorted((PROJECT_DIR / "tasks").glob("*.toml")):
        try:
            load_task(task_file)
            _ok(True, f"task {task_file.name}")
        except TaskError as e:
            good &= _ok(False, f"task {task_file.name}", str(e))
    if args.online:
        good &= asyncio.run(_doctor_online(cfg, secrets))
    if args.tunnel:
        good &= asyncio.run(_tunnel_test(cfg)) == 0
    print("\nall good" if good else "\nsome checks failed (see !! above)")
    return 0 if good else 1


async def _doctor_online(cfg, secrets) -> bool:
    import aiohttp

    from .elevenlabs import ElevenLabs
    from .twilio_api import TwilioClient
    for v in secrets.redaction_values():
        register_secret(v)
    good = True
    print("online (read-only requests, no charges)")
    async with aiohttp.ClientSession() as http:
        if secrets.get("TWILIO_ACCOUNT_SID") and secrets.get("TWILIO_AUTH_TOKEN"):
            tw = TwilioClient(http, secrets.get("TWILIO_ACCOUNT_SID"), secrets.get("TWILIO_AUTH_TOKEN"),
                              cfg.twilio.api_base)
            try:
                acct = await tw.fetch_account()
                full = acct.get("type") == "Full"
                good &= _ok(acct.get("status") == "active", "twilio account", f"{acct.get('status')}, {acct.get('type')}")
                good &= _ok(full, "twilio account upgraded",
                            "yes" if full else "TRIAL: trial accounts strip <Stream>, so calls cannot work; upgrade")
                frm = secrets.get("TWILIO_FROM_NUMBER")
                if frm:
                    good &= _ok(await tw.owns_number(frm), "TWILIO_FROM_NUMBER belongs to the account", frm)
            except Exception as e:  # noqa: BLE001
                good &= _ok(False, "twilio", str(e))
        if secrets.get("ELEVENLABS_API_KEY"):
            el = ElevenLabs(http, secrets.get("ELEVENLABS_API_KEY"), cfg.elevenlabs)
            try:
                sub = await el.get_json("/v1/user/subscription")
                good &= _ok(True, "elevenlabs key", f"tier {sub.get('tier')}, "
                                                    f"{sub.get('character_count')}/{sub.get('character_limit')} credits used")
            except Exception as e:  # noqa: BLE001
                good &= _ok(False, "elevenlabs key (needs user_read scope)", str(e))
            if cfg.elevenlabs.voice_id:
                try:
                    v = await el.get_json(f"/v1/voices/{cfg.elevenlabs.voice_id}")
                    good &= _ok(True, "voice", f"{v.get('name')} ({v.get('category')})")
                except Exception as e:  # noqa: BLE001
                    good &= _ok(False, "voice (needs voices_read scope)", str(e))
    return good


# ---------------------------------------------------------------- tunnel test

async def _tunnel_test(cfg) -> int:
    import aiohttp

    from .server import CallRegistry, build_app, start_server
    from .tunnel import TunnelError, make_tunnel, public_http
    print("tunnel")
    tunnel = make_tunnel(cfg, cfg.server.port)
    app = build_app(CallRegistry(), auth_token="x", public_base=lambda: tunnel.public_url, validate=True)
    server = await start_server(app, cfg.server.host, cfg.server.port)
    try:
        url = await tunnel.start()
        _ok(True, "public https reaches /health", url)
        async with public_http() as http:
            async with http.ws_connect(tunnel.ws_base + "/twilio/media", timeout=15) as ws:
                await ws.send_str(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
                await ws.send_str(json.dumps({"event": "start", "streamSid": "MZtest",
                                              "start": {"customParameters": {"call_id": "nope", "token": "x"}}}))
                msg = await ws.receive(timeout=15)
                closed = msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED) or ws.closed
                code = ws.close_code or (msg.data if msg.type == aiohttp.WSMsgType.CLOSE else None)
        ok = _ok(closed and code == 1008, "public wss reaches /twilio/media",
                 "connected; unknown stream correctly refused (1008)" if closed else f"unexpected {msg.type}")
        return 0 if ok else 1
    except (TunnelError, Exception) as e:  # noqa: BLE001
        _ok(False, "tunnel", str(e))
        return 1
    finally:
        await tunnel.stop()
        await server.cleanup()


def cmd_tunnel_test(args) -> int:
    cfg = load_config(args.config)
    return asyncio.run(_tunnel_test(cfg))


# ---------------------------------------------------------------- voices / tts sample

def cmd_voices(args) -> int:
    cfg, secrets = load_config(args.config), load_secrets()
    if not secrets.get("ELEVENLABS_API_KEY"):
        print("ELEVENLABS_API_KEY is not set (see README 'Secrets')")
        return 1

    async def go():
        import aiohttp

        from .elevenlabs import ElevenLabs
        async with aiohttp.ClientSession() as http:
            el = ElevenLabs(http, secrets.get("ELEVENLABS_API_KEY"), cfg.elevenlabs)
            data = await el.get_json("/v2/voices?page_size=100")
        voices = data.get("voices", [])
        for v in voices:
            mark = "*" if v.get("voice_id") == cfg.elevenlabs.voice_id else " "
            print(f"{mark} {v.get('voice_id')}  {v.get('name')}  [{v.get('category')}]")
        print(f"{len(voices)} voices. Put one voice_id in config.toml [elevenlabs] voice_id "
              "(* marks the current one).")
        return 0
    register_secret(secrets.get("ELEVENLABS_API_KEY"))
    return asyncio.run(go())


def cmd_tts_sample(args) -> int:
    cfg, secrets = load_config(args.config), load_secrets()
    voice = args.voice or cfg.elevenlabs.voice_id
    if not secrets.get("ELEVENLABS_API_KEY") or not voice:
        print("need ELEVENLABS_API_KEY and a voice_id (config.toml or --voice)")
        return 1
    register_secret(secrets.get("ELEVENLABS_API_KEY"))

    async def go():
        import aiohttp

        from .audio import pcm_to_wav, ulaw_to_pcm
        from .elevenlabs import ElevenLabs
        async with aiohttp.ClientSession() as http:
            el = ElevenLabs(http, secrets.get("ELEVENLABS_API_KEY"), cfg.elevenlabs)
            audio = b"".join([c async for c in el.tts_stream(args.text, voice_id=voice)])
        out = cfg.output_dir / "samples" / f"{datetime.now():%Y%m%d-%H%M%S}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(pcm_to_wav(ulaw_to_pcm(audio)))
        print(f"{len(audio) / 8000:.1f} s of 8 kHz phone audio -> {out}")
        return 0
    return asyncio.run(go())


# ---------------------------------------------------------------- dry run

def cmd_dry_run(args) -> int:
    sys.path.insert(0, str(PROJECT_DIR))
    from sim.dryrun import run_dry_run
    cfg = load_config(args.config)
    if args.brain == "claude" and args.speed != 1.0:
        print("note: with the Claude brain, keep --speed 1 so latency numbers mean something")
    ok, _ = asyncio.run(run_dry_run(cfg, Path(args.task), brain=args.brain, speed=args.speed))
    return 0 if ok else 1


# ---------------------------------------------------------------- real call

def cmd_call(args) -> int:
    from .console import attach_console
    from .runner import callback_task, estimate_max_twilio_cost, run_call
    cfg = load_config(args.config)
    secrets = load_secrets()
    if args.model:
        cfg.brain.model = args.model
    if args.voice:
        cfg.elevenlabs.voice_id = args.voice
    if args.from_number:
        secrets.set("TWILIO_FROM_NUMBER", normalize_number(args.from_number))
    if args.no_redial:
        cfg.calls.redial_if_unreached = False
    if args.no_callback:
        cfg.calls.callback_if_hung_up = False
    task = load_task(Path(args.task), to_override=args.to)
    task.to = check_dialable(task.to, allow_international=cfg.safety.allow_international,
                             own_number=secrets.get("TWILIO_FROM_NUMBER"))
    problems = [f"{k} is not set" for k in secrets.missing()]
    if problems and secrets.status.state != "ok":
        problems.insert(0, f"secrets file {secrets.status.path}: {secrets.status.detail or secrets.status.state}")
    if not cfg.elevenlabs.voice_id:
        problems.append("[elevenlabs] voice_id is empty (see `./vc voices`)")
    listen = None
    if args.listen_phone:
        if secrets.get("LISTEN_TO") and secrets.get("LISTEN_FROM"):
            listen = (normalize_number(secrets.get("LISTEN_TO")), normalize_number(secrets.get("LISTEN_FROM")))
            if listen[0] == task.to:
                problems.append("--listen-phone would ring the number being called")
        else:
            problems.append("--listen-phone needs LISTEN_TO (your phone) and LISTEN_FROM (a Twilio number) in the secrets file")
    print(f"Call plan\n  to:        {task.business_name} at {task.to}\n"
          f"  from:      {secrets.get('TWILIO_FROM_NUMBER') or '(TWILIO_FROM_NUMBER unset)'}\n"
          f"  for:       {task.on_behalf_of} (discloses AI: {'yes' if task.disclose_ai else 'only if asked'})\n"
          f"  goal:      {task.goal}\n"
          f"  brain:     {cfg.brain.model} via claude CLI; voice {cfg.elevenlabs.voice_id or '?'}\n"
          f"  tunnel:    {cfg.tunnel.mode}; hard time limit {cfg.twilio.time_limit_s // 60} min "
          f"(Twilio cost at the limit ~${estimate_max_twilio_cost(cfg)})"
          + (f"\n  listen:    ringing {listen[0]} from {listen[1]} to listen in" if listen else ""))
    if problems:
        print("cannot call yet:\n  - " + "\n  - ".join(problems))
        return 2
    if cfg.safety.require_confirmation and not args.yes:
        print("dry plan only. Re-run with --yes to dial.")
        return 0
    current, label, redialed, called_back = task, "", False, False
    while True:
        result = asyncio.run(run_call(current, cfg, secrets, on_session=lambda s: attach_console(s),
                                      label=label, listen=listen))
        print(f"\ncall {result.call_sid or '(not placed)'}: {result.final_status or 'unknown'}"
              + (f", error: {result.error}" if result.error else ""))
        if result.outcome:
            print("outcome: " + json.dumps(result.outcome, indent=2))
        print(f"records: {result.directory}")
        if cfg.calls.redial_if_unreached and not redialed and not called_back and result.unreached:
            redialed, label = True, "-redial"
            print("\nnot reached, calling once more right away (do not disturb lets a quick repeat call ring)")
            continue
        if cfg.calls.callback_if_hung_up and not called_back and result.hung_up_early:
            called_back, label, current = True, "-callback", callback_task(task, result)
            print("\nthey hung up partway, calling back once to ask if they meant to")
            continue
        break
    ok = not result.error and (result.outcome or {}).get("outcome") == "success"
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="config file (default: config.toml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check the setup")
    d.add_argument("--online", action="store_true", help="also check Twilio/ElevenLabs credentials (read-only)")
    d.add_argument("--tunnel", action="store_true", help="also bring up the tunnel and test https + wss")
    d.set_defaults(fn=cmd_doctor)
    r = sub.add_parser("dry-run", help="simulated call against a fake bakery, no keys needed")
    r.add_argument("--task", default=str(PROJECT_DIR / "tasks" / "example-bakery-order.toml"))
    r.add_argument("--brain", choices=["claude", "scripted"], default="claude")
    r.add_argument("--speed", type=float, default=1.0, help="simulated audio speed-up (scripted brain only)")
    r.set_defaults(fn=cmd_dry_run)
    v = sub.add_parser("voices", help="list ElevenLabs voices your key can use")
    v.set_defaults(fn=cmd_voices)
    t = sub.add_parser("tts-sample", help="render text to a phone-quality WAV in the configured voice")
    t.add_argument("text")
    t.add_argument("--voice", default=None)
    t.set_defaults(fn=cmd_tts_sample)
    tt = sub.add_parser("tunnel-test", help="bring up the public tunnel and test https + wss end to end")
    tt.set_defaults(fn=cmd_tunnel_test)
    c = sub.add_parser("call", help="place a real call")
    c.add_argument("task", help="task file, e.g. tasks/my-bakery.toml")
    c.add_argument("--yes", action="store_true", help="actually dial (otherwise only print the plan)")
    c.add_argument("--to", default=None, help="override the number in the task file")
    c.add_argument("--model", default=None, help="override [brain] model")
    c.add_argument("--voice", default=None, help="override [elevenlabs] voice_id")
    c.add_argument("--from", dest="from_number", default=None, help="caller ID for this call (default TWILIO_FROM_NUMBER)")
    c.add_argument("--no-redial", action="store_true", help="don't call again if it goes to voicemail")
    c.add_argument("--no-callback", action="store_true", help="don't call back if they hang up partway")
    c.add_argument("--listen-phone", action="store_true", help="also ring LISTEN_TO (from LISTEN_FROM) to listen in")
    c.set_defaults(fn=cmd_call)
    args = p.parse_args(argv)
    log_dir = PROJECT_DIR / "calls"
    log_dir.mkdir(exist_ok=True)
    setup_logging(args.verbose, logfile=log_dir / "voice-caller.log")
    try:
        return args.fn(args)
    except (ConfigError, TaskError) as e:
        print(f"error: {e}")
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
