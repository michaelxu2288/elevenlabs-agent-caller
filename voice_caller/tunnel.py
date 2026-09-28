"""Public HTTPS/WSS URL for Twilio to reach this machine.

cloudflared-quick  starts `cloudflared tunnel --url http://127.0.0.1:<port>` for the
                   length of one call. No Cloudflare account; the hostname is random
                   each time, which is fine because every call passes its URLs to
                   Twilio explicitly.
static             you run a tunnel yourself (ngrok static domain, named Cloudflare
                   tunnel) and set [tunnel] public_url.
none               local only (the dry run).
Whatever the mode, the URL must stay up for the whole call: the media stream lives on it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import socket
import time
from collections import deque
from pathlib import Path

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

log = logging.getLogger("tunnel")
# `api.trycloudflare.com` shows up in cloudflared's error messages; it is never our URL
QUICK_URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")


class TunnelError(RuntimeError):
    pass


class PublicDnsResolver(AbstractResolver):
    """Looks the tunnel host up in public DNS (Cloudflare DNS over HTTPS), as Twilio will. A local
    resolver asked before a new quick-tunnel host exists caches the miss for up to a minute, and
    public answers flip between found and missing for a few seconds, so the first answer is kept."""

    known: dict[str, list[str]] = {}

    def __init__(self, doh_url: str = "https://1.1.1.1/dns-query"):
        self.doh_url = doh_url
        self.system = ThreadedResolver()

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        ips = self.known.get(host)
        if not ips:
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.get(self.doh_url, params={"name": host, "type": "A"},
                                        headers={"accept": "application/dns-json"},
                                        timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        answer = (await resp.json(content_type=None)).get("Answer", [])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return await self.system.resolve(host, port, family)
            ips = [a["data"] for a in answer if a.get("type") == 1]
            if not ips:
                raise OSError(f"{host} is not in public DNS yet")
            self.known[host] = ips
        return [ResolveResult(hostname=host, host=ip, port=port, family=socket.AF_INET, proto=0,
                              flags=socket.AI_NUMERICHOST) for ip in ips]

    async def close(self) -> None:
        await self.system.close()


def public_http() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicDnsResolver()))


async def wait_until_reachable(base_url: str, timeout_s: float = 45.0) -> float:
    """Poll <base>/health through the public URL (new quick-tunnel DNS can take a few seconds)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    t0 = time.monotonic()
    async with public_http() as http:
        while time.monotonic() < deadline:
            try:
                async with http.get(base_url.rstrip("/") + "/health",
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    body = await resp.text()
                    if resp.status == 200 and "voice-caller" in body:
                        return time.monotonic() - t0
                    last = f"HTTP {resp.status}"
            except Exception as e:  # noqa: BLE001 - DNS not there yet, TLS, etc.
                last = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1.0)
    raise TunnelError(f"{base_url} did not reach this server within {timeout_s:.0f}s ({last})")


class Tunnel:
    public_url: str = ""

    async def start(self) -> str:
        raise NotImplementedError

    async def stop(self) -> None:
        pass

    @property
    def ws_base(self) -> str:
        return re.sub(r"^http", "ws", self.public_url.rstrip("/"))


class NoTunnel(Tunnel):
    def __init__(self, local_url: str):
        self.public_url = local_url

    async def start(self) -> str:
        return self.public_url


class StaticTunnel(Tunnel):
    def __init__(self, public_url: str):
        if not public_url.startswith("https://"):
            raise TunnelError("[tunnel] public_url must be an https:// URL when mode = static")
        self.public_url = public_url.rstrip("/")

    async def start(self) -> str:
        await wait_until_reachable(self.public_url, 15)
        return self.public_url


class QuickTunnel(Tunnel):
    def __init__(self, local_port: int, cloudflared_bin: str = "cloudflared", timeout_s: float = 45.0):
        self.local_port = local_port
        self.bin = cloudflared_bin
        self.timeout_s = timeout_s
        self.proc: asyncio.subprocess.Process | None = None
        self.log_tail: deque[str] = deque(maxlen=40)
        self._drain: asyncio.Task | None = None

    async def start(self) -> str:
        if not shutil.which(self.bin):
            raise TunnelError(f"{self.bin} not found; install cloudflared or use [tunnel] mode = static")
        cfg_file = Path.home() / ".cloudflared" / "config.yml"
        if cfg_file.exists() or cfg_file.with_suffix(".yaml").exists():
            raise TunnelError(f"{cfg_file.parent}/config.y(a)ml exists; Quick Tunnels refuse to run with it. "
                              "Move it away or use [tunnel] mode = static")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            metrics_port = s.getsockname()[1]
        # --protocol auto: quick tunnels otherwise force QUIC (UDP 7844) with no http2 fallback
        self.proc = await asyncio.create_subprocess_exec(
            self.bin, "tunnel", "--no-autoupdate", "--protocol", "auto",
            "--metrics", f"127.0.0.1:{metrics_port}", "--url", f"http://127.0.0.1:{self.local_port}",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        self._drain = asyncio.create_task(self._drain_stderr())
        url = await self._url_from_metrics(metrics_port)
        if not url:
            await self.stop()
            hint = ""
            if any("api.trycloudflare.com" in l and ("deadline" in l or "timeout" in l.lower())
                   for l in self.log_tail):
                hint = ("\n  -> this network cannot reach api.trycloudflare.com; "
                        "see README 'Tunnel'")
            raise TunnelError("cloudflared did not bring up a quick tunnel:\n  "
                              + "\n  ".join(list(self.log_tail)[-4:]) + hint)
        self.public_url = url
        took = await wait_until_reachable(url, self.timeout_s)
        log.info("quick tunnel up: %s (reachable after %.1fs)", url, took)
        return url

    async def _url_from_metrics(self, port: int) -> str:
        """cloudflared's metrics server: /ready turns 200 once connected, /quicktunnel names the host."""
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self.timeout_s
        async with aiohttp.ClientSession() as http:
            while time.monotonic() < deadline and self.proc.returncode is None:
                try:
                    async with http.get(base + "/ready", timeout=aiohttp.ClientTimeout(total=2)) as r:
                        ready = r.status == 200
                    if ready:
                        async with http.get(base + "/quicktunnel", timeout=aiohttp.ClientTimeout(total=2)) as r:
                            host = (await r.json(content_type=None)).get("hostname", "")
                        if host:
                            return f"https://{host}"
                except Exception:  # noqa: BLE001 - metrics server not up yet
                    pass
                await asyncio.sleep(0.5)
        for line in self.log_tail:          # fall back to the banner in the log
            m = QUICK_URL_RE.search(line)
            if m:
                return m.group(0)
        return ""

    async def _drain_stderr(self) -> None:
        # keep reading so the pipe never fills and blocks cloudflared mid-call
        while self.proc and self.proc.stderr:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self.log_tail.append(line.decode("utf-8", "replace").rstrip())

    async def stop(self) -> None:
        if self._drain:
            self._drain.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()


def make_tunnel(cfg, local_port: int) -> Tunnel:
    mode = cfg.tunnel.mode
    if mode == "static":
        return StaticTunnel(cfg.tunnel.public_url)
    if mode == "none":
        return NoTunnel(f"http://127.0.0.1:{local_port}")
    return QuickTunnel(local_port, cfg.tunnel.cloudflared_bin, cfg.tunnel.start_timeout_s)
