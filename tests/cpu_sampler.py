"""cpu_sampler.py — background sampler for WindowServer + python3 CPU%.

Used by ci_smoke.py and test_2mbps.py to distinguish a real responsiveness
regression from a CI-runner CPU bottleneck. If max(WindowServer, python3)
saturates above SATURATION_PCT during the test, the fps bar is treated
as runner-limited rather than a system failure.
"""
import subprocess, threading, time

SATURATION_PCT  = 90.0   # CPU-bound: encoder/compositor pegged
HEADROOM_PCT    = 50.0   # both well under this = runner can't deliver more frames
                         # (SCK/compositor delivery rate cap, not a system bug)


class CpuSampler(threading.Thread):
    """Samples %cpu of `python3 server.py` and `WindowServer` every 0.5s."""

    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.max_python     = 0.0
        self.max_winserver  = 0.0
        self.samples        = 0

    def run(self):
        py_pid = self._find_pid("python3 server.py") or self._find_pid("server.py")
        ws_pid = self._find_pid("WindowServer")
        if py_pid is None or ws_pid is None:
            return
        while not self._stop.is_set():
            self._sample(py_pid, "max_python")
            self._sample(ws_pid, "max_winserver")
            self.samples += 1
            time.sleep(0.5)

    def _sample(self, pid, attr):
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "%cpu="],
                text=True, timeout=1,
            ).strip()
            cpu = float(out)
            if cpu > getattr(self, attr):
                setattr(self, attr, cpu)
        except Exception:
            pass

    def _find_pid(self, pattern):
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", pattern], text=True,
            ).strip()
            return int(out.split("\n")[0]) if out else None
        except Exception:
            return None

    def stop(self, timeout=2.0):
        self._stop.set()
        self.join(timeout=timeout)

    @property
    def saturated(self) -> bool:
        return max(self.max_python, self.max_winserver) > SATURATION_PCT

    @property
    def runner_capped(self) -> bool:
        """True if both server and WindowServer are clearly under-utilised.
        Means the runner can't deliver more frames regardless of what we do —
        SCK/compositor delivery rate cap on virtualized macOS CI hardware."""
        return (self.samples > 0
                and self.max_python    < HEADROOM_PCT
                and self.max_winserver < HEADROOM_PCT)

    def summary(self) -> str:
        return (f"python={self.max_python:.0f}%  "
                f"WindowServer={self.max_winserver:.0f}%  "
                f"samples={self.samples}")
