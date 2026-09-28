# elevenlabs agent caller

An AI agent that makes phone calls for you. You write a short task file (who to call, what you want, what you'd accept instead) and it dials the number, has the conversation out loud, hangs up, and leaves a transcript plus a JSON summary of what happened.

The example task is ordering cookies and a loaf of bread from a bakery for pickup. It also works for plain questions to a person.

- **Twilio** is the phone line. The REST API places the call and Media Streams carries the live audio over a WebSocket.
- **ElevenLabs** is the voice (text to speech) and the hearing (speech to text).
- **Claude** is the brain. It runs through the `claude` CLI you are already logged into, so there is no Anthropic API key.

Everything in between is in this repo: turn detection, barge-in, streaming the reply sentence by sentence, phone-menu key presses, holds, and knowing what the other person actually heard before they cut in.

```
[00:14] THEM: Sweet Crumb Bakery, this is Maria, how can I help you?
[00:18] AGENT: Hi Maria, I'm an AI assistant calling on behalf of Alex Kim to place a pickup order. Could I get
               a dozen chocolate chip cookies and a loaf of sourdough bread for pickup this Saturday?
[00:27] THEM: Okay, a dozen chocolate chip cookies, got it. Oh, I'm sorry, we're all out of sourdough for the
               weekend. We do have a country white loaf, it's seven fifty. Would that work instead?
[00:37] AGENT (interrupted): Yes, that country white loaf works --
[00:38] THEM: Oh, sorry to cut you off, is this for pickup or delivery?
[00:43] AGENT: Pickup, please.
```

That excerpt is from the simulated call (see [Dry run and tests](#dry-run-and-tests)).

## Where it stands

- It has placed about 40 real calls over Twilio from a Mac, to my own phone and to friends who knew it was coming. On those the gap between them finishing a sentence and the agent starting to talk was around 1.7 s (Claude Haiku 4.5, about 0.5 s to first token).
- It has not ordered from a real bakery yet. The bakery flow is only proven in the simulated call.
- Key presses on a real phone menu are untested. They work in the simulation.
- 38 offline tests pass, and the simulated end to end call passes with the real Claude brain.

## Quick start

Needs Python 3.12, `cloudflared`, the `claude` CLI logged in, a paid Twilio account with one number, and an ElevenLabs API key.

```bash
scripts/setup.sh                 # builds .venv
./vc dry-run --brain scripted    # full fake call, offline, no accounts needed
./vc dry-run                     # same call with the real Claude brain (~2 min)

mkdir -p ~/.config/voice-caller
install -m 600 secrets.env.example ~/.config/voice-caller/secrets.env   # then fill it in
./vc doctor --online             # checks keys, number, voice, tunnel
./vc voices                      # pick a voice id, put it in config.toml

cp tasks/templates/first-test-call.toml tasks/first-test-call.toml      # put YOUR number in `to`
./vc call tasks/first-test-call.toml          # prints the plan, dials nothing
./vc call tasks/first-test-call.toml --yes    # dials
```

Call yourself first and play the bakery. Tell it the sourdough is sold out, talk over it, put it on hold.

Useful flags on `vc call`: `--listen-phone` also rings your own phone so you can listen to both sides live, `--from` picks the caller ID, `--no-redial` and `--no-callback` turn off the one automatic retry.

## How it works

```
                 PSTN                    HTTPS (REST)             api.twilio.com
  their phone  <------> Twilio <---------------------------------  vc call  (your machine)
                          |  status webhooks  (HTTPS POST)  ----\     |
                          |  Media Stream     (WSS, mu-law) ----+-> public tunnel URL
                          v                                     |   (cloudflared / ngrok)
                                                                v
     +------------------------------ 127.0.0.1:8765 -----------------------------------------+
     |  server.py   /twilio/status (signature-checked)   /twilio/media (call-token-checked)  |
     |      |                                                                                 |
     |  session.py: 20 ms mu-law frames -> VAD (turn end / barge-in) -> utterance audio       |
     |      |                                                                                 |
     |      +-> ElevenLabs STT (scribe_v2) -> "<heard>text</heard>"                           |
     |      |                                                                                 |
     |      +-> brain.py: one warm `claude -p` process (stream-json, no tools)                |
     |      |        streams reply text + [[WAIT]] / [[END_CALL]] / [[DTMF:n]] markers        |
     |      |                                                                                 |
     |      +-> sentence chunks -> ElevenLabs TTS (flash v2.5, ulaw_8000) -> media frames     |
     |               + Twilio `mark` per chunk (what was heard) + `clear` on barge-in         |
     +-----------------------------------------------------------------------------------------+
```

In words, one call goes like this:

1. **Preflight, before any money is spent.**
   - `vc call` starts the local server and the public tunnel, and checks that the tunnel reaches `/health`.
   - It starts one `claude` process and warms it up. That warm-up proves the CLI login works.
   - It renders a short phrase with ElevenLabs TTS and transcribes it back. That proves the key, both scopes, and the voice.
   - If anything fails here, nothing is dialed.
2. **Dial.**
   - A single REST request creates the call.
   - The inline TwiML is `<Connect><Stream url="wss://…/twilio/media">`, with a one-time call token passed as a `<Parameter>`.
   - Twilio posts signed status callbacks: initiated, ringing, answered, completed.
3. **Listen.**
   - When they answer, Twilio opens the Media Stream and sends their audio as 20 ms frames of 8 kHz μ-law.
   - An energy VAD with an adaptive noise floor finds where their turn ends.
   - After 400 ms of silence the utterance is sent to ElevenLabs speech-to-text speculatively. After 600 ms the turn is committed, and the transcript is usually already back.
4. **Think.** The transcript goes to Claude, wrapped in `<heard>` tags and treated as untrusted. The brain process has no tools, no MCP, and no settings, and its environment has the Twilio and ElevenLabs keys removed.
5. **Speak.**
   - Claude's reply streams back. The first clause goes to TTS as soon as it's complete, and then each following sentence.
   - ElevenLabs returns `ulaw_8000`, which is Twilio's own format, so the audio is forwarded without transcoding.
   - A `mark` after each chunk tells us exactly what the other person actually heard.
6. **Turn-taking.**
   - If they talk over the agent for 400 ms, it stops (Twilio `clear`), and Claude is told exactly what they heard.
   - If they resume talking before the agent's reply becomes audible, that reply is dropped.
   - "Mm-hm"s are passed along as notes, and the agent keeps talking.
   - Silence nudges let Claude choose between speaking and `[[WAIT]]`, for example while on hold.
   - `[[DTMF:2]]` presses keys on phone menus.
   - If Claude is ever slow (over 3 s), a pre-rendered "One moment." fills the gap.
7. **Hang up.** After its goodbye, Claude emits `[[END_CALL]]`. The agent waits until the goodbye has actually played, then ends the call through the REST API.
8. **Record.**
   - `calls/<stamp>-<business>/` gets `transcript.md`, `outcome.json`, `call.json` (stats and latencies), and `events.jsonl` (written live).
   - `outcome.json` is a structured summary from a separate Claude call: order placed, items, total, pickup time, name, follow-ups.

**If nobody picks up** it calls once more right away, since a quick repeat call gets through Do Not Disturb. **If they hang up partway** it calls back once and asks if they meant to. Both are in `[calls]` in `config.toml`.

Guardrails:
- It refuses 911, N11, 988, premium-rate and international numbers.
- It dials nothing without `--yes`.
- Twilio's `TimeLimit` cuts every call at 8 minutes.
- There is a turn cap.
- Any crash leads to a spoken apology and a hang-up. A call is never left open and mute.
- Secret values are redacted from every log and record.

## Setup notes

**Twilio.** The account has to be upgraded (paid). Trial accounts strip `<Stream>` out of the TwiML, so this design cannot run on a trial. Buy one US local number with Voice (about $1.15 a month) and check that US geo permissions are on. A new personal number can show up as "Spam Likely" on the other end.

**ElevenLabs.** The free plan is enough to start. The API key needs four scopes: Text to Speech, Speech to Text, Voices read, User read. Run `./vc voices` to see which voice ids your key can use and `./vc tts-sample "some text"` to hear one at phone quality.

**Secrets.** Four lines in `~/.config/voice-caller/secrets.env`, mode 600 (template in `secrets.env.example`). Real environment variables override the file, and `VOICE_CALLER_SECRETS_FILE` moves it. `--listen-phone` also needs `LISTEN_TO` (your phone) and `LISTEN_FROM` (a Twilio number you own).

### Tunnel

Twilio has to reach your machine over public HTTPS and WSS for the whole call, so a tunnel is needed. A dropped tunnel is a dropped call.

- `mode = "cloudflared-quick"` (default): `vc call` starts a Cloudflare Quick Tunnel for the call and stops it after. No account. The hostname is random each time, which is fine because every call hands its URLs to Twilio.
- `mode = "static"`: you run the tunnel yourself (ngrok with a free static domain works) and put the URL in `public_url`.

Some networks cache "no such host" for the brand-new tunnel hostname, so the tunnel looks dead for a minute. `tunnel.py` resolves it over DNS-over-HTTPS instead of waiting.

`./vc tunnel-test` checks the whole path without placing a call.

## Costs

List prices from Aug to Sep 2026. A typical 2 to 3 minute call costs about 4 to 6 cents on Twilio ($0.014/min voice plus $0.0044/min Media Streams, rounded up to whole minutes). The 8 minute cap puts the worst case around 15 cents. ElevenLabs free tier covers roughly 10 to 25 calls a month. Claude runs on the CLI login, so it counts against that plan and nothing else.

## Dry run and tests

```bash
./vc dry-run                    # full simulated call, real Claude brain via the CLI (~2 min); must print RESULT: PASS
./vc dry-run --brain scripted   # fully offline, deterministic brain
scripts/test.sh                 # 38 unit + integration tests, offline, ~40 s
```

The dry run runs the real server, session, brain, runner and recorder. The fakes stand in for the outside world.

**Fake Twilio** (`sim/fake_twilio.py`):
- It handles the REST calls (`Calls.json`, hang-up, account and number lookups), checks Basic auth, and parses the inline TwiML.
- It sends **signed** status callbacks.
- It dials back into the server's media WebSocket exactly as Twilio documents: `connected`/`start` with `customParameters`, 20 ms inbound frames including silence, outbound audio played at real-time speed, marks echoed when playback reaches them, and `clear` flushing the buffer and returning its marks.

**Fake ElevenLabs** (`sim/fake_elevenlabs.py`):
- TTS streams `ulaw_8000` after a 150 ms time-to-first-byte.
- STT accepts the multipart WAV upload.
- Both check the key and the request shape.

**The audio is real.** Text is "spoken" as a CRC-checked 16-tone FSK signal (`sim/fsk.py`). The fake STT and the fake employee's ears demodulate it back. So every byte crosses the actual pipeline (VAD, speculative STT, marks, barge-in truncation, DTMF), and any corruption fails a check.

**The scripted bakery employee** (`sim/bakery.py`) runs this script:
1. A phone menu ("press 2").
2. A greeting, which checks the AI disclosure.
3. She interrupts the agent once, mid-sentence.
4. Sourdough is sold out and she offers country white.
5. She asks the pickup time and name.
6. A 9 s hold.
7. A read-back with a 550 ms mid-sentence pause.
8. A goodbye, after which the agent must hang up.

It fails if the agent talks over the hold, ignores the substitution rule, or doesn't hang up.

**Credentials in the dry run** are throwaway values generated on the spot, and every record is scanned afterwards to prove none of them leaked.

Excerpt from the final Claude-brain dry run (`calls/dry-runs/20260927-160952-sweet-crumb-bakery-dryrun/transcript.md`):

```
[00:05] THEM: Thank you for calling Sweet Crumb Bakery. ... To place an order or to speak with someone, press 2.
          (agent pressed 2)
[00:14] THEM: Sweet Crumb Bakery, this is Maria, how can I help you?
[00:18] AGENT: Hi Maria, I'm an AI assistant calling on behalf of Alex Kim to place a pickup order. Could I get
               a dozen chocolate chip cookies and a loaf of sourdough bread for pickup this Saturday?
[00:27] THEM: Okay, a dozen chocolate chip cookies, got it. Oh, I'm sorry, we're all out of sourdough for the
               weekend. We do have a country white loaf, it's seven fifty. Would that work instead?
[00:37] AGENT (interrupted): Yes, that country white loaf works --
[00:38] THEM: Oh, sorry to cut you off, is this for pickup or delivery?
[00:43] AGENT: Pickup, please.
  ...
[01:04] THEM: Perfect. Let me just double-check we'll have enough chocolate chip for Saturday. Hang on one sec.
[01:11] AGENT: Sure, no problem.                       (then silent through the 9 s hold)
[01:17] THEM: Okay, sorry about that, we're all set. So that's a dozen chocolate chip cookies [550 ms pause]
               and one country white loaf, for pickup Saturday at ten, under Alex Kim. Your total comes to
               twenty-five fifty, and you can pay when you pick up. Anything else?
[01:31] AGENT: That's everything, thank you! Just to confirm: a dozen chocolate chip cookies and one country
               white loaf, total twenty-five fifty, pickup Saturday at ten a.m., under the name Alex Kim, ...
[01:41] THEM: Great! We'll see you Saturday, Alex. Bye now!
[01:46] AGENT: Thank you so much, bye!                 -> hang-up via REST, status completed
```

## Configuration

`config.toml` holds the non-secret settings. Every key is optional and the defaults are in `voice_caller/config.py`. Unknown keys are rejected, which catches typos.

- `[brain] model`: `claude-haiku-4-5` by default because it answers fastest. `--model claude-sonnet-5` for harder calls. The post-call summary uses a bigger model since nobody is waiting on it.
- `[elevenlabs]`: `voice_id` (required), `tts_model`, `stt_model`, `speed`.
- `[twilio]`: `time_limit_s = 480` is the hard cost cap.
- `[calls]`: `redial_if_unreached`, `callback_if_hung_up`.

### Tuning

All of these live under `[turns]`:

| Symptom | Setting |
|---|---|
| Agent cuts people off mid-thought | Raise `endpoint_silence_ms` (600 → 900). |
| Agent feels slow to answer | Lower `endpoint_silence_ms` (→ 500). |
| Background noise triggers turns or barge-ins | Raise `min_speech_rms` (compare with `vad` in `call.json`), or `barge_in_min_ms`. |
| Agent stops talking at every "mm-hm" | Raise `barge_in_min_ms` (400 → 600). |
| It hears its own voice echoed back | Set `echo_guard_ratio` to 1.5–2.0. |
| Filler phrase unwanted | Set `stall_filler_s = 0`. |

## Safety, legal and etiquette (not legal advice)

**Disclosure.**
- By default (`disclose_ai = true` in the task) the agent says in its first turn that it's an AI assistant calling on behalf of the person in the task.
- If asked whether it's an AI, it always says yes.
- 47 CFR 64.1200(b) expects artificial-voice calls to identify the caller up front and give a callback number. Set `callback_number` in the task, and the agent gives it when confirming the details.

**Consent.**
- The TCPA requires prior consent for artificial-voice calls to **cell phones** and residential lines.
- Calling a business landline to place an order is not telemarketing and falls outside the residential rule. A small business that answers on a mobile number is technically covered.
- For your own test phone, you are the one consenting.

**Payment.** The agent never gives payment details; it has none, and it asks to pay at pickup instead.

**Recording.** No audio is recorded. Text transcripts are kept locally in `calls/` (gitignored). Treat them as personal data.

**Hard limits.** The code enforces:
- no emergency, N11, 988, premium-rate or international numbers
- nothing is dialed without `--yes`
- an 8-minute Twilio `TimeLimit`
- a turn cap
- one call at a time (fixed port)

## Known limits

- **VAD.** It is an energy VAD with an adaptive noise floor, not a trained model. Loud rooms may need `min_speech_rms` raised.
- **DTMF.** Media Streams cannot send DTMF, so `[[DTMF:n]]` plays the tones in the audio. Unverified on real menus. For a menu you already know, `send_digits` in the task uses Twilio's own `SendDigits`.
- **Echo.** No echo cancellation of its own. `echo_guard_ratio` helps if it hears itself.
- **Summary schema.** `outcome.json` still has order fields (items, total, pickup time) even for calls that are not orders.
- **One call at a time.** The port is fixed.

## Files

```
vc                        entry point (uses .venv)          config.toml          non-secret settings
voice_caller/
  __main__.py             CLI: doctor, dry-run, voices, tts-sample, tunnel-test, call
  runner.py               one call end to end: preflight, dial, wait, summarize, record
  session.py              live call: VAD -> STT -> brain -> TTS, barge-in, silence, hang-up
  brain.py                persistent `claude -p` stream-json process; one-shot JSON summary
  prompts.py              system prompt from the task, message wrappers, summary schema
  server.py               aiohttp: /health, /twilio/status, /twilio/media
  twilio_api.py           REST client, signature check, TwiML
  elevenlabs.py           TTS (ulaw_8000 stream) and STT (scribe_v2)
  listen.py               listen-in leg: a second call to your phone that hears both sides
  vad.py  audio.py        turn detection; mu-law codec, WAV, DTMF
  speech_text.py          streaming sentence chunker, [[markers]]
  tunnel.py               cloudflared quick tunnel / static URL, DoH resolver
  task.py  config.py      task files and dialing guardrails; config + secrets loading
  recorder.py logs.py console.py   call records, redacting logs, live transcript
sim/                      fake Twilio, fake ElevenLabs, FSK modem, bakery employee, dry run
tasks/                    example-bakery-order.toml, templates/first-test-call.toml
tests/                    unit + integration tests (scripts/test.sh)
scripts/                  setup.sh, test.sh, scheduled-call.sh (macOS, timed calls from a LaunchAgent)
calls/                    per-call records and logs (gitignored)
```
