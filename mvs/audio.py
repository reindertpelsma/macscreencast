import asyncio
import logging
import queue
import struct
import threading

import numpy as np

from mvs.codec import _AV_OK

log = logging.getLogger("macvnc")

# ---------------------------------------------------------------------------
# Audio globals — SCK + Opus encoder + WebSocket fan-out
# ---------------------------------------------------------------------------
_audio_clients: int = 0              # count of active audio WS subscribers
_audio_subs: dict = {}               # qid → (asyncio.Queue, event_loop)
_audio_subs_lock = threading.Lock()
_audio_raw_q: queue.Queue = queue.Queue(maxsize=500)   # raw PCM chunks from SCK callback
_audio_encoder_started: bool = False  # encoder thread started lazily on first subscriber


def _put_drop_oldest(aq, msg):
    """Put `msg` into asyncio.Queue `aq`, evicting the oldest item if full.
    Runs in the event loop via call_soon_threadsafe."""
    while aq.full():
        try:
            aq.get_nowait()
        except Exception:
            break
    try:
        aq.put_nowait(msg)
    except Exception:
        pass


def _audio_encoder_thread():
    """Drain _audio_raw_q, encode PCM→Opus, fan-out to audio subscribers.
    Started lazily when the first audio WS client connects.
    Runs for the life of the server; exits only if Opus init fails."""
    global _audio_encoder_started
    if not _AV_OK:
        log.warning("Audio encoder: PyAV not available — audio disabled")
        _audio_encoder_started = False
        return
    try:
        import av as _av_audio
        codec_ctx = _av_audio.CodecContext.create('libopus', 'w')
        codec_ctx.sample_rate = 48000
        # libopus supports 'flt' (interleaved float32) and 's16' — NOT 'fltp' (planar)
        codec_ctx.format = 'flt'
        codec_ctx.bit_rate = 128000
        # PyAV 13+ uses .layout; older PyAV used .channel_layout / .channels
        try:
            codec_ctx.layout = 'stereo'
        except AttributeError:
            try:
                codec_ctx.channel_layout = 'stereo'
            except AttributeError:
                codec_ctx.channels = 2
        codec_ctx.open()
        log.info("Audio encoder: Opus ready (48 kHz stereo 64 kbps)")
    except Exception as e:
        log.warning("Audio encoder: Opus init failed (%s) — audio disabled", e)
        _audio_encoder_started = False
        return

    FRAME_SAMPLES = 960          # 20 ms at 48 kHz (one Opus frame)
    CHANNELS      = 2
    FRAME_FLOATS  = FRAME_SAMPLES * CHANNELS  # interleaved float32 count
    pcm_buf = np.zeros(0, dtype=np.float32)   # accumulation buffer
    pts     = 0                               # monotonic sample counter

    while True:
        # Block until a PCM chunk arrives (or 5s timeout to stay responsive).
        try:
            raw = _audio_raw_q.get(timeout=5.0)
        except queue.Empty:
            continue

        if _audio_clients == 0:
            # Drain queue without encoding while nobody is listening.
            while not _audio_raw_q.empty():
                try: _audio_raw_q.get_nowait()
                except queue.Empty: break
            pcm_buf = np.zeros(0, dtype=np.float32)
            pts = 0
            continue

        # Append new samples. SCK delivers float32 non-interleaved (planar): [L×N, R×N].
        # libopus 'flt' expects interleaved [L0,R0,L1,R1,...], so we must interleave.
        try:
            planar = np.frombuffer(raw, dtype=np.float32)
            n = len(planar) // 2
            interleaved_in = np.empty(len(planar), dtype=np.float32)
            interleaved_in[0::2] = planar[:n]   # left channel
            interleaved_in[1::2] = planar[n:]   # right channel
            pcm_buf = np.concatenate([pcm_buf, interleaved_in])
        except Exception:
            continue

        # Encode as many complete 20 ms frames as are available.
        while len(pcm_buf) >= FRAME_FLOATS:
            chunk   = pcm_buf[:FRAME_FLOATS]
            pcm_buf = pcm_buf[FRAME_FLOATS:]
            try:
                # Clamp and keep interleaved: libopus uses 'flt' (interleaved float32).
                interleaved = np.ascontiguousarray(np.clip(chunk, -1.0, 1.0))

                frame = _av_audio.AudioFrame.from_ndarray(
                    interleaved.reshape(1, -1), format='flt', layout='stereo')
                frame.sample_rate = 48000
                frame.pts         = pts

                for pkt in codec_ctx.encode(frame):
                    opus_bytes = bytes(pkt)
                    ts_us = pts * 1_000_000 // 48000
                    # Wire format: 8-byte uint64 big-endian timestamp (µs) + Opus payload.
                    msg = struct.pack('>Q', ts_us) + opus_bytes
                    with _audio_subs_lock:
                        subs = list(_audio_subs.values())
                    for aq, lp in subs:
                        # Drop-oldest semantics: if a client is briefly slow,
                        # the freshest audio wins. Old approach (silently fail
                        # on full queue) held stale audio and dropped fresh,
                        # so audio could lag behind video by the queue's depth.
                        try:
                            lp.call_soon_threadsafe(_put_drop_oldest, aq, msg)
                        except Exception:
                            pass  # subscriber loop gone

                pts += FRAME_SAMPLES
            except Exception as e:
                log.debug("Audio encoder frame: %s", e)


async def audio_session(ws):
    """Stream Opus frames to one audio WebSocket client.
    Runs independently from the video session — stays alive when tab is hidden."""
    global _audio_clients, _audio_encoder_started
    import uuid as _uuid

    qid = _uuid.uuid4().hex
    # 25 frames × 20 ms = 500 ms ceiling. Combined with drop-oldest
    # semantics in the fan-out, a slow client drops to freshness rather
    # than playing 4 seconds of stale audio out of sync with video.
    aq  = asyncio.Queue(maxsize=25)
    lp  = asyncio.get_event_loop()

    with _audio_subs_lock:
        _audio_subs[qid] = (aq, lp)
    _audio_clients += 1
    log.info("Audio client connected (total %d)", _audio_clients)

    # Start encoder thread on first subscriber (lazy init).
    if not _audio_encoder_started:
        _audio_encoder_started = True
        threading.Thread(target=_audio_encoder_thread, daemon=True, name="audio-enc").start()

    try:
        while True:
            try:
                msg = await asyncio.wait_for(aq.get(), timeout=10.0)
                await ws.send(msg)
            except asyncio.TimeoutError:
                # Send a WS ping so the connection doesn't silently drop.
                await ws.ping()
    except Exception:
        pass
    finally:
        _audio_clients -= 1
        with _audio_subs_lock:
            _audio_subs.pop(qid, None)
        log.info("Audio client disconnected (remaining %d)", _audio_clients)
