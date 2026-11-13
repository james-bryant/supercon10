"""Light show app: badges running it animate their LEDs in step.

The badges agree on a clock over ESP-NOW (lib/mesh_sync.py) and each works out
its own LEDs from that clock, so they stay together without sending frames.
UserA picks the next pattern and UserB the previous one; the choice spreads to
the other badges within a second or two. The screen is drawn once when the app
opens and then left alone, because redrawing it would stall the LEDs.
"""

import math
import time

import lvgl

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display, display_height, display_width
from lib.led_bar import NUM_LEDS, leds

try:
    import lib.mesh_sync as mesh_sync
except Exception as ex:  # no (working) mesh on this badge: animate alone, but never block boot
    print("lights: no shared clock -", repr(ex))
    mesh_sync = None

PATTERNS = ("rainbow", "pulse", "chase", "flash", "cylon")  # new ones go at the end
BRIGHT = 24               # out of 255: these LEDs are bright (the menu uses 3)
PAD = 16

_WHITE = lvgl.color_hex(0xFFFFFF)
_BLACK = lvgl.color_hex(0x000000)
_BIG = getattr(lvgl, "font_montserrat_48", lvgl.font_montserrat_24)
_MID = lvgl.font_montserrat_24


def _hue(h):
    """A rainbow colour at BRIGHT, for h in [0, 1)."""
    h = (h % 1.0) * 6
    k = int(h) % 6
    up = int(BRIGHT * (h - int(h)))
    down = BRIGHT - up
    return ((BRIGHT, up, 0), (down, BRIGHT, 0), (0, BRIGHT, up),
            (0, down, BRIGHT), (up, 0, BRIGHT), (BRIGHT, 0, down))[k]


def _cylon(t):
    """A red eye that sweeps from end to end and back every 2 s, with a fading tail."""
    glow = [0.0] * NUM_LEDS
    for lag, fade in ((0, 1.0), (100, 0.5), (200, 0.25), (300, 0.12)):  # the eye, then where it was
        x = (t - lag) % 2000 / 1000             # 0 to 2: out, then back
        eye = (x if x < 1 else 2 - x) * (NUM_LEDS - 1)
        for i in range(NUM_LEDS):
            level = (1 - abs(i - eye) / 1.5) * fade  # lights the LEDs within 1.5 of the eye
            if level > glow[i]:
                glow[i] = level
    return [(int(BRIGHT * g), 0, 0) for g in glow]


def frame(pattern, t):
    """The colours of the LEDs for a pattern at shared time t (ms).

    Every badge with the same t shows the same colours. Times are reduced with
    integer % first: the badge's floats can't hold a big t to the ms.
    """
    if pattern == 0:  # rainbow: turns once every 4 s
        base = (t % 4000) / 4000
        return [_hue(base + i / NUM_LEDS) for i in range(NUM_LEDS)]
    if pattern == 1:  # pulse: every badge breathes together, once every 2 s
        level = (1 - math.cos(2 * math.pi * (t % 2000) / 2000)) / 2
        return [(int(BRIGHT * level / 2), 0, int(BRIGHT * level))] * NUM_LEDS
    if pattern == 2:  # chase: a dot runs along the bar, 80 ms per LED
        step = t // 80 % NUM_LEDS
        return [(0, BRIGHT, 0) if i == step else
                (0, BRIGHT // 6, 0) if i == (step - 1) % NUM_LEDS else (0, 0, 0)
                for i in range(NUM_LEDS)]
    if pattern == 3:  # flash: a short white flash every second, the easiest way to see the sync
        return [(BRIGHT, BRIGHT, BRIGHT) if t % 1000 < 60 else (0, 0, 0)] * NUM_LEDS
    return _cylon(t)  # cylon: a red glow moving back and forth


def _label(parent, font, text, width=0):
    label = lvgl.label(parent)
    label.remove_style_all()
    label.set_style_text_font(font, 0)
    label.set_style_text_color(_BLACK, 0)
    if width:
        label.set_width(width)  # fixed width, so long text wraps
    label.set_text(text)
    return label


class LightShowApp(BaseApp):
    def __init__(self, name=None):
        super().__init__("Light show")
        self.sleep_ms = 20   # about 40 frames a second
        self._synced = False
        self._pattern = 0    # used when there's no shared clock
        self._scr = None

    def run(self):
        if self.launched:
            self._join()
            self._draw()
        if UserA.pressed():
            self._choose(1)
        elif UserB.pressed():
            self._choose(-1)
        if self._synced:
            t, p = mesh_sync.now(), mesh_sync.pattern()
        else:
            t, p = time.ticks_ms(), self._pattern
        for i, colour in enumerate(frame(p % len(PATTERNS), t)):
            leds[i] = colour  # then one write: newer LedBar.set() writes as well
        leds.write()

    def _join(self):
        if mesh_sync is not None and not self._synced:
            try:
                self._synced = mesh_sync.start()
            except Exception as ex:  # e.g. an older lib/espnow_mesh.py
                print("lights: no shared clock -", repr(ex))

    def _choose(self, step):
        current = mesh_sync.pattern() if self._synced else self._pattern
        n = (current + step) % len(PATTERNS)
        if self._synced:
            mesh_sync.set_pattern(n)
        else:
            self._pattern = n
        print("lights:", PATTERNS[n])

    def _draw(self):
        if self._scr is None:
            scr = lvgl.obj()
            scr.remove_style_all()
            scr.set_size(display_width, display_height)
            scr.set_style_bg_color(_WHITE, 0)
            scr.set_style_bg_opa(lvgl.OPA.COVER, 0)
            _label(scr, _BIG, "Light show").set_pos(PAD, PAD)
            box = lvgl.obj(scr)  # lines stack top down, wrapping as needed
            box.remove_style_all()
            box.set_pos(PAD, 100)
            box.set_size(display_width - 2 * PAD, display_height - 100 - PAD)
            box.set_flex_flow(lvgl.FLEX_FLOW.COLUMN)
            box.set_style_pad_row(18, 0)
            width = display_width - 2 * PAD
            if self._synced:
                _label(box, _MID, "Badges running this app light up together, on a clock "
                                  "they share over ESP-NOW. Changing the pattern changes it "
                                  "on every badge in the show.", width)
            else:
                _label(box, _MID, "No mesh on this badge, so it animates on its own.", width)
            _label(box, _MID, "UserA: next pattern      UserB: previous pattern", width)
            _label(box, _MID, "Patterns: " + ", ".join(PATTERNS), width)
            self._scr = scr
        display.clear()
        lvgl.screen_load(self._scr)
        self._scr.invalidate()  # draw all of it, even if LVGL thinks it's on screen
        self._scr.update_layout()
        lvgl.refr_now(display.lv_display)
        try:
            display.update(slow=True)  # one full refresh: solid black text, nothing left behind
        except TypeError:
            display.update()  # the original driver: always a full refresh
