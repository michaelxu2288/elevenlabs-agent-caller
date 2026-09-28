"""Fast unit tests: python -m unittest discover -s tests   (or ./scripts/test.sh)"""
import logging
import math
import os
import stat
import sys
import tempfile
import unittest
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sim import fsk  # noqa: E402
from voice_caller import audio  # noqa: E402
from voice_caller.config import (ConfigError, TurnConfig, load_config, load_secrets,  # noqa: E402
                                 parse_env_file)
from voice_caller.logs import RedactingFilter, redact, register_secret  # noqa: E402
from voice_caller.speech_text import Control, Speak, SpeechChunker, clean_for_tts  # noqa: E402
from voice_caller.task import TaskError, check_dialable, load_task, normalize_number  # noqa: E402
from voice_caller.twilio_api import compute_signature, stream_twiml, validate_signature  # noqa: E402
from voice_caller.vad import TurnDetector  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class AudioTests(unittest.TestCase):
    def test_ulaw_roundtrip_within_g711_error(self):
        for s in range(-32000, 32000, 997):
            back = audio.ULAW_DECODE[audio.ULAW_ENCODE[s & 0xFFFF]]
            self.assertLessEqual(abs(back - s), max(abs(s) * 0.07, 16), s)

    def test_silence_is_0xff(self):
        self.assertEqual(audio.ULAW_ENCODE[0], 0xFF)
        self.assertEqual(audio.ULAW_DECODE[0xFF], 0)

    def test_wav_roundtrip(self):
        pcm = array("h", [0, 1000, -1000, 32767, -32768])
        back, rate = audio.wav_to_pcm(audio.pcm_to_wav(pcm))
        self.assertEqual((list(back), rate), (list(pcm), 8000))

    def test_dtmf_tones_are_detectable(self):
        for digit in "0123456789*#":
            pcm = audio.ulaw_to_pcm(audio.dtmf_ulaw(digit))
            hits = [fsk.detect_dtmf(pcm[i:i + 160]) for i in range(0, 160 * 6, 160)]
            self.assertEqual(hits.count(digit), 6, (digit, hits))

    def test_fsk_modem_survives_ulaw_and_misalignment(self):
        pcm = audio.ulaw_to_pcm(fsk.encode_ulaw(["Twenty-five fifty, ¿sí?", 300, "second"]))
        packets, errors = fsk.decode_all(array("h", [0] * 53) + pcm)
        self.assertEqual((packets, errors), (["Twenty-five fifty, ¿sí?", "second"], []))


class ChunkerTests(unittest.TestCase):
    def run_chunker(self, text, step=4, early=False):
        c = SpeechChunker(early_first=early)
        out = []
        for i in range(0, len(text), step):
            out += c.feed(text[i:i + step])
        return out + c.finish()

    def test_sentences_and_markers_split_across_deltas(self):
        out = self.run_chunker("Thanks. Pickup at ten a.m. on Saturday, Dr. Lee said. [[DTMF:2]] Bye! [[END_CALL]]")
        self.assertEqual(out, [Speak("Thanks."), Speak("Pickup at ten a.m. on Saturday, Dr. Lee said."),
                               Control("dtmf", "2"), Speak("Bye!"), Control("end_call")])

    def test_wait_only(self):
        self.assertEqual(self.run_chunker("[[WAIT]]"), [Control("wait")])

    def test_truncated_marker_is_not_spoken(self):
        self.assertEqual(self.run_chunker("Okay [[END_C"), [Speak("Okay")])

    def test_early_first_clause(self):
        out = self.run_chunker("Hi Maria, I'm calling for Alex. Thanks.", early=True)
        self.assertEqual(out[0], Speak("Hi Maria,"))
        self.assertEqual(len(out), 3)

    def test_cleaning(self):
        self.assertEqual(clean_for_tts("**Sure!** <event>x</event> *laughs* ok"), "Sure! ok")
        self.assertEqual(clean_for_tts("2*3 is six"), "2 3 is six")


class VadTests(unittest.TestCase):
    def frames(self, ms, amp):
        n = ms // 20
        return [array("h", [int(amp * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(160)])
                for _ in range(n)]

    def kinds(self, det, frames):
        out = []
        for f in frames:
            out += [e.kind for e in det.process(f) if e.kind != "voiced"]
        return out

    def test_utterance_with_speculative_transcription(self):
        det = TurnDetector(TurnConfig())
        ev = self.kinds(det, self.frames(400, 0) + self.frames(1000, 6000) + self.frames(900, 0))
        self.assertEqual(ev, ["start", "speculate", "end"])

    def test_short_pause_does_not_end_the_turn(self):
        det = TurnDetector(TurnConfig())
        ev = self.kinds(det, self.frames(600, 6000) + self.frames(550, 0) + self.frames(600, 6000)
                        + self.frames(900, 0))
        self.assertEqual(ev, ["start", "speculate", "resume", "speculate", "end"])

    def test_blip_is_discarded(self):
        det = TurnDetector(TurnConfig())
        self.assertEqual(self.kinds(det, self.frames(100, 6000) + self.frames(900, 0)), ["start", "discard"])

    def test_quiet_line_noise_is_not_speech(self):
        det = TurnDetector(TurnConfig())
        self.assertEqual(self.kinds(det, self.frames(3000, 150)), [])


class TwilioTests(unittest.TestCase):
    def test_official_signature_vector(self):
        # https://www.twilio.com/docs/usage/security
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
                  "From": "+14158675310", "To": "+18005551212"}
        url = "https://example.com/myapp.php?foo=1&bar=2"
        self.assertEqual(compute_signature("12345", url, params), "L/OH5YylLD5NRKLltdqwSvS0BnU=")
        self.assertTrue(validate_signature("12345", url, params, "L/OH5YylLD5NRKLltdqwSvS0BnU="))
        self.assertFalse(validate_signature("12346", url, params, "L/OH5YylLD5NRKLltdqwSvS0BnU="))

    def test_twiml_escapes_values(self):
        import xml.etree.ElementTree as ET
        x = stream_twiml("wss://h/twilio/media", {"call_id": 'a"b<c', "token": "t&k"})
        stream = ET.fromstring(x).find("./Connect/Stream")
        self.assertEqual(stream.get("url"), "wss://h/twilio/media")
        self.assertEqual({p.get("name"): p.get("value") for p in stream}, {"call_id": 'a"b<c', "token": "t&k"})


class ConfigAndSecretsTests(unittest.TestCase):
    def test_env_file_parsing(self):
        got = parse_env_file("# c\nA=1\nexport B = 'two'\nC=\"x=y\"\n\nbad line\n")
        self.assertEqual(got, {"A": "1", "B": "two", "C": "x=y"})

    def test_secrets_file_env_wins_and_repr_hides_values(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "secrets.env"
            f.write_text("TWILIO_AUTH_TOKEN=from-file-123456\nELEVENLABS_API_KEY=sk_file_abcdef\n")
            os.chmod(f, 0o600)
            s = load_secrets(f, environ={"TWILIO_AUTH_TOKEN": "from-env-654321"})
            self.assertEqual(s.get("TWILIO_AUTH_TOKEN"), "from-env-654321")
            self.assertEqual(s.status.state, "ok")
            self.assertFalse(s.status.loose_permissions)
            self.assertNotIn("sk_file_abcdef", repr(s) + str(s))
            self.assertIn("TWILIO_ACCOUNT_SID", s.missing())

    def test_missing_and_loose_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_secrets(Path(d) / "nope.env", environ={}).status.state, "missing")
            f = Path(d) / "s.env"
            f.write_text("A=1\n")
            os.chmod(f, 0o644)
            self.assertTrue(load_secrets(f, environ={}).status.loose_permissions)

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_untraversable_directory_is_explained(self):
        with tempfile.TemporaryDirectory() as d:
            locked = Path(d) / "locked"
            locked.mkdir()
            (locked / "secrets.env").write_text("A=1\n")
            os.chmod(locked, 0)
            try:
                st = load_secrets(locked / "secrets.env", environ={}).status
                self.assertEqual(st.state, "unreadable")
                self.assertIn(str(locked), st.detail)
            finally:
                os.chmod(locked, stat.S_IRWXU)

    def test_config_rejects_unknown_keys_and_bad_types(self):
        with self.assertRaises(ConfigError):
            load_config(overrides={"turns": {"endpoint_silence": 5}})
        with self.assertRaises(ConfigError):
            load_config(overrides={"twilio": {"time_limit_s": "long"}})
        self.assertEqual(load_config(overrides={"turns": {"min_speech_rms": 400}}).turns.min_speech_rms, 400.0)

    def test_repo_config_loads(self):
        cfg = load_config(ROOT / "config.toml")
        self.assertEqual(cfg.tunnel.mode, "cloudflared-quick")

    def test_log_redaction(self):
        register_secret("supersecretvalue42")
        self.assertEqual(redact("token=supersecretvalue42!"), "token=***!")
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "auth %s", ("supersecretvalue42",), None)
        RedactingFilter().filter(rec)
        self.assertEqual(rec.getMessage(), "auth ***")


class TaskTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_number("(512) 555-0199"), "+15125550199")
        self.assertEqual(normalize_number("+1 512.555.0199"), "+15125550199")

    def test_refuses_dangerous_numbers(self):
        for n in ["911", "988", "+1 900 555 1234", "+1 212 976 1234", "+1 512 411 1234", "+44 20 7946 0000",
                  "+1 012 555 1234"]:
            with self.assertRaises(TaskError, msg=n):
                check_dialable(n)
        with self.assertRaises(TaskError):
            check_dialable("+15125550142")                     # fictional 555-01xx
        self.assertEqual(check_dialable("+15125550142", simulated=True), "+15125550142")
        self.assertEqual(check_dialable("512-867-5309"), "+15128675309")
        with self.assertRaises(TaskError):
            check_dialable("+15128675309", own_number="512 867 5309")

    def test_example_task_loads(self):
        t = load_task(ROOT / "tasks" / "example-bakery-order.toml")
        self.assertEqual(t.business_name, "Sweet Crumb Bakery")
        self.assertTrue(t.disclose_ai)


class ListenLegTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_hears_callee_mixed_with_agent(self):
        from voice_caller.audio import silence_ulaw, ulaw_to_pcm
        from voice_caller.listen import ListenLeg, TeeSender, mix_ulaw

        class FakeSender:
            stream_sid, clears, bytes_sent = "MZ1", 0, 0

            def __init__(self):
                self.sent = []

            async def audio(self, ulaw):
                self.sent.append(bytes(ulaw))

            async def mark(self, name):
                pass

            async def clear(self):
                self.clears += 1

        class Rec:
            def __init__(self):
                self.events = []

            def event(self, kind, **fields):
                self.events.append(kind)

        rec, callee, listener = Rec(), FakeSender(), FakeSender()
        leg = ListenLeg(rec)
        tee = TeeSender(callee, leg)
        await leg.on_start({}, listener)
        self.assertEqual(rec.events, ["listener_connected"])
        self.assertEqual(len(listener.sent), 1)
        agent = bytes([0x10] * 320)
        await tee.audio(agent)
        self.assertEqual(callee.sent, [agent])
        frame = silence_ulaw(20)
        await leg.callee_frame(frame)
        self.assertEqual(listener.sent[-1], mix_ulaw(frame, agent[:160]))
        self.assertNotEqual(ulaw_to_pcm(listener.sent[-1]).tolist(), ulaw_to_pcm(frame).tolist())
        await tee.clear()
        self.assertEqual(callee.clears, 1)
        await leg.callee_frame(frame)
        self.assertEqual(listener.sent[-1], frame)
        await leg.on_stop("hung up")
        await leg.callee_frame(frame)
        self.assertEqual(len(listener.sent), 3)
        self.assertIn("listener_stopped", rec.events)


class RedialTests(unittest.TestCase):
    def test_only_unreached_calls_are_redialed(self):
        from voice_caller.runner import CallResult
        d = Path("unused")
        self.assertTrue(CallResult(d, final_status="no-answer").unreached)
        self.assertTrue(CallResult(d, final_status="busy").unreached)
        self.assertTrue(CallResult(d, final_status="completed", outcome={"outcome": "voicemail"}).unreached)
        self.assertFalse(CallResult(d, final_status="completed", outcome={"outcome": "success"}).unreached)
        self.assertFalse(CallResult(d, final_status="completed", outcome={"outcome": "failed"}).unreached)
        self.assertFalse(CallResult(d, outcome={"outcome": "no_answer"}, error="Twilio HTTP 400 code 21215").unreached)

    def test_only_early_hang_ups_get_a_callback(self):
        from voice_caller.runner import CallResult
        d = Path("unused")

        def result(outcome, end_reason, error="", mid=True):
            return CallResult(d, final_status="completed",
                              outcome={"outcome": outcome, "hung_up_mid_conversation": mid},
                              stats={"end_reason": end_reason, "turns": 4}, error=error)
        self.assertTrue(result("partial", "twilio sent stop").hung_up_early)
        self.assertFalse(result("failed", "twilio sent stop", mid=False).hung_up_early)
        self.assertTrue(result("failed", "twilio sent stop").hung_up_early)
        self.assertFalse(result("success", "twilio sent stop").hung_up_early)
        self.assertFalse(result("partial", "agent ended the call").hung_up_early)
        self.assertFalse(result("voicemail", "twilio sent stop").hung_up_early)
        self.assertFalse(result("partial", "twilio sent stop", error="aborted").hung_up_early)

    def test_callback_task_asks_if_they_meant_to_hang_up(self):
        from voice_caller.runner import CallResult, callback_task
        task = load_task(ROOT / "tasks" / "example-bakery-order.toml")
        task.details = '- Open with exactly: "Hi there!"\n- Ask about cookies.'
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "transcript.md").write_text(
                "# Call\n\n## Transcript\n\n```\n[00:01] THEM: Hello?\n[00:03] AGENT: Hi there!\n```\n")
            cb = callback_task(task, CallResult(Path(tmp)))
        self.assertIn('"Hey, did you mean to hang up?"', cb.details)
        self.assertNotIn("Hi there!", cb.details)
        self.assertIn("Ask about cookies.", cb.details)
        self.assertIn("[00:01] THEM: Hello?", cb.extra_context)
        self.assertEqual(cb.to, task.to)


if __name__ == "__main__":
    unittest.main()
