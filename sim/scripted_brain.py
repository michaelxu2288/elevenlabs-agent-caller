"""A deterministic stand-in for the Claude brain (offline pipeline tests only).

Same interface as voice_caller.brain.ClaudeCliBrain; replies come from keyword rules
tuned to the sample bakery task, streamed in small deltas like the real thing.
"""
from __future__ import annotations

import asyncio
import re

from voice_caller.brain import BrainTurn

RULES = [
    (r"press 2|press two", "[[DTMF:2]]"),
    (r"pickup or delivery", "It's for pickup. I'd like a dozen chocolate chip cookies and one sourdough loaf, please."),
    (r"how can i help", "Hi Maria, I'm an AI assistant calling on behalf of Alex Kim. I'd like to place an order "
                        "for pickup, please."),
    (r"what can i get", "I'd like a dozen chocolate chip cookies and one sourdough loaf, please, for pickup."),
    (r"out of sourdough", "Oh, no problem. The country white sounds great, thank you."),
    (r"country white loaf instead", "Yes, the country white works, thank you."),
    (r"when would you like", "Saturday at ten a.m., please."),
    (r"what name", "Alex Kim, please."),
    (r"hang on|one sec", "Sure, no problem. [[WAIT]]"),
    (r"anything else", "No, that's everything. That's a dozen chocolate chip cookies and a country white loaf, "
                       "pickup Saturday at ten, under Alex Kim. Thanks so much!"),
    (r"\bbye\b", "Thanks, Maria. Bye! [[END_CALL]]"),
    (r"nobody has spoken|no one has said", "Hi, is this Sweet Crumb Bakery?"),
]


class ScriptedBrain:
    def __init__(self, delay_s: float = 0.35):
        self.delay_s = delay_s
        self.turn_log: list[dict] = []
        self._n = 0
        self.messages: list[str] = []

    async def start(self) -> None:
        pass

    async def warmup(self, message: str) -> BrainTurn:
        turn = BrainTurn(0, message)
        turn._finish({})
        return turn

    def _reply_for(self, message: str) -> str:
        heard = " ".join(re.findall(r"<heard>(.*?)</heard>", message, re.S)).lower()
        events = " ".join(re.findall(r"<event>(.*?)</event>", message, re.S)).lower()
        for pattern, reply in RULES:
            if re.search(pattern, heard):
                return reply
        for pattern, reply in RULES:
            if re.search(pattern, events):
                return reply
        return "[[WAIT]]" if not heard else "Sorry, could you say that again?"

    async def ask(self, message: str) -> BrainTurn:
        self._n += 1
        self.messages.append(message)
        turn = BrainTurn(self._n, message)
        asyncio.ensure_future(self._stream(turn, self._reply_for(message)))
        return turn

    async def _stream(self, turn: BrainTurn, reply: str) -> None:
        await asyncio.sleep(self.delay_s)
        for piece in re.findall(r"\S+\s*", reply):
            if turn.cancelled:
                break
            turn._delta(piece)
            await asyncio.sleep(0.01)
        turn._finish({})
        self.turn_log.append({"turn": turn.id, "ttft_ms": turn.ttft_ms, "cancelled": turn.cancelled})

    def cancel(self, turn: BrainTurn) -> None:
        turn.cancelled = True
        turn._q.put_nowait(None)

    async def close(self) -> None:
        pass
