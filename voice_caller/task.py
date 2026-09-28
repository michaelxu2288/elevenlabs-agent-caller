"""Call task files (tasks/*.toml): who to call, on whose behalf, and what to get done.

Also the dialing guardrails: US/Canada (NANP) numbers only unless international is
enabled, and never emergency, N11 or premium-rate numbers.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class TaskError(ValueError):
    pass


@dataclass
class CallTask:
    to: str
    business_name: str
    on_behalf_of: str
    goal: str
    details: str = ""
    constraints: str = ""
    callback_number: str = ""
    disclose_ai: bool = True
    voicemail: str = "hang_up"          # "hang_up" or "leave_message"
    voicemail_message: str = ""
    timezone: str = ""
    extra_context: str = ""
    send_digits: str = ""               # keys to press right after answer (known phone menu)
    source: Path | None = None
    simulated: bool = False             # set by the dry run; relaxes the fictional-555 check
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.business_name.lower()).strip("-")[:40] or "call"


_TASK_KEYS = {
    "call": {"to", "business_name", "send_digits"},
    "caller": {"on_behalf_of", "callback_number", "disclose_ai"},
    "goal": {"summary", "details", "constraints", "extra_context"},
    "voicemail": {"policy", "message"},
    "options": {"timezone"},
}


def load_task(path: Path, to_override: str | None = None) -> CallTask:
    path = Path(path)
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    for section, values in data.items():
        if section not in _TASK_KEYS:
            raise TaskError(f"{path.name}: unknown section [{section}]")
        unknown = set(values) - _TASK_KEYS[section]
        if unknown:
            raise TaskError(f"{path.name}: unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")

    def need(section, key):
        v = str(data.get(section, {}).get(key, "")).strip()
        if not v:
            raise TaskError(f"{path.name}: [{section}] {key} is required")
        return v

    vm = data.get("voicemail", {})
    task = CallTask(
        to=to_override or need("call", "to"),
        business_name=need("call", "business_name"),
        on_behalf_of=need("caller", "on_behalf_of"),
        goal=need("goal", "summary"),
        details=str(data.get("goal", {}).get("details", "")).strip(),
        constraints=str(data.get("goal", {}).get("constraints", "")).strip(),
        extra_context=str(data.get("goal", {}).get("extra_context", "")).strip(),
        callback_number=str(data.get("caller", {}).get("callback_number", "")).strip(),
        disclose_ai=bool(data.get("caller", {}).get("disclose_ai", True)),
        voicemail=str(vm.get("policy", "hang_up")),
        voicemail_message=str(vm.get("message", "")).strip(),
        timezone=str(data.get("options", {}).get("timezone", "")),
        send_digits=str(data.get("call", {}).get("send_digits", "")).strip(),
        source=path,
        raw=data,
    )
    if task.send_digits and not re.fullmatch(r"[0-9A-D*#wW]{1,32}", task.send_digits):
        raise TaskError(f"{path.name}: [call] send_digits may only contain 0-9 A-D * # w W (max 32)")
    if task.voicemail not in ("hang_up", "leave_message"):
        raise TaskError(f"{path.name}: [voicemail] policy must be hang_up or leave_message")
    if task.voicemail == "leave_message" and not task.voicemail_message:
        raise TaskError(f"{path.name}: [voicemail] message is required when policy = leave_message")
    return task


# ---------------------------------------------------------------- number safety

_EMERGENCY = {"911", "112", "999", "988", "933"}
_PREMIUM_NPA = {"900", "976"}


def normalize_number(raw: str) -> str:
    """'+1 (512) 555-0123', '512.555.0123', '15125550123' -> '+15125550123'."""
    s = raw.strip()
    if re.search(r"[A-Za-z]", s):
        raise TaskError(f"phone number {raw!r} contains letters; spell it out in digits")
    digits = re.sub(r"\D", "", s)
    if s.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise TaskError(f"cannot read {raw!r} as a phone number; use E.164 like +15125550123")


def check_dialable(number: str, *, allow_international: bool = False, simulated: bool = False,
                   own_number: str = "") -> str:
    """Return the normalized number or raise TaskError explaining why it must not be dialed."""
    raw_digits = re.sub(r"\D", "", number)
    if raw_digits in _EMERGENCY or (len(raw_digits) == 3 and raw_digits.endswith("11")):
        raise TaskError(f"refusing to dial emergency/N11 number {number}")
    e164 = normalize_number(number)
    if own_number and e164 == normalize_number(own_number):
        raise TaskError("refusing to dial the Twilio number itself")
    if not e164.startswith("+1"):
        if not allow_international:
            raise TaskError(f"{e164} is outside the US/Canada; set [safety] allow_international = true to allow")
        if not re.fullmatch(r"\+[2-9]\d{6,14}", e164):
            raise TaskError(f"{e164} is not a valid E.164 number")
        return e164
    nanp = e164[2:]
    if not re.fullmatch(r"\d{10}", nanp):
        raise TaskError(f"{e164} is not a 10-digit US/Canada number")
    npa, nxx, line = nanp[:3], nanp[3:6], nanp[6:]
    if npa[0] in "01" or nxx[0] in "01":
        raise TaskError(f"{e164} is not a valid NANP number (area code/exchange cannot start with 0 or 1)")
    if npa[1:] == "11" or nxx[1:] == "11":
        raise TaskError(f"refusing {e164}: N11 service codes are never dialed")
    if npa in _PREMIUM_NPA or nxx == "976":
        raise TaskError(f"refusing {e164}: premium-rate number")
    if nxx == "555" and line.startswith("01") and not simulated:
        raise TaskError(f"{e164} is a fictional 555-01xx number; use the business's real number")
    return e164
