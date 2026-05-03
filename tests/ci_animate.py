#!/usr/bin/env python3
"""Borderless fullscreen bouncing-balls animation to stress the H.264 encoder.

Run as a background subprocess before the smoke/2Mbps tests. Fills the
entire main display with a hue-cycling background and bouncing colored
circles so SCK captures motion across the WHOLE captured frame, not
just a small windowed region. This is what gives the encoder enough
visual residual to actually load a 2Mbps link (a small windowed
animation only changes ~5% of the screen → tiny P-frames → ~0.5Mbps,
which the controller serves trivially without ever ramping up).

Stress is the contract: even with the screen visually saturated, the
end-to-end pipeline must still deliver 20fps and the controller must
find equilibrium near the link cap (≥1.7Mbps of the 2Mbps budget).
If GitHub's runner can't hold 20fps under this animation, that's a
real signal about the system's responsiveness budget — not a reason
to weaken the test.
"""
import sys, random

try:
    import AppKit
    from Quartz import (
        CALayer, CATransaction,
        CGRectMake, CGColorGetColorSpace,
    )
except ImportError:
    import time
    time.sleep(86400)
    sys.exit(0)

random.seed(42)   # reproducible layout across runs

NUM_BALLS    = 15
TICK_HZ      = 60

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

screen_frame = AppKit.NSScreen.mainScreen().frame()
WIN_W = int(screen_frame.size.width)
WIN_H = int(screen_frame.size.height)

win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(0, 0, WIN_W, WIN_H),
    AppKit.NSWindowStyleMaskBorderless,
    AppKit.NSBackingStoreBuffered,
    False,
)
win.setTitle_("CI Stress Animation")
win.setLevel_(AppKit.NSScreenSaverWindowLevel)
win.setIgnoresMouseEvents_(True)

view = win.contentView()
view.setWantsLayer_(True)
root = view.layer()


def _nscolor(h, s=0.9, b=0.95, a=1.0):
    return AppKit.NSColor.colorWithHue_saturation_brightness_alpha_(h, s, b, a)


# Bouncing ball state + a CALayer per ball.
# Sizes/velocities scaled so the balls traverse a meaningful fraction of
# whatever the runner's display happens to be (don't hardcode 900×650).
_scale = min(WIN_W, WIN_H) / 650.0
balls = []
for _ in range(NUM_BALLS):
    r  = random.uniform(30, 80) * _scale
    bx = random.uniform(r, WIN_W - r)
    by = random.uniform(r, WIN_H - r)
    vx = random.choice([-1, 1]) * random.uniform(8, 18) * _scale
    vy = random.choice([-1, 1]) * random.uniform(8, 18) * _scale
    h  = random.uniform(0.0, 1.0)

    layer = CALayer.layer()
    layer.setFrame_(CGRectMake(bx - r, by - r, r * 2, r * 2))
    layer.setCornerRadius_(r)
    layer.setBackgroundColor_(_nscolor(h).CGColor())
    root.addSublayer_(layer)

    balls.append({"layer": layer, "x": bx, "y": by, "r": r,
                  "vx": vx, "vy": vy, "h": h, "hv": random.uniform(0.003, 0.008)})

_bg_h = [0.0]


class _Ticker(AppKit.NSObject):
    def tick_(self, _timer):
        # Background: slow hue cycle
        _bg_h[0] = (_bg_h[0] + 2.5) % 360.0
        bg = _nscolor(_bg_h[0] / 360.0, 0.75, 0.65)
        root.setBackgroundColor_(bg.CGColor())

        # Balls: move + bounce + hue drift.
        # Disable implicit animations so every position update appears
        # immediately — each captured frame sees discrete new positions,
        # maximising the H.264 residual per P-frame.
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        for b in balls:
            b["x"] += b["vx"]
            b["y"] += b["vy"]
            if b["x"] < b["r"] or b["x"] > WIN_W - b["r"]:
                b["vx"] *= -1
            if b["y"] < b["r"] or b["y"] > WIN_H - b["r"]:
                b["vy"] *= -1
            b["h"] = (b["h"] + b["hv"]) % 1.0
            r = b["r"]
            b["layer"].setFrame_(CGRectMake(b["x"] - r, b["y"] - r, r * 2, r * 2))
            b["layer"].setBackgroundColor_(_nscolor(b["h"]).CGColor())
        CATransaction.commit()


win.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)

_t = _Ticker.alloc().init()
AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
    1.0 / TICK_HZ, _t, "tick:", None, True
)

app.run()
