"""The scripted fake bakery employee on the other end of the simulated call.

Maria at Sweet Crumb Bakery. The scenario exercises the hard parts of a real call:
  1. a phone menu ("to place an order, press 2")       -> agent must send DTMF
  2. greeting                                          -> agent should disclose it's an AI
  3. she cuts the agent off mid-sentence once           -> barge-in / Twilio clear
  4. sourdough is sold out, country white offered       -> agent applies the task's substitution rule
  5. pickup time and name questions
  6. "hang on one sec" + 9 s of silence                 -> agent must not talk over the hold
  7. read-back with a 550 ms pause mid-sentence          -> must not be split into two turns
  8. goodbye                                            -> agent should say bye and hang up
She is keyword-driven, tolerant of phrasing, and keeps a verdict of what she heard.
"""
from __future__ import annotations

import asyncio
import re

HOUR = {"nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "noon": 12, "9": 9, "10": 10, "11": 11, "12": 12}
YES = re.compile(r"\b(yes|yeah|yep|sure|okay|ok|sounds good|that works|that'?s fine|that'?s great|perfect|"
                 r"great|country white|let'?s do|we'?ll take|i'?ll take|that would be)\b")
NO = re.compile(r"\b(no thanks|no,? thank|skip|pass on|don'?t need|without the bread|never ?mind|not needed|"
                r"leave the bread)\b")
DONE = re.compile(r"(that'?s (all|everything|it)|nothing else|no,? that'?s|that will be all|that should be it|"
                  r"that'?s all for|we'?re all set|all set|no,? thank)")
BYE = re.compile(r"\b(bye|goodbye|have a (great|good|nice|wonderful) (day|one|weekend|afternoon))\b")
AI = re.compile(r"\b(a\.?i\.?|artificial|automated|virtual assistant|ai assistant|assistant)\b")


class BakeryEmployee:
    HOLD_S = 9.0

    def __init__(self, phone, log=print):
        self.phone = phone
        self.log = log
        self.q: asyncio.Queue = asyncio.Queue()
        self.stage = "ivr"
        self.expect = ""
        self.cookie_qty: int | None = None
        self.cookie_kind: str | None = None
        self.sourdough_requested = False
        self.offered_sub = False
        self.sub_reasks = 0
        self.bread: str | None = None
        self.saturday_mentioned = False
        self.pickup: str | None = None
        self.name: str | None = None
        self.ordering_turns = 0
        self.agent_turns = 0
        self.first_agent_line = ""
        self.did_interrupt = False
        self.interrupting = False
        self.answered_pickup = False
        self.on_hold = False
        self.hold_lines: list[str] = []
        self.did_hold = False
        self.readback_done = False
        self.said_goodbye = False
        self.agent_said_bye = False
        self.agent_hung_up = False
        self.pressed = []
        self.repeats = 0
        self.heard_log: list[str] = []
        self.said_log: list[str] = []
        self.end_reason = ""
        self._ivr_heard_digit = asyncio.Event()

    # ---- callbacks from the phone line
    def on_heard(self, text: str, errors: list[str]) -> None:
        self.q.put_nowait(("heard", text, errors))

    def on_agent_audio_start(self) -> None:
        if (self.stage == "ordering" and self.ordering_turns >= 1 and not self.did_interrupt
                and not self.on_hold and not self.phone.speaking):
            self.did_interrupt = True
            asyncio.ensure_future(self._interrupt_soon())

    def on_dtmf(self, digit: str) -> None:
        self.pressed.append(digit)
        if self.stage == "ivr" and digit == "2":
            self._ivr_heard_digit.set()

    def on_call_end(self, reason: str) -> None:
        self.end_reason = reason
        self.agent_hung_up = reason == "hung up via API"

    # ---- speaking
    async def say(self, *segments, wait: bool = True) -> None:
        text = " ".join(s for s in segments if isinstance(s, str))
        self.said_log.append(text)
        self.log(f"   bakery> {text}")
        self.phone.say(list(segments))
        if wait:
            await self.phone.until_quiet()

    async def _interrupt_soon(self) -> None:
        await self.phone.sleep(1.2)
        if self.phone.agent_speaking and not self.phone.ended.is_set():
            self.interrupting = True
            self.expect = "pickup_or_delivery"
            await self.say("Oh, sorry to cut you off, is this for pickup or delivery?")
        else:
            self.did_interrupt = False   # it was too short to interrupt; try the next one

    # ---- the script
    async def run(self) -> None:
        await self.phone.sleep(0.6)
        await self.say("Thank you for calling Sweet Crumb Bakery. For our hours and location, press 1. "
                       "To place an order or to speak with someone, press 2.")
        try:
            await asyncio.wait_for(self._ivr_heard_digit.wait(), timeout=20 / self.phone.speed)
        except asyncio.TimeoutError:
            self.log("   bakery: (nobody pressed 2; picking up anyway)")
        self.stage = "greeting"
        await self.phone.sleep(1.0)
        # drop anything said to the menu recording
        while not self.q.empty():
            self.q.get_nowait()
        await self.say("Sweet Crumb Bakery, this is Maria, how can I help you?")
        while not self.phone.ended.is_set():
            kind, text, errors = await self.q.get()
            if kind == "heard":
                await self.handle(text, errors)

    async def handle(self, text: str, errors: list[str]) -> None:
        self.log(f"   bakery heard: {text!r}" + (f" (+{len(errors)} garbled)" if errors else ""))
        self.heard_log.append(text)
        if self.interrupting:
            # the cut-off half-sentence; she's already asked her question
            self.interrupting = False
            return
        if self.on_hold:
            self.hold_lines.append(text)
            return
        if not text:
            if self.repeats < 2:
                self.repeats += 1
                await self.say("Sorry, you cut out there. Could you say that again?")
            return
        t = text.lower()
        self.agent_turns += 1
        if not self.first_agent_line and self.stage in ("greeting", "ordering"):
            self.first_agent_line = text
        if BYE.search(t):
            self.agent_said_bye = True
        self._extract(t)
        if self.agent_turns > 30:
            await self.say("Sorry, I've got a line of customers, I have to go. Bye.")
            self.phone.hang_up()
            return
        reply = self._respond(t)
        if reply == "HOLD":
            await self._hold_then_readback()
        elif reply:
            await self.phone.sleep(0.3)
            await self.say(*reply)
        if self.stage == "closing" and not self.said_goodbye:
            pass

    def _extract(self, t: str) -> None:
        if "cookie" in t:
            if re.search(r"\bhalf(?: a)? dozen\b", t):
                self.cookie_qty = 6
            elif (m := re.search(r"\b(two|three|2|3) dozen\b", t)):
                self.cookie_qty = 12 * {"two": 2, "three": 3, "2": 2, "3": 3}[m.group(1)]
            elif re.search(r"\bdozen\b|\btwelve\b|\b12\b", t):
                self.cookie_qty = 12
            if re.search(r"chocolate[- ]chip", t):
                self.cookie_kind = "chocolate chip"
        if "sourdough" in t:
            self.sourdough_requested = True
        if self.offered_sub and self.bread is None:
            if NO.search(t):
                self.bread = "none"
            elif YES.search(t):
                self.bread = "country white"
        if "saturday" in t:
            self.saturday_mentioned = True
        if self.saturday_mentioned or self.expect == "pickup_time":
            m = re.search(r"\b(nine|ten|eleven|twelve|noon|9|10|11|12)\b(?:[: ](thirty|30|fifteen|15))?", t)
            if m and ("saturday" in t or self.expect == "pickup_time"):
                hour = HOUR[m.group(1)]
                self.pickup = f"Saturday {hour}:{'30' if m.group(2) in ('thirty', '30') else '00'}"
        if re.search(r"\balex kim\b", t):
            self.name = "Alex Kim"
        elif self.expect == "name" and re.search(r"\balex\b", t):
            self.name = "Alex"
        if self.expect == "pickup_or_delivery" and "pickup" in t.replace("pick up", "pickup"):
            self.answered_pickup = True

    def _respond(self, t: str):
        prefix = []
        if self.expect == "pickup_or_delivery":
            self.expect = ""
            prefix = ["Pickup, perfect. Sorry, go ahead."] if self.answered_pickup else \
                ["Okay, we only do pickup, just so you know."]
        if self.stage == "greeting":
            self.stage = "ordering"
        if self.stage == "ordering":
            self.ordering_turns += 1
            if re.search(r"(what time|when) do you (open|close)|your hours", t):
                return prefix + ["We're open seven to three on Saturday."]
            if self.sourdough_requested and not self.offered_sub:
                self.offered_sub = True
                self.expect = "sub"
                got = "Okay, a dozen chocolate chip cookies, got it. " if self.cookie_qty and self.cookie_kind else ""
                return prefix + [got + "Oh, I'm sorry, we're all out of sourdough for the weekend. We do have "
                                       "a country white loaf, it's seven fifty. Would that work instead?"]
            if not (self.cookie_qty and self.cookie_kind):
                self.expect = "items"
                if prefix:
                    return prefix
                return ["Sure! What can I get for you?"]
            if self.offered_sub and self.bread is None:
                if self.sub_reasks == 0:
                    self.sub_reasks += 1
                    return prefix + ["Sorry, did you want the country white loaf instead?"]
                self.bread = "none"
            if not self.pickup:
                self.expect = "pickup_time"
                return prefix + ["And when would you like to pick that up?"]
            if not self.name:
                self.expect = "name"
                return prefix + ["What name should I put that under?"]
            if not self.did_hold:
                return "HOLD"
        if self.stage == "readback":
            if DONE.search(t) or self.agent_said_bye or re.search(r"\b(thank|thanks|perfect|great)\b", t):
                self.stage = "closing"
                self.said_goodbye = True
                asyncio.ensure_future(self._hang_up_if_agent_does_not())
                return ["Great! We'll see you Saturday, " + (self.name or "hon").split()[0] + ". Bye now!"]
            if re.search(r"how much|what('s| is) the total", t):
                return ["It's " + self._total_words() + " altogether."]
            return ["Mm-hm. Anything else I can get you?"]
        if self.stage == "closing":
            return None
        return prefix or None

    def _total_words(self) -> str:
        return "twenty-five fifty" if self.bread == "country white" else "eighteen dollars"

    async def _hold_then_readback(self) -> None:
        self.did_hold = True
        await self.phone.sleep(0.3)
        await self.say("Perfect. Let me just double-check we'll have enough chocolate chip for Saturday. "
                       "Hang on one sec.")
        self.on_hold = True
        await self.phone.sleep(self.HOLD_S)
        self.on_hold = False
        while not self.q.empty():          # anything said during the hold was noted, not answered
            kind, text, _ = self.q.get_nowait()
            if kind == "heard" and text:
                self.hold_lines.append(text)
        bread = " and one country white loaf" if self.bread == "country white" else ""
        self.stage = "readback"
        self.readback_done = True
        who = self.name or "you"
        await self.say("Okay, sorry about that, we're all set. So that's a dozen chocolate chip cookies",
                       550, f"{bread}, for pickup {self.pickup_words()}, under {who}. Your total comes to "
                            f"{self._total_words()}, and you can pay when you pick up. Anything else?")

    def pickup_words(self) -> str:
        if not self.pickup:
            return "Saturday"
        hour = int(self.pickup.split()[1].split(":")[0])
        words = {9: "nine", 10: "ten", 11: "eleven", 12: "noon"}[hour]
        half = self.pickup.endswith(":30")
        return f"Saturday at {words}{' thirty' if half and hour != 12 else ''}"

    async def _hang_up_if_agent_does_not(self) -> None:
        await self.phone.sleep(12)
        if not self.phone.ended.is_set():
            self.log("   bakery: (agent never hung up; hanging up)")
            self.phone.hang_up()

    def verdict(self) -> dict:
        checks = {
            "pressed 2 at the phone menu": "2" in self.pressed,
            "disclosed it is an AI in its opening line": bool(AI.search(self.first_agent_line.lower())),
            "ordered a dozen chocolate chip cookies": self.cookie_qty == 12 and self.cookie_kind == "chocolate chip",
            "asked for sourdough, took country white instead": self.sourdough_requested and self.bread == "country white",
            "pickup Saturday between 10 and noon": bool(self.pickup) and 10 <= int(self.pickup.split()[1][:2].rstrip(":")) <= 12,
            "gave the name Alex Kim": self.name == "Alex Kim",
            "handled being interrupted (answered pickup)": self.did_interrupt and self.answered_pickup,
            "stayed quiet on hold": len(self.hold_lines) == 0 or (len(self.hold_lines) == 1 and len(self.hold_lines[0]) <= 60),
            "heard the read-back and closed politely": self.readback_done and self.said_goodbye,
            "agent hung up after goodbye": self.agent_hung_up,
        }
        return {"checks": checks, "passed": sum(checks.values()), "total": len(checks),
                "pickup": self.pickup, "bread": self.bread, "hold_lines": self.hold_lines,
                "first_agent_line": self.first_agent_line, "end_reason": self.end_reason}
