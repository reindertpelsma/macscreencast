#!/usr/bin/env python3
"""tcp_throttle.py — rate-limiting TCP proxy for local congestion testing.

Sits between the test client and the server. The server writes into the
proxy's buffer freely (no TCP backpressure — the server→proxy hop is
localhost and drains instantly). The proxy then drains toward the client
at the requested rate, accumulating data in its own buffer. This faithfully
models a congested downstream hop: large internal buffer, no backpressure
reaching the sender.

Upload (client→server) is unthrottled — lag reports flow freely.

Usage:
  # Terminal 1 — start proxy
  python3 tests/tcp_throttle.py <listen_port> <upstream_port> <kbps>

  # Terminal 2 — run test through proxy
  python3 tests/test_2mbps.py <listen_port> [token] [min_fps] [min_mbps]

Example (2Mbps throttle, server on 6081, proxy on 6082):
  python3 tests/tcp_throttle.py 6082 6081 2000
  python3 tests/test_2mbps.py  6082 guacweb 20 1.7
"""
import asyncio, sys, time

LISTEN_PORT   = int(sys.argv[1]) if len(sys.argv) > 1 else 6082
UPSTREAM_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 6081
RATE_KBPS     = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
RATE          = RATE_KBPS * 1000 / 8   # bytes/sec


async def _throttled(src: asyncio.StreamReader, dst: asyncio.StreamWriter, tag: str):
    """Forward src→dst at RATE bytes/sec using a token bucket."""
    tokens = 0.0
    last_t = time.monotonic()
    total  = 0
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            now     = time.monotonic()
            tokens  = min(tokens + (now - last_t) * RATE, RATE * 2)  # cap at 2s burst
            last_t  = now
            deficit = len(chunk) - tokens
            if deficit > 0:
                await asyncio.sleep(deficit / RATE)
                tokens = 0.0
            else:
                tokens -= len(chunk)
            dst.write(chunk)
            await dst.drain()
            total += len(chunk)
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    print(f"  [{tag}] done  total={total//1024}KB")
    dst.close()


async def _passthrough(src: asyncio.StreamReader, dst: asyncio.StreamWriter, tag: str):
    """Forward src→dst at full speed (upload / control direction)."""
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    dst.close()


async def _handle(client_r, client_w):
    peer = client_w.get_extra_info("peername")
    print(f"  connect from {peer}")
    server_r, server_w = await asyncio.open_connection("127.0.0.1", UPSTREAM_PORT)
    await asyncio.gather(
        _throttled(server_r, client_w, "server→client"),
        _passthrough(client_r, server_w, "client→server"),
        return_exceptions=True,
    )


async def main():
    srv = await asyncio.start_server(_handle, "127.0.0.1", LISTEN_PORT)
    print(f"tcp_throttle: 127.0.0.1:{LISTEN_PORT} → 127.0.0.1:{UPSTREAM_PORT} "
          f"@ {RATE_KBPS}kbps downstream")
    async with srv:
        await srv.serve_forever()


asyncio.run(main())
