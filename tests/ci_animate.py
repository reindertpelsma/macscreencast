#!/usr/bin/env python3
"""Bouncing-shape NSWindow at 60fps to stress the H.264 encoder in CI.

Run as a background subprocess before the smoke/2Mbps tests. Fifteen
bouncing colored circles on a hue-cycling background force the macOS
compositor to produce visually complex new frames every tick, so H.264
P-frames are large enough to genuinely exercise the 2Mbps congestion
controller (solid-color cycling produces ~3KB P-frames; this produces
~30-80KB P-frames, which actually loads a 2Mbps link).

Stress is the contract: even with the screen visually saturated, the
end-to-end pipeline must still deliver 20fps. If GitHub's runner can't
hold 20fps under this animation, that's a real signal about the
system's responsiveness budget — not a reason to weaken the test.
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

WIN_W, WIN_H = 900, 650
NUM_BALLS    = 15
TICK_HZ      = 60

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(10, 10, WIN_W, WIN_H),
    AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskResizable,
    AppKit.NSBackingStoreBuffered,
    False,
)
win.setTitle_("CI Stress Animation")

view = win.contentView()
view.setWantsLayer_(True)
root = view.layer()


def _nscolor(h, s=0.9, b=0.95, a=1.0):
    return AppKit.NSColor.colorWithHue_saturation_brightness_alpha_(h, s, b, a)


# Bouncing ball state + a CALayer per ball.
balls = []
for _ in range(NUM_BALLS):
    r  = random.uniform(15, 45)
    bx = random.uniform(r, WIN_W - r)
    by = random.uniform(r, WIN_H - r)
    vx = random.choice([-1, 1]) * random.uniform(4, 9)
    vy = random.choice([-1, 1]) * random.uniform(4, 9)
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
