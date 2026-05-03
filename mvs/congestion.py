import logging
import threading
import time

log = logging.getLogger("macvnc")


# ---------------------------------------------------------------------------
# AdaptiveController — per-client fps + bitrate management
# ---------------------------------------------------------------------------
class AdaptiveController:

    def __init__(self, cfg):
        self.fps = float(cfg.max_fps)
        self.max_fps = float(cfg.max_fps)
        self.bitrate = 1_000_000     # start at 1Mbps — ramps up via feedback gating, no initial burst
        self.jpeg_quality = 85
        self.client_w = 1920
        self.client_h = 1080
        self.cap_h   = 0    # 0 = auto (use canvas physical size); >0 = explicit height cap
        self.fps_cap = 0    # 0 = use max_fps; >0 = explicit fps ceiling
        self.canvas_phys_w = 0
        self.canvas_phys_h = 0
        self._min_br = 300_000
        self._min_fps = 5.0          # fps floor — only reduced after bitrate hits minimum
        self._max_br = 50_000_000   # 50Mbps cap — plenty for any screenshare quality
        self.user_bw_cap = 0        # hard send-level cap in bits/sec; 0 = unlimited
        self.lag_budget_override = 0  # user-specified lag budget in ms; 0 = auto (50ms floor)
        # Congestion ceiling: bitrate at the moment the last backoff fired.
        # 0 = not yet measured — on_fresh probes slowly until first congestion event.
        self._ceil_bitrate = 0
        self._last_slow = 0.0
        self._last_fast = 0.0
        self._drain_until = 0.0     # monotonic deadline: stop sending until this time
        self._last_clear_t = 0.0   # monotonic time of last client "clear" (low-lag) confirmation
        self._lock = threading.Lock()
        self._ping_smooth = 0.0     # EWA-smoothed video ping RTT (jitter suppression)
        self._ping_history = []     # last 4 smoothed samples for gradient computation
        self._metric_rtt = 0.0      # EWA of unloaded metric-channel RTT; 0 = not measured yet

    @property
    def draining(self):
        """True while a transmit pause is active (waiting for downstream buffers to drain)."""
        return time.monotonic() < self._drain_until

    def end_drain_if_clear(self, write_buf):
        """Allow the sender to short-circuit the drain pause once the
        downstream queue has actually cleared.

        Two signals are required, because they capture different buffers:
          * write_buf low — server-side TCP send buffer is empty (catches
            real TCP backpressure scenarios like the tcp_throttle.py harness)
          * recent low-age client report — browser is decoding fresh frames
            (catches application-layer throttles like Chrome DevTools where
            the server's wb stays at 0 even when the client-side queue is
            deep — only the browser's own age_ms reports can confirm depth)
        """
        if write_buf < self.lag_wb_budget() // 2:
            with self._lock:
                # Wait for client confirmation of clear delivery; otherwise
                # the queue may be sitting in the browser's read-throttle
                # buffer where wb can't see it.
                if (self._last_clear_t > 0
                        and (time.monotonic() - self._last_clear_t) < 1.0
                        and self._drain_until > time.monotonic()):
                    self._drain_until = 0.0

    @property
    def frame_interval(self):
        return 1.0 / max(1.0, self.fps)

    def on_resolution(self, w, h):
        with self._lock:
            self.client_w = max(1, w)
            self.client_h = max(1, h)
            # w/h are physical canvas pixels (canvas.width × canvas.height after DPR scaling)
            self.canvas_phys_w = max(1, w)
            self.canvas_phys_h = max(1, h)

    def on_quality(self, cap_h: int, fps_cap: int, max_kbps: int = 0, lag_ms: int = 0):
        with self._lock:
            self.cap_h   = max(0, cap_h)
            self.fps_cap = max(0, fps_cap)
            ceil = float(self.fps_cap) if self.fps_cap > 0 else self.max_fps
            self.fps = min(self.fps, ceil)
            if max_kbps > 0:
                self._max_br = max_kbps * 1000
                self.user_bw_cap = self._max_br
                self.bitrate = min(self.bitrate, self._max_br)
            else:
                self._max_br = 50_000_000
                self.user_bw_cap = 0
            self.lag_budget_override = max(0, lag_ms)

    def effective_target(self, native_w: int, native_h: int):
        """Return (tw, th) — the target encode resolution.
        Never upscales; always preserves the source aspect ratio; dimensions are even."""
        with self._lock:
            if self.cap_h > 0:
                th = min(self.cap_h, native_h)
            elif self.canvas_phys_h > 0:
                th = min(self.canvas_phys_h, native_h)
            else:
                th = native_h
            tw = round(native_w * th / native_h) if native_h else native_w
            return (tw & ~1), (th & ~1)

    def _backoff(self, severe):
        """Reduce quality. Must be called with _lock held; enforces 300ms debounce.

        Priority: cut bitrate (quality) first — preserves fps (input responsiveness).
        fps is only reduced as a last resort when bitrate is already at the floor.
        fps floor is derived from lag_budget_ms so it never goes below one frame per
        budget period — e.g. at 50ms budget, min fps = 20 (not 5)."""
        now = time.monotonic()
        if now - self._last_slow < 0.3:
            return
        self._last_slow = now
        self._last_fast = 0.0
        factor = 0.5 if severe else 0.75
        # fps floor: never slower than one frame per budget window
        min_fps = max(self._min_fps, 1000.0 / self.lag_budget_ms())
        if self.bitrate > self._min_br:
            # Save congestion point before reducing — this is the network ceiling (SSTHRESH).
            # On recovery, ramp fast back to here, probe slowly above.
            self._ceil_bitrate = self.bitrate
            self.bitrate = max(self._min_br, int(self.bitrate * factor))
            self.jpeg_quality = max(10, int(self.jpeg_quality * factor))
        elif self.fps > min_fps:
            self.fps = max(min_fps, self.fps * factor)
        log.debug("backoff: fps=%.1f br=%dk ceil=%dk severe=%s min_fps=%.1f",
                  self.fps, self.bitrate // 1000, self._ceil_bitrate // 1000, severe, min_fps)

    def lag_budget_ms(self):
        """Allowed in-flight delay before congestion backoff fires.

        Auto: 1 frame interval (floors at 50ms at high fps, caps at 500ms).
        Override: user-specified value — higher = smoother video, more input latency.
        """
        if self.lag_budget_override > 0:
            return float(self.lag_budget_override)
        return max(50.0, min(1000.0 / max(1.0, self.fps), 500.0))

    def lag_wb_budget(self):
        """Write-buffer byte equivalent of lag_budget_ms at current bitrate.
        Scales with lag_budget_ms so a higher lag budget also tolerates a larger
        TCP send buffer before triggering backoff. Floor is 2 average frame sizes."""
        avg_frame = int(self.bitrate / max(1.0, self.fps) / 8)  # bytes per average frame
        return max(2 * avg_frame, 4 * 1024, int(self.lag_budget_ms() * self.bitrate / 8000))

    def on_lag(self, age_ms, write_buf=0):
        budget = self.lag_budget_ms()
        if age_ms > 0 and age_ms < budget and write_buf < self.lag_wb_budget():
            return
        if age_ms == 0 and write_buf < self.lag_wb_budget():
            return
        severe = age_ms > budget * 3 or write_buf > self.lag_wb_budget() * 6
        with self._lock:
            self._backoff(severe)
            # Transmit pause: when severe lag is reported (either via browser
            # age_ms or via local wb), the queue is sitting in some downstream
            # buffer. Halving bitrate helps long-term but doesn't drain the
            # existing queue fast. Pause sending so it can drain before we
            # resume — screen freezes briefly but recovers clean.
            #
            # Trigger on EITHER age (browser-side queue) OR wb (server-side
            # TCP backpressure). The wb-only path was previously gated by
            # age_ms > 300, which never fires when the lag signal is wb=0
            # because TCP backpressure reached us before the lag report did.
            wb_severe = write_buf > self.lag_wb_budget() * 12
            if (age_ms > 300 or wb_severe) and severe and not self.draining:
                # Pause length: take the LARGER of age-based and wb-based
                # drain estimates. wb at 2Mbps with 4MB queued = 16s of
                # buffered video — old 2s cap couldn't clear deep queues
                # and drains chained. 5s cap with realistic estimate per
                # signal closes the loop.
                age_pause = (age_ms - budget) / 1000.0
                wb_pause  = (write_buf * 8) / max(self.bitrate, 1)
                pause_s = min(5.0, max(age_pause, wb_pause))
                self._drain_until = time.monotonic() + pause_s
                log.debug("drain pause: %.0fms (age=%.0fms wb=%dKB budget=%.0fms)",
                          pause_s * 1000, age_ms, write_buf // 1024, budget)

    def on_ping_rtt(self, rtt_ms):
        """Two-signal congestion detection via video-channel RTT.

        Signal 1 — gradient (primary): RTT rising means a buffer is FORMING right now.
        Fires early, before the queue is large, and requires no baseline or metric channel.
        Link-agnostic: RTT going up is RTT going up regardless of absolute value.

        Signal 2 — delta vs metric (secondary): RTT stable but elevated above the unloaded
        metric channel means a STATIC buffer exists. This catches the case where the gradient
        already fired and settled, or where we joined mid-congestion. A static buffer is an
        unstable equilibrium; slight backoff drains it quickly."""
        with self._lock:
            # Smooth to suppress per-sample jitter before computing gradient
            self._ping_smooth = (self._ping_smooth * 0.6 + rtt_ms * 0.4
                                 if self._ping_smooth > 0 else rtt_ms)
            s = self._ping_smooth
            self._ping_history.append(s)
            if len(self._ping_history) > 4:
                self._ping_history.pop(0)

            # Signal 1: gradient — buffer FORMING
            gradient_fired = False
            if len(self._ping_history) >= 3:
                prev_mean = sum(self._ping_history[:-1]) / len(self._ping_history[:-1])
                gradient = s - prev_mean
                if gradient > 15:       # rising >15ms per 2s sample = queue building
                    self._backoff(gradient > 40)
                    gradient_fired = True
                    log.debug("ping gradient=%.1fms rtt=%.1fms", gradient, s)

            # Signal 2: delta — buffer STATIC (only when gradient hasn't already fired)
            # Threshold scales with lag budget: at 50ms budget fire at 50ms delta;
            # at 200ms budget fire at 200ms delta (user explicitly allows that much queuing).
            if not gradient_fired and self._metric_rtt > 0:
                delta = s - self._metric_rtt
                budget = self.lag_budget_ms()
                if delta > budget:
                    self._backoff(delta > budget * 2)
                    log.debug("ping delta=%.1fms rtt=%.1fms metric=%.1fms budget=%.0fms",
                              delta, s, self._metric_rtt, budget)

    def on_client_clear(self):
        """Client lag report confirmed path is clear — allow next ramp step promptly.

        Called when browser reports age_ms < budget: not a backoff signal but a positive
        'path is clear' confirmation. Used to unlock early ramp steps instead of waiting
        the full 2s heuristic interval."""
        with self._lock:
            self._last_clear_t = time.monotonic()

    def on_metric_rtt(self, rtt_ms):
        """RTT on the unloaded metric channel — pure link latency, no video queuing.
        Fast EWA (0.7/0.3) so link changes from WiFi↔5G roaming are reflected in ~4s."""
        with self._lock:
            if self._metric_rtt == 0.0:
                self._metric_rtt = rtt_ms
            else:
                self._metric_rtt = self._metric_rtt * 0.7 + rtt_ms * 0.3

    def on_fresh(self):
        with self._lock:
            now = time.monotonic()
            # Minimum tick: 100ms — lag reports are throttled to 10/s; no point checking faster.
            if now - self._last_fast < 0.1:
                return
            # Post-congestion settle: require 2s quiet after BOTH last backoff and drain end.
            # _last_slow is set at backoff START, not drain end — without the drain_until term
            # we'd ramp 0.65s after a 1.35s drain, immediately re-filling the cleared buffer.
            settle_until = max(self._last_slow + 2.0, self._drain_until + 2.0)
            if now < settle_until:
                return
            # Step interval: short when client actively confirms "clear" via lag reports,
            # long when flying blind (no metric_rtt or no recent clear signal).
            #
            # clear_window = max(500ms, 2×RTT): how recently a clear report must have arrived.
            # On a 20ms SSH tunnel, reports arrive every 100ms → clear_window=500ms → we step
            # every 100ms (limited by the 0.1s tick above). Each +20% step is validated by the
            # client before the next — application-layer ACK-clocking.
            # Without metric_rtt or with stale clear signal: 2s fallback (original heuristic).
            # clear_window = max(500ms, 2×RTT).  When metric_rtt is not yet measured (first
            # ping hasn't completed) use the 500ms floor so lag reports from the initial
            # connect can still gate the ramp — otherwise the first 2s would use the 2s
            # fallback regardless of what the browser is reporting.
            if self._metric_rtt > 0:
                clear_window = max(0.5, 2.0 * self._metric_rtt / 1000.0)
            else:
                clear_window = 0.5
            have_clear = (now - self._last_clear_t) < clear_window
            if not have_clear and now - self._last_fast < 2.0:
                return
            self._last_fast = now
            fps_ceil = float(self.fps_cap) if self.fps_cap > 0 else self.max_fps
            if self.fps < fps_ceil:
                self.fps = fps_ceil
            elif self.bitrate < self._max_br:
                # Ramp bitrate in +20% steps per 2s tick rather than jumping to the ceiling
                # in one hop. At 3Mbps after a 6Mbps congestion event this takes ~8 ticks
                # (~16s) to reach 5.4Mbps, giving time for lag reports to fire if we overshoot
                # before the queue builds to the drain-threshold again.
                # Below the known ceiling (where we've congested before): +20%/tick.
                # Above the ceiling (probing new territory): +10%/tick below 20Mbps, +5% above.
                target = int(self._ceil_bitrate * 0.90) if self._ceil_bitrate > 0 else self._max_br
                target = min(target, self._max_br)
                if self.bitrate < target:
                    step = max(int(self.bitrate * 1.20), self.bitrate + 200_000)
                    self.bitrate = min(target, step)
                elif self.bitrate < 20_000_000:
                    self.bitrate = min(self._max_br, int(self.bitrate * 1.10))
                else:
                    self.bitrate = min(self._max_br, int(self.bitrate * 1.05))
                self.jpeg_quality = min(95, self.jpeg_quality + 5)
            log.debug("fresh: fps=%.1f br=%dk ceil=%dk", self.fps, self.bitrate//1000, self._ceil_bitrate//1000)

    def on_screen_active(self):
        """Screen content changed after a static period — restore fps and jump toward last
        known stable bitrate. Uses 90% of the congestion ceiling (same as on_fresh recovery)
        to avoid immediately re-triggering congestion on every screen-active event."""
        with self._lock:
            fps_ceil = float(self.fps_cap) if self.fps_cap > 0 else self.max_fps
            self.fps = fps_ceil
            if self._ceil_bitrate > 0 and self._ceil_bitrate > self.bitrate:
                target = int(self._ceil_bitrate * 0.90)
                step_ceil = max(self.bitrate * 2, self.bitrate + 500_000)
                self.bitrate = max(self._min_br, min(target, step_ceil))
                self.jpeg_quality = min(95, self.jpeg_quality + 20)
            self._last_fast = time.monotonic()
            log.debug("screen active: fps=%.1f br=%dk ceil=%dk", self.fps, self.bitrate//1000, self._ceil_bitrate//1000)

    def snapshot(self):
        with self._lock:
            return self.fps, self.bitrate, self.jpeg_quality
