"""Live, human-readable view of a call on stdout (the records on disk are the source of truth)."""
from __future__ import annotations

_SHOWN_EVENTS = {
    "twilio_status": lambda e: f"status: {e.get('status')}",
    "barge_in": lambda e: "(they talked over the agent; agent stopped)",
    "reply_dropped": lambda e: "(they kept talking; agent's reply dropped)",
    "dtmf_sent": lambda e: f"(agent pressed {e.get('digits')})",
    "agent_waits": lambda e: "(agent stays quiet)",
    "stall_filler": lambda e: f"(brain slow: filler after {e.get('after_s')} s)",
    "nudge": lambda e: f"(silence: {e.get('text')})",
    "hangup_requested": lambda e: f"hang-up requested: {e.get('reason')}",
    "stt_error": lambda e: f"speech-to-text error: {e.get('error')}",
    "error": lambda e: f"ERROR: {e.get('error')}",
    "stream_upgrade_signature": lambda e: f"media stream connected (upgrade signature: {e.get('state')})",
    "listener_dialed": lambda e: "(ringing your phone to listen in)",
    "listener_connected": lambda e: "(you are listening in)",
    "listener_stopped": lambda e: "(listener hung up)",
}


def attach_console(session, echo=print) -> None:
    rec = session.rec
    orig_say, orig_event = rec.say, rec.event

    def stamp(t: float) -> str:
        m, s = divmod(int(t), 60)
        return f"[{m:02d}:{s:02d}]"

    def say(who, text, t=None, **meta):
        orig_say(who, text, t=t, **meta)
        tag = " (cut off)" if meta.get("interrupted") else " (over agent)" if meta.get("backchannel") else ""
        echo(f"{stamp(rec.now() if t is None else t)} {'AGENT' if who == 'agent' else 'THEM '}{tag}: {text}")

    def event(kind, **data):
        orig_event(kind, **data)
        fmt = _SHOWN_EVENTS.get(kind)
        if fmt:
            echo(f"{stamp(rec.now())}   {fmt(data)}")

    rec.say, rec.event = say, event
