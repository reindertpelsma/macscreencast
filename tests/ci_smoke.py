#!/usr/bin/env python3
"""ci_smoke.py — non-interactive CI smoke test for mac-vnc-stream.

Tests:
  1. SCK video: connect WebSocket, send caps, assert >= MIN_FRAMES in 8s
  2. Audio:     connect audio WebSocket, assert >= MIN_AUDIO_PKTS in 5s

Exit 0 = all pass. Exit 1 = failure with details.
"""
import asyncio, json, struct, sys, time

HOST    = "127.0.0.1"
PORT    = int(sys.argv[1]) if len(sys.argv) > 1 else 6081
TOKEN   = sys.argv[2] if len(sys.argv) > 2 else ""

WS_URL  = f"ws://{HOST}:{PORT}/" + (f"?token={TOKEN}" if TOKEN else "")
AUD_URL = f"ws://{HOST}:{PORT}/audio" + (f"?token={TOKEN}" if TOKEN else "")

MIN_FRAMES          = 120  # in 8s — 15fps minimum; GitHub VM compositor runs ~21fps
MIN_AUDIO_PKTS      = 10   # total packets in 5s (includes DTX silence)
MIN_REAL_AUDIO_PKTS = 30   # non-DTX packets — proves real audio is captured, not just silence

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

failures = []

async def test_video():
    try:
        import websockets
    except ImportError:
        print(f"  video: SKIP (websockets not installed)")
        return

    print(f"  video: connecting {WS_URL} ...")
    try:
        async with websockets.connect(WS_URL, open_timeout=10) as ws:
            await ws.send(json.dumps({
                "t": "caps",
                "webcodecs": True,
                "codecs": ["avc1.42E01F"],
                "explicit": False,
                "w": 1280, "h": 720,
            }))
            frames = 0
            keyframes = 0
            start = time.monotonic()
            _last_lag_sent = 0.0
            while time.monotonic() - start < 8.0:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                except asyncio.TimeoutError:
                    print("    timeout waiting for frame")
                    break
                if not isinstance(msg, (bytes, bytearray)) or len(msg) < 18:
                    continue
                # Header: >IQBBI = seq(I) ts(Q) type(B) flags(B) codec(I)
                _, ts_ms, ftype, _, _ = struct.unpack_from(">IQBBI", msg)
                frames += 1
                if ftype == 1:  # keyframe
                    keyframes += 1
                # Send lag reports so the server's feedback-gated ramp-up is exercised.
                # Also prevents the proactive-backoff path from firing (which requires a
                # real lag report to have been received before it arms itself).
                now_ms = int(time.monotonic() * 1000)
                if now_ms / 1000 - _last_lag_sent > 0.1:  # ~10/s, matching browser rate
                    age_ms = now_ms - ts_ms
                    await ws.send(json.dumps({"t": "lag", "age_ms": max(1, age_ms)}))
                    _last_lag_sent = now_ms / 1000

            elapsed = time.monotonic() - start
            fps = frames / max(elapsed, 0.001)
            if frames >= MIN_FRAMES:
                print(f"  video: {PASS}  {frames} frames ({keyframes} key) in {elapsed:.1f}s = {fps:.1f} fps")
            else:
                msg = f"video: only {frames} frames in {elapsed:.1f}s (need {MIN_FRAMES})"
                print(f"  video: {FAIL}  {msg}")
                failures.append(msg)
    except Exception as e:
        msg = f"video: exception: {e}"
        print(f"  video: {FAIL}  {msg}")
        failures.append(msg)

async def test_audio():
    try:
        import websockets
    except ImportError:
        print(f"  audio: SKIP (websockets not installed)")
        return

    print(f"  audio: connecting {AUD_URL} ...")
    try:
        async with websockets.connect(AUD_URL, open_timeout=10) as ws:
            pkts = 0
            dtx  = 0   # DTX silence packets (< 4 bytes payload)
            start = time.monotonic()
            while time.monotonic() - start < 5.0:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                except asyncio.TimeoutError:
                    print("    timeout waiting for audio packet")
                    break
                if not isinstance(msg, (bytes, bytearray)) or len(msg) < 8:
                    continue
                opus = msg[8:]
                pkts += 1
                if len(opus) < 4:
                    dtx += 1

            elapsed = time.monotonic() - start
            real_pkts = pkts - dtx
            if pkts >= MIN_AUDIO_PKTS and real_pkts >= MIN_REAL_AUDIO_PKTS:
                print(f"  audio: {PASS}  {pkts} packets ({real_pkts} real, {dtx} DTX) in {elapsed:.1f}s")
            else:
                issues = []
                if pkts < MIN_AUDIO_PKTS:
                    issues.append(f"only {pkts} total packets (need {MIN_AUDIO_PKTS})")
                if real_pkts < MIN_REAL_AUDIO_PKTS:
                    issues.append(f"only {real_pkts} real (non-DTX) packets (need {MIN_REAL_AUDIO_PKTS})")
                msg = "audio: " + "; ".join(issues)
                print(f"  audio: {FAIL}  {msg}")
                failures.append(msg)
    except Exception as e:
        msg = f"audio: exception: {e}"
        print(f"  audio: {FAIL}  {msg}")
        failures.append(msg)

async def main():
    print(f"mac-vnc-stream CI smoke test — {HOST}:{PORT}")
    print()
    await test_video()
    await test_audio()
    print()
    if failures:
        print(f"RESULT: {FAIL} — {len(failures)} failure(s):")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)
    else:
        print(f"RESULT: {PASS} — all checks passed")

asyncio.run(main())
