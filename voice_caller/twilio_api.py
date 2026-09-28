"""Minimal async Twilio REST client (plain HTTP, no SDK) and webhook signature checks."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from xml.sax.saxutils import quoteattr

import aiohttp

log = logging.getLogger("twilio")

# Twilio error codes worth explaining in plain words when a call fails to start
ERROR_HINTS = {
    20003: "authentication failed: check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN",
    21210: "the From number is not a Twilio number on this account (TWILIO_FROM_NUMBER)",
    21211: "the To number is not a valid phone number",
    21212: "the From number is invalid (TWILIO_FROM_NUMBER)",
    21215: "calling this destination is blocked by Voice Geographic Permissions (Console > Voice > Settings > Geo permissions)",
    21217: "the To number is not a valid phone number",
    21219: "trial accounts can only call Verified Caller IDs: verify the number or upgrade the account",
    21216: "this number is blocked or not reachable from Twilio",
}


class TwilioError(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str):
        hint = ERROR_HINTS.get(code or 0)
        super().__init__(f"Twilio HTTP {status} code {code}: {message}" + (f" ({hint})" if hint else ""))
        self.status, self.code, self.twilio_message = status, code, message


def compute_signature(auth_token: str, url: str, params: dict | None) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(auth_token, url + concat(sorted key+value)))."""
    s = url
    for key in sorted(params or {}):
        values = params[key]
        if not isinstance(values, (list, tuple)):
            values = [values]
        for v in sorted(values):
            s += key + v
    digest = hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_signature(auth_token: str, url: str, params: dict | None, signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


def stream_twiml(ws_url: str, parameters: dict[str, str]) -> str:
    """<Connect><Stream>: a bidirectional media stream that holds the call until we hang up."""
    params = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in parameters.items())
    return f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"


class TwilioClient:
    def __init__(self, http: aiohttp.ClientSession, account_sid: str, auth_token: str,
                 api_base: str = "https://api.twilio.com"):
        self._http = http
        self._sid = account_sid
        self._auth = aiohttp.BasicAuth(account_sid, auth_token)
        self._base = api_base.rstrip("/") + f"/2010-04-01/Accounts/{account_sid}"

    async def _request(self, method: str, path: str, data=None) -> dict:
        url = self._base + path
        async with self._http.request(method, url, data=data, auth=self._auth,
                                      timeout=aiohttp.ClientTimeout(total=20)) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {"message": (await resp.text())[:300]}
            if resp.status >= 400:
                raise TwilioError(resp.status, body.get("code"), body.get("message", ""))
            return body

    async def create_call(self, *, to: str, from_: str, twiml: str, status_callback: str,
                          ring_timeout_s: int, time_limit_s: int, send_digits: str = "") -> dict:
        data = [
            ("To", to), ("From", from_), ("Twiml", twiml),
            ("StatusCallback", status_callback), ("StatusCallbackMethod", "POST"),
            ("Timeout", str(ring_timeout_s)), ("TimeLimit", str(time_limit_s)),
        ]
        if send_digits:
            data.append(("SendDigits", send_digits))
        for ev in ("initiated", "ringing", "answered", "completed"):
            data.append(("StatusCallbackEvent", ev))
        return await self._request("POST", "/Calls.json", data=data)

    async def hangup(self, call_sid: str) -> dict:
        return await self._request("POST", f"/Calls/{call_sid}.json", data={"Status": "completed"})

    async def fetch_call(self, call_sid: str) -> dict:
        return await self._request("GET", f"/Calls/{call_sid}.json")

    async def fetch_account(self) -> dict:
        return await self._request("GET", ".json")

    async def owns_number(self, e164: str) -> bool:
        query = "?PhoneNumber=" + e164.replace("+", "%2B")
        body = await self._request("GET", "/IncomingPhoneNumbers.json" + query)
        if body.get("incoming_phone_numbers"):
            return True
        body = await self._request("GET", "/OutgoingCallerIds.json" + query)
        return bool(body.get("outgoing_caller_ids"))
