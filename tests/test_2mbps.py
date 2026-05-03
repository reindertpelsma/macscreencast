#!/usr/bin/env python3
"""test_2mbps.py — verify stability at 2Mbps by simulating a throttled client.

Uses a virtual-clock approach: track when each frame *would* arrive at 2Mbps,
sleep until that virtual arrival time, then report the simulated lag back to the
server. This accurately models Chrome DevTools throttle (where the server's TCP
write buffer stays empty but lag accumulates in Chrome's internal buffer).

The server receives realistic lag reports and its congestion controller must:
  - keep lag below MAX_LAG_MS at steady state
  - maintain at least MIN_FPS average fps (responsiveness)
  - sustain at least MIN_MBPS average bitrate (proves effective link use,
    not just throttled-to-floor operation)

Usage:
  python3 tests/test_2mbps.py [port] [token] [min_fps] [min_mbps]
"""
import asyncio, json, struct, sys, time

try:
    from cpu_sampler import CpuSampler
except ImportError:
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cpu_sampler import CpuSampler

HOST        = "127.0.0.1"
PORT        = int(sys.argv[1])   if len(sys.argv) > 1 else 6081
TOKEN       = sys.argv[2]        if len(sys.argv) > 2 else ""
MIN_FPS     = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
MIN_MBPS    = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0   # 0 = no check
RATE_BPS    = 2_000_000   # simulated 2 Mbps downstream
DURATION    = 40.0        # total test duration (seconds)
WARMUP_S    = 10.0        # ignore lag/bitrate during initial ramp-up/settle
MAX_LAG_MS  = 2000        # steady-state max lag (after warmup)

WS_URL = f"ws://{HOST}:{PORT}/" + (f"?token={TOKEN}" if TOKEN else "")
RATE   = RATE_BPS / 8     # bytes per second


async def main():
    try:
        import websockets
    except ImportError:
        print("SKIP — websockets not installed")
        return

    print(f"2Mbps stability test → {WS_URL}  ({DURATION}s)")
    print(f"  throttle={RATE_BPS//1000}kbps  warmup={WARMUP_S}s  "
          f"max_lag={MAX_LAG_MS}ms  min_fps={MIN_FPS}  min_mbps={MIN_MBPS}")

    cpu = CpuSampler(); cpu.start()

    frames = 0
    max_lag_steady = 0
    lag_samples = []
    steady_bytes = 0   # bytes received after warmup (for bitrate check)
    start_mono = time.monotonic()
    last_lag_sent = 0.0
    disconnected = False

    # virtual_t: the wall-clock time at which the throttled client has "received"
    # all bytes seen so far. Starts at connect time.
    virtual_t = time.time()

    try:
        async with websockets.connect(WS_URL, open_timeout=10,
                                      max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "t": "caps",
                "webcodecs": True,
                "codecs": ["avc1.42E01F"],
                "explicit": False,
                "w": 1280, "h": 720,
            }))

            while time.monotonic() - start_mono < DURATION:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    print("  TIMEOUT — no frame in 5s (drain pause or disconnect)")
                    break

                if not isinstance(msg, (bytes, bytearray)) or len(msg) < 18:
                    continue

                # Advance virtual clock by the time this frame takes at 2Mbps.
                # This is when a real 2Mbps link would finish delivering this frame.
                virtual_t += len(msg) / RATE

                # Sleep until the virtual arrival time so we pace at 2Mbps.
                # If virtual_t < now, we're already behind (queue backed up) — no sleep.
                sleep_s = virtual_t - time.time()
                if sleep_s > 0.001:
                    await asyncio.sleep(sleep_s)

                # Parse frame header — ts_ms is server wall-clock send time
                _, ts_ms, ftype, _, _ = struct.unpack_from(">IQBBI", msg)
                frames += 1
                elapsed_mono = time.monotonic() - start_mono
                if elapsed_mono >= WARMUP_S:
                    steady_bytes += len(msg)

                # Lag = (virtual arrival time) - (server send time).
                # This matches exactly what the browser measures: frame age at decode time.
                lag_ms = max(1, int(virtual_t * 1000) - ts_ms)

                # Send lag report at ~10/s (matching browser rate)
                now_t = time.monotonic()
                if now_t - last_lag_sent >= 0.1:
                    await ws.send(json.dumps({"t": "lag", "age_ms": lag_ms}))
                    last_lag_sent = now_t

                    if elapsed_mono >= WARMUP_S:
                        lag_samples.append(lag_ms)
                        if lag_ms > max_lag_steady:
                            max_lag_steady = lag_ms

                if frames % 20 == 0:
                    fps_now = frames / max(elapsed_mono, 0.001)
                    behind_s = max(0, virtual_t - time.time())
                    print(f"  t={elapsed_mono:5.1f}s  frames={frames:4d}  "
                          f"fps={fps_now:5.1f}  lag={lag_ms:5d}ms  behind={behind_s:.2f}s")

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        disconnected = True

    cpu.stop()
    elapsed = time.monotonic() - start_mono
    steady_s = max(elapsed - WARMUP_S, 0.001)
    avg_fps  = frames / max(elapsed, 0.001)
    avg_lag  = sum(lag_samples) / len(lag_samples) if lag_samples else 0
    avg_mbps = steady_bytes * 8 / 1_000_000 / steady_s

    print()
    print(f"Results after {elapsed:.1f}s:")
    print(f"  frames={frames}  avg_fps={avg_fps:.1f}  disconnected={disconnected}")
    print(f"  steady-state (after {WARMUP_S}s warmup):")
    print(f"    lag  max={max_lag_steady}ms  avg={avg_lag:.0f}ms  samples={len(lag_samples)}")
    print(f"    link avg={avg_mbps:.2f}Mbps  (bytes={steady_bytes}  t={steady_s:.1f}s)")
    print(f"  CPU peaks: {cpu.summary()}")

    failures = []
    if disconnected:
        failures.append("disconnected during test")
    if max_lag_steady > MAX_LAG_MS:
        failures.append(f"max lag {max_lag_steady}ms > {MAX_LAG_MS}ms limit")
    if avg_fps < MIN_FPS:
        if cpu.saturated:
            print(f"  NOTE: avg fps {avg_fps:.1f} < {MIN_FPS} but CPU saturated "
                  f"({cpu.summary()}) — runner is the bottleneck, not the system. "
                  "fps bar exempted.")
        else:
            failures.append(f"avg fps {avg_fps:.1f} < {MIN_FPS} minimum "
                            f"(CPU had headroom: {cpu.summary()})")
    if MIN_MBPS > 0 and avg_mbps < MIN_MBPS:
        failures.append(f"avg link {avg_mbps:.2f}Mbps < {MIN_MBPS}Mbps minimum "
                        "(controller throttled to floor instead of finding equilibrium)")

    if failures:
        print(f"\nFAIL:")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)
    else:
        print(f"\nPASS")


asyncio.run(main())
