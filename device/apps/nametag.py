"""Nametag app

The white band between the red header and footer alternates between the name
(nametag_band_v1) and every image in /data/nametag, in file-name order:
name, image 1, name, image 2, ..., name, image N, then back to the start.
UserA steps forward, UserB steps back, and the band advances by itself every
_CYCLE_MS. tools/build_nametag_monochrome.ps1 makes the images.
Before each image the band is first refreshed to plain white, so the name
lettering is wiped before the image goes on.
"""

import os
import time

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display
from lib.led_bar import leds


_BAND_TOP = 221
_BAND_BOTTOM = 557
_NAME_BAND = "/data/graphics/nametag_band_v1.png"
_IMAGE_DIR = "/data/nametag"
_CYCLE_MS = 10_000
_FAST_PASSES = 2
# Dithered images keep a faint ghost of the name lettering after the usual
# passes, so they get one more full fast refresh of the band.
_IMAGE_FAST_PASSES = _FAST_PASSES + 2
# Fast passes for the all-white band shown between the name and an image.
_BLANK_FAST_PASSES = _FAST_PASSES


def _band_sequence():
    """The name band before each image, so it always shows between two images."""
    try:
        names = sorted(n for n in os.listdir(_IMAGE_DIR) if n.lower().endswith(".png"))
    except OSError:
        names = []
    sequence = []
    for name in names:
        sequence.append(_NAME_BAND)
        sequence.append(_IMAGE_DIR + "/" + name)
    return sequence or [_NAME_BAND]


class NametagApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Nametag"
        self.sleep_ms = 100
        self.launched = False
        self.bands = [_NAME_BAND]
        self.design_idx = 0
        self.last_update_ms = 0
        self._initial_pending = False
        self._band_pending = False
        self._waiting_for_refresh = False
        # The band now on the panel, or None while it is blanked to white.
        self._shown_band = None

    def _draw_initial(self):
        leds.fill((0, 1, 1))
        display.clear()
        display.draw_png("/data/graphics/hello_i_am_blank.png")
        display.draw_png(self.bands[self.design_idx], _BAND_TOP, 0)
        # Establish a clean red header and known panel state once on launch.
        # Subsequent rotations hold the header and refresh only the name band.
        display.update(slow=True)
        self._shown_band = self.bands[self.design_idx]
        self._waiting_for_refresh = True

    def _blank_band(self):
        # Paint the band white and refresh only the band, so every pixel the
        # name used is driven back to white before the image is drawn.
        height = _BAND_BOTTOM - _BAND_TOP
        display.black.fill_rect(0, _BAND_TOP, display.width, height, 0)
        display.red.fill_rect(0, _BAND_TOP, display.width, height, 0)
        display.update(
            y_start=_BAND_TOP, y_end=_BAND_BOTTOM, fast_passes=_BLANK_FAST_PASSES,
        )
        self._shown_band = None

    def _draw_band_only(self):
        leds.fill((0, 1, 1))
        band = self.bands[self.design_idx]
        if band != _NAME_BAND and self._shown_band is not None:
            # Blank first, then draw the image on the next run() after the
            # blank finishes (or whatever band the buttons chose meanwhile).
            # The blank is not a finished refresh: the LED stays busy and the
            # cycle timer restarts only once the image is up.
            self._blank_band()
            self._band_pending = True
            return
        # One-bit band assets contain no red. Only this band selects the short
        # no-red waveform; the red header remains held outside it.
        display.draw_png(band, _BAND_TOP, 0)
        passes = _FAST_PASSES if band == _NAME_BAND else _IMAGE_FAST_PASSES
        display.update(
            y_start=_BAND_TOP, y_end=_BAND_BOTTOM, fast_passes=passes,
        )
        self._shown_band = band
        self._waiting_for_refresh = True

    def run(self):
        if self.launched:
            # Re-read /data/nametag on each launch so newly pushed images join
            # the rotation, and start on the name.
            self.bands = _band_sequence()
            self.design_idx = 0
            self._initial_pending = True
        elif UserA.pressed():
            self.design_idx = (self.design_idx + 1) % len(self.bands)
            self._band_pending = True
        elif UserB.pressed():
            self.design_idx = (self.design_idx - 1) % len(self.bands)
            self._band_pending = True

        # Remember button presses while the panel works; draw the latest choice
        # when all fast redraws have finished.
        if not display.is_done_updating():
            return
        if self._waiting_for_refresh:
            self._waiting_for_refresh = False
            self.last_update_ms = time.ticks_ms()
            leds.fill((0, 1, 0))

        if self._initial_pending:
            self._initial_pending = False
            self._band_pending = False
            self._draw_initial()
        elif self._band_pending:
            self._band_pending = False
            self._draw_band_only()
        elif (
            len(self.bands) > 1
            and time.ticks_diff(time.ticks_ms(), self.last_update_ms) >= _CYCLE_MS
        ):
            self.design_idx = (self.design_idx + 1) % len(self.bands)
            self._draw_band_only()
