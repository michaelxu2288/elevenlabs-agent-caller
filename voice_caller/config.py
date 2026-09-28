"""Configuration: non-secret settings from config.toml, secrets from secrets.env.

Secrets live outside the project in one plain KEY=value file (mode 600). Resolution order for each secret: the
process environment first, then $VOICE_CALLER_SECRETS_FILE, then
~/.config/voice-caller/secrets.env. Secret values are never printed or
logged; only key names and file status are ever reported.
"""
from __future__ import annotations

import dataclasses
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = PROJECT_DIR / "config.toml"
DEFAULT_SECRETS_FILE = Path.home() / ".config" / "voice-caller" / "secrets.env"

# name -> what it is. Everything here is required to place a real call.
REQUIRED_SECRETS = {
    "TWILIO_ACCOUNT_SID": "Twilio Account SID (starts with AC)",
    "TWILIO_AUTH_TOKEN": "Twilio Auth Token (REST auth and webhook signature checks)",
    "TWILIO_FROM_NUMBER": "your Twilio phone number in E.164, e.g. +15125550123",
    "ELEVENLABS_API_KEY": "ElevenLabs API key (text-to-speech + speech-to-text)",
}
# values that must never appear in logs or call records (the from-number is not secret)
REDACTED_SECRETS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "ELEVENLABS_API_KEY")


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class TunnelConfig:
    # "cloudflared-quick": start a Cloudflare Quick Tunnel for each call (no account needed)
    # "static": you run your own tunnel (ngrok static domain, named Cloudflare tunnel)
    #           and put its https URL in public_url
    mode: str = "cloudflared-quick"
    public_url: str = ""
    cloudflared_bin: str = "cloudflared"
    start_timeout_s: float = 45.0


@dataclass
class TwilioConfig:
    api_base: str = "https://api.twilio.com"
    ring_timeout_s: int = 30          # how long to let it ring before giving up
    time_limit_s: int = 480           # hard cap on call length (Twilio TimeLimit): cost guard
    validate_signatures: bool = True  # check X-Twilio-Signature on status webhooks
    outbound_frame_bytes: int = 160   # 20 ms of 8 kHz mu-law per media message


@dataclass
class ElevenLabsConfig:
    api_base: str = "https://api.elevenlabs.io"
    voice_id: str = ""                # must be a voice your key can use: see `./vc voices`
    tts_model: str = "eleven_flash_v2_5"
    stt_model: str = "scribe_v2"
    language_code: str = "en"
    stability: float = 0.5
    similarity_boost: float = 0.75
    speed: float = 1.0
    request_timeout_s: float = 20.0


@dataclass
class BrainConfig:
    claude_bin: str = "claude"
    model: str = "claude-sonnet-5"
    effort: str = "low"
    summary_model: str = "claude-opus-5"
    thinking: bool = False            # off halves time-to-first-word (~1.1 s -> ~0.6 s measured)
    turn_timeout_s: float = 25.0


@dataclass
class TurnConfig:
    min_speech_rms: float = 300.0       # absolute floor for "someone is talking" (PCM16 RMS)
    speech_to_noise_ratio: float = 3.0  # or this many times the tracked noise floor
    start_frames: int = 3               # 60 ms of speech before a turn starts
    preroll_ms: int = 200
    min_utterance_ms: int = 200         # shorter blips (clicks, coughs) are dropped
    speculative_stt_ms: int = 400       # start transcribing after this much silence...
    endpoint_silence_ms: int = 700      # ...and commit the turn after this much
    max_utterance_ms: int = 30000
    barge_in_min_ms: int = 400          # their speech needed to cut the agent off mid-sentence
    resume_cancel_ms: int = 160         # their speech that cancels a reply not yet audible
    echo_guard_ratio: float = 1.0       # threshold multiplier while the agent is talking
    agent_speaks_first_after_s: float = 4.0
    silence_nudge_s: float = 12.0
    early_first_chunk: bool = True      # voice the reply's first clause before its sentence is finished
    stall_filler_s: float = 3.0         # brain silent this long after they spoke -> say the filler (0 = off)
    stall_filler_text: str = "One moment."


@dataclass
class SafetyConfig:
    allow_international: bool = False
    require_confirmation: bool = True
    max_turns: int = 80


@dataclass
class CallDefaults:
    timezone: str = "America/Chicago"
    output_dir: str = "calls"
    redial_if_unreached: bool = True  # dnd repeat rule
    callback_if_hung_up: bool = True  # one callback


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    twilio: TwilioConfig = field(default_factory=TwilioConfig)
    elevenlabs: ElevenLabsConfig = field(default_factory=ElevenLabsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    turns: TurnConfig = field(default_factory=TurnConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    calls: CallDefaults = field(default_factory=CallDefaults)

    @property
    def output_dir(self) -> Path:
        p = Path(self.calls.output_dir)
        return p if p.is_absolute() else PROJECT_DIR / p


class ConfigError(ValueError):
    pass


def _coerce(section: str, obj, values: dict) -> None:
    fields = {f.name: f for f in dataclasses.fields(obj)}
    for key, value in values.items():
        if key not in fields:
            raise ConfigError(f"[{section}] unknown key {key!r} (known: {', '.join(fields)})")
        current = getattr(obj, key)
        if isinstance(current, bool):
            ok = isinstance(value, bool)
        elif isinstance(current, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        elif isinstance(current, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, type(current))
        if not ok:
            raise ConfigError(f"[{section}] {key} should be {type(current).__name__}, got {value!r}")
        setattr(obj, key, value)


def load_config(path: Path | None = None, overrides: dict | None = None) -> Config:
    """config.toml merged over the defaults above, then `overrides` ({section: {key: value}})."""
    cfg = Config()
    path = Path(path) if path else DEFAULT_CONFIG_FILE
    data = {}
    if path.exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    for source in (data, overrides or {}):
        for section, values in source.items():
            if not hasattr(cfg, section) or not isinstance(values, dict):
                raise ConfigError(f"unknown config section [{section}]")
            _coerce(section, getattr(cfg, section), values)
    if cfg.tunnel.mode not in ("cloudflared-quick", "static", "none"):
        raise ConfigError("[tunnel] mode must be cloudflared-quick, static or none")
    return cfg


# ---------------------------------------------------------------- secrets

@dataclass
class SecretsStatus:
    path: Path
    state: str               # "ok", "missing", "unreadable", "not-set"
    detail: str = ""
    loose_permissions: bool = False


def secrets_path() -> Path:
    return Path(os.environ.get("VOICE_CALLER_SECRETS_FILE") or DEFAULT_SECRETS_FILE)


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=value lines; blank lines and # comments ignored; optional matching quotes stripped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


class Secrets:
    """Holds secret values without ever exposing them through repr/str."""

    def __init__(self, values: dict[str, str], status: SecretsStatus):
        self._values = dict(values)
        self.status = status

    def get(self, key: str) -> str:
        return self._values.get(key, "")

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def missing(self, keys=REQUIRED_SECRETS) -> list[str]:
        return [k for k in keys if not self._values.get(k)]

    def redaction_values(self) -> list[str]:
        return [self._values[k] for k in REDACTED_SECRETS if len(self._values.get(k, "")) >= 6]

    def __repr__(self) -> str:
        present = sorted(k for k, v in self._values.items() if v)
        return f"Secrets(present={present}, file={self.status.path}, state={self.status.state})"

    __str__ = __repr__


def load_secrets(path: Path | None = None, environ: dict | None = None) -> Secrets:
    """Environment wins over the file, like the other projects on this box."""
    environ = os.environ if environ is None else environ
    path = Path(path) if path else secrets_path()
    file_values: dict[str, str] = {}
    try:
        st = path.stat()
        file_values = parse_env_file(path.read_text(encoding="utf-8"))
        status = SecretsStatus(path, "ok",
                               loose_permissions=bool(st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)))
    except FileNotFoundError:
        status = SecretsStatus(path, "missing", "file does not exist")
    except PermissionError:
        status = SecretsStatus(path, "unreadable", _permission_hint(path))
    values = {}
    for key in set(REQUIRED_SECRETS) | set(file_values):
        v = environ.get(key) or file_values.get(key, "")
        if v:
            values[key] = v
    return Secrets(values, status)


def _permission_hint(path: Path) -> str:
    """Name the first directory on the way to `path` this user cannot enter."""
    p = Path("/")
    for part in path.parts[1:-1]:
        p = p / part
        if not os.access(p, os.X_OK):
            try:
                st = p.stat()
                mode = stat.filemode(st.st_mode)
            except OSError:
                mode = "?"
            return (f"this user ({os.environ.get('USER', os.getuid())}) cannot enter {p} "
                    f"({mode}); see README 'Secrets' for the one-time fix")
    return "file exists but is not readable by this user"
