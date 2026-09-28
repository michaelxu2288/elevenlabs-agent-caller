"""Integration tests: the real server against fake Twilio traffic, and the full offline
simulated call (scripted brain, 4x speed). No keys, no network."""
import asyncio
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from voice_caller.config import load_config  # noqa: E402
from voice_caller.recorder import CallRecorder  # noqa: E402
from voice_caller.server import CallContext, CallRegistry, build_app, start_server  # noqa: E402
from voice_caller.twilio_api import compute_signature  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubSession:
    def __init__(self, rec):
        self.rec = rec
        self.started = []

    async def on_start(self, start, sender):
        self.started.append(start)

    async def on_media(self, b):
        pass

    def on_mark(self, n):
        pass

    async def on_stop(self, reason=""):
        pass


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.registry = CallRegistry()
        self.session = _StubSession(CallRecorder(Path(self.tmp.name)))
        self.registry.add(CallContext("cid", "tok", self.session))
        app = build_app(self.registry, auth_token="authtok", public_base=lambda: self.base)
        self.runner = await start_server(app, "127.0.0.1", self.port)
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.http.close()
        await self.runner.cleanup()
        self.session.rec.close()
        self.tmp.cleanup()

    async def test_status_callback_signature_enforced(self):
        url = f"{self.base}/twilio/status?call_id=cid"
        params = {"CallSid": "CA1", "CallStatus": "ringing"}
        async with self.http.post(url, data=params, headers={"X-Twilio-Signature": "bogus"}) as r:
            self.assertEqual(r.status, 403)
        sig = compute_signature("authtok", url, params)
        async with self.http.post(url, data=params, headers={"X-Twilio-Signature": sig}) as r:
            self.assertEqual(r.status, 204)
        self.assertEqual(self.registry.get("cid").statuses[-1]["status"], "ringing")

    async def test_media_stream_needs_the_call_token(self):
        async with self.http.ws_connect(f"{self.base.replace('http', 'ws')}/twilio/media") as ws:
            await ws.send_str(json.dumps({"event": "start", "streamSid": "MZ1",
                                          "start": {"customParameters": {"call_id": "cid", "token": "wrong"}}}))
            msg = await ws.receive(timeout=5)
            self.assertEqual(msg.type, aiohttp.WSMsgType.CLOSE)
            self.assertEqual(msg.data, 1008)
        self.assertEqual(self.session.started, [])
        async with self.http.ws_connect(f"{self.base.replace('http', 'ws')}/twilio/media") as ws:
            await ws.send_str(json.dumps({"event": "start", "streamSid": "MZ2", "start": {
                "callSid": "CA1", "streamSid": "MZ2", "customParameters": {"call_id": "cid", "token": "tok"}}}))
            await ws.send_str(json.dumps({"event": "stop", "streamSid": "MZ2"}))
            await ws.receive(timeout=5)
        self.assertEqual(len(self.session.started), 1)


class OfflineCallTest(unittest.IsolatedAsyncioTestCase):
    async def test_scripted_call_passes_every_check(self):
        from sim.dryrun import run_dry_run
        cfg = load_config(ROOT / "config.toml")
        with tempfile.TemporaryDirectory() as d:
            cfg.calls.output_dir = d
            out = io.StringIO()
            with redirect_stdout(out):
                ok, report = await run_dry_run(cfg, ROOT / "tasks" / "example-bakery-order.toml",
                                               brain="scripted", speed=4.0, echo=lambda *a: None)
            failed = [k for k, v in report["pipeline"].items() if not v]
            failed += [k for k, v in report["conversation"]["checks"].items() if not v]
            self.assertTrue(ok, f"failed checks: {failed}")


class BrainFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_brain_dying_mid_call_apologizes_and_hangs_up(self):
        from sim.dryrun import run_dry_run
        from sim.scripted_brain import ScriptedBrain
        from voice_caller.brain import BrainTurn

        class DyingBrain(ScriptedBrain):
            async def ask(self, message):
                if self._n >= 2:
                    self._n += 1
                    turn = BrainTurn(self._n, message)
                    turn._finish(error="claude CLI exited (code 1): simulated crash")
                    return turn
                return await super().ask(message)

        cfg = load_config(ROOT / "config.toml")
        with tempfile.TemporaryDirectory() as d:
            cfg.calls.output_dir = d
            ok, report = await run_dry_run(cfg, ROOT / "tasks" / "example-bakery-order.toml",
                                           brain=DyingBrain(), speed=4.0, echo=lambda *a: None)
            self.assertFalse(ok)
            self.assertTrue(report["stats"]["end_reason"].startswith("error"), report["stats"]["end_reason"])
            self.assertTrue(report["pipeline"]["agent hung up through the REST API"])
            transcript = (Path(report["call_dir"]) / "transcript.md").read_text()
            self.assertIn("technical problem", transcript)


class PreflightTest(unittest.IsolatedAsyncioTestCase):
    async def test_bad_voice_key_aborts_before_dialing(self):
        from aiohttp import web

        from sim.fake_elevenlabs import FakeElevenLabs
        from sim.fake_twilio import FakeTwilio
        from sim.scripted_brain import ScriptedBrain
        from voice_caller.config import Secrets, SecretsStatus
        from voice_caller.runner import run_call
        from voice_caller.task import load_task
        from voice_caller.tunnel import NoTunnel
        tw_port, el_port, port = free_port(), free_port(), free_port()
        fake_tw = FakeTwilio("AC" + "1" * 32, "tok" * 8, {"+15125550100"}, lambda phone: None)
        fake_el = FakeElevenLabs("sk_right_key_000000", "voice1")
        runners = []
        for app, p in ((fake_tw.app, tw_port), (fake_el.app, el_port)):
            r = web.AppRunner(app)
            await r.setup()
            await web.TCPSite(r, "127.0.0.1", p).start()
            runners.append(r)
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg = load_config(overrides={
                    "server": {"port": port}, "twilio": {"api_base": f"http://127.0.0.1:{tw_port}"},
                    "elevenlabs": {"api_base": f"http://127.0.0.1:{el_port}", "voice_id": "voice1"},
                    "tunnel": {"mode": "none"}, "calls": {"output_dir": d}})
                secrets = Secrets({"TWILIO_ACCOUNT_SID": "AC" + "1" * 32, "TWILIO_AUTH_TOKEN": "tok" * 8,
                                   "TWILIO_FROM_NUMBER": "+15125550100",
                                   "ELEVENLABS_API_KEY": "sk_WRONG_key_111111"}, SecretsStatus(Path(d), "ok"))
                task = load_task(ROOT / "tasks" / "example-bakery-order.toml")
                result = await run_call(task, cfg, secrets, brain=ScriptedBrain(), summarize=False,
                                        tunnel=NoTunnel(f"http://127.0.0.1:{port}"))
                self.assertIn("401", result.error)
                self.assertFalse(any(path.endswith("/Calls.json") for _, path in fake_tw.requests),
                                 "dialed despite a broken voice setup")
                self.assertNotIn("sk_WRONG_key_111111", (result.directory / "events.jsonl").read_text())
        finally:
            for r in runners:
                await r.cleanup()


class ListenLegServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_listen_stream_is_admitted_and_gets_audio(self):
        from voice_caller.audio import silence_ulaw
        from voice_caller.listen import ListenLeg
        with tempfile.TemporaryDirectory() as d:
            rec = CallRecorder(Path(d))
            leg = ListenLeg(rec)
            registry = CallRegistry()
            registry.add(CallContext("lid", "ltok", leg))
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            runner = await start_server(build_app(registry, auth_token="t", public_base=lambda: base),
                                        "127.0.0.1", port)
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(f"ws://127.0.0.1:{port}/twilio/media") as ws:
                        await ws.send_str(json.dumps({"event": "start", "streamSid": "MZL", "start": {
                            "callSid": "CAL", "customParameters": {"call_id": "lid", "token": "ltok"}}}))
                        beep = json.loads((await ws.receive(timeout=5)).data)
                        self.assertEqual((beep["event"], beep["streamSid"]), ("media", "MZL"))
                        await leg.callee_frame(silence_ulaw(20))
                        frame = json.loads((await ws.receive(timeout=5)).data)
                        self.assertEqual(frame["event"], "media")
                        await ws.send_str(json.dumps({"event": "stop"}))
                await asyncio.sleep(0.1)
                self.assertIsNone(leg.sender)
            finally:
                await runner.cleanup()
                rec.close()


class PublicDnsResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_public_dns_then_uses_its_answer(self):
        from aiohttp import web

        from voice_caller.tunnel import PublicDnsResolver
        asked = []

        async def doh(request):
            asked.append(request.query["name"])
            if len(asked) != 2:
                return web.json_response({"Status": 3})
            return web.json_response({"Status": 0, "Answer": [
                {"name": "t.example.com", "type": 5, "data": "edge.example.net."},
                {"name": "edge.example.net", "type": 1, "data": "203.0.113.7"}]})
        app = web.Application()
        app.router.add_get("/dns-query", doh)
        port = free_port()
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        try:
            resolver = PublicDnsResolver(f"http://127.0.0.1:{port}/dns-query")
            with self.assertRaises(OSError):
                await resolver.resolve("t.example.com", 443)
            hosts = await resolver.resolve("t.example.com", 443)
            self.assertEqual([(h["hostname"], h["host"], h["port"]) for h in hosts],
                             [("t.example.com", "203.0.113.7", 443)])
            again = await PublicDnsResolver(f"http://127.0.0.1:{port}/dns-query").resolve("t.example.com", 443)
            self.assertEqual([h["host"] for h in again], ["203.0.113.7"])
            self.assertEqual(asked, ["t.example.com", "t.example.com"])
        finally:
            await runner.cleanup()

    async def test_falls_back_to_system_dns_when_doh_is_unreachable(self):
        from voice_caller.tunnel import PublicDnsResolver
        resolver = PublicDnsResolver(f"http://127.0.0.1:{free_port()}/dns-query")
        hosts = await resolver.resolve("localhost", 80)
        self.assertTrue({h["host"] for h in hosts} & {"127.0.0.1", "::1"})


if __name__ == "__main__":
    unittest.main()
