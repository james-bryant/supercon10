"""E-Paper Display Driver for Billboard Badge

Ported from Wiring in https://github.com/PervasiveDisplays/EPD_Driver_GU_large/blob/e408154652802a82e520cc25ba04adeb347d0528/src/EPD_Driver.cpp

--COPYRIGHT--
Brief The drivers and functions of development board
Copyright (c) 2012-2021 Pervasive Displays Inc. All rights reserved.
 Authors: Pervasive Displays Inc.
 Redistribution and use in source and binary forms, with or without
 modification, are permitted provided that the following conditions
 are met:
 1. Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.
 2. Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in
    the documentation and/or other materials provided with the
    distribution.
 THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import asyncio as aio
import btree
import framebuf
import gc
from machine import Pin, SPI, SoftSPI
import micropython
from micropython import const
import time

import lvgl

from data.rendered_luts import (
    lut8_nored_clear,
    lut8_red_clear,
    lutb_base,
)
from lib.large_png import show_png
from lib.led_bar import leds

EPD_CS1_PIN = Pin(13, mode=Pin.OUT)
EPD_CS2_PIN = Pin(3, mode=Pin.OUT)
EPD_BUSY1_PIN = Pin(12, mode=Pin.IN)
EPD_BUSY2_PIN = Pin(8, mode=Pin.IN)
EPD_DC_PIN = Pin(10, mode=Pin.OUT)  # Data High Command Low
EPD_RST_PIN = Pin(11, mode=Pin.OUT)
EPD_SCL_PIN = Pin(21, mode=Pin.OUT)
EPD_SDA_PIN = Pin(47, mode=Pin.OUT)
EPD_SPI_MISO_PIN = Pin(45, mode=Pin.IN)  # Not connected, signal not needed
EPD_PWR_EN_PIN = Pin(48, mode=Pin.OUT, value=False)


pdi_cp = 0x9608
pdi_size = 0x96  # 9.69"
display_width = const(960)
half_width = const(display_width // 2)
display_height = const(672)
# Frame-buffer size = width * height / 8 * depth, uint32_t
# image_data_size is really only half the panel pixels, of one color
half_frame_one_color_size = const(display_height * display_width // (8 * 2))
# The width of rows in the full-frame and half-frame framebuffers
# Counting bytes, not pixels, since each byte is 8 packed pixels
display_width_bytes = const(display_width // 8)
display_half_width_bytes = const(display_width_bytes // 2)
# How long _wait_ready() gives the second COG half once the first is ready
_BUSY_GRACE_MS = const(500)


# Two displays use different bit orders
def reverse_byte(byte: int) -> int:
    byte = (byte & 0xF0) >> 4 | (byte & 0x0F) << 4
    byte = (byte & 0xCC) >> 2 | (byte & 0x33) << 2
    byte = (byte & 0xAA) >> 1 | (byte & 0x55) << 1
    return byte


_REV_LUT = bytes(reverse_byte(i) for i in range(256))

# Build Driver B 2-pass fast LUTs from lutb_base:
# - Table 0 ((red=0, bw=0)): drive White
# - Table 1 ((red=0, bw=1)): drive Black
# - Table 2 ((red=1, bw=0)): drive Red
# - Table 3 ((red=1, bw=1)): 0V Hold (leaves non-dirty pixels untouched!)
_LUTB_PASS1_RED = bytes(lutb_base)

_lutb_p1_nored = list(lutb_base)
for _g in range(5, 10):
    _off = 160 + _g * 8
    _lutb_p1_nored[_off : _off + 8] = [0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01]
_LUTB_PASS1_NORED = bytes(_lutb_p1_nored)

# Pass 2 (Cleanup Pass): immediately follows Pass 1 over the same dirty pixels
# without bipolar shake or white-flash on red, pulling any lagging ghosting
# cleanly to its target state while Table 3 holds non-dirty pixels at 0V.
_lutb_clean_red = list(lutb_base)
# Table 0 (White cleanup): pure VDL white drive (0xaa) in Group 0..4 (no shake)
_lutb_clean_red[0:8] = [0xaa, 0x0a, 0x0a, 0x01, 0x0a, 0x0a, 0x01, 0x03]
# Table 1 (Black cleanup): pure chopped VDH black drive (0x44) in Group 0..4 (no shake)
_lutb_clean_red[80:88] = [0x44, 0x0a, 0x0a, 0x01, 0x0a, 0x0a, 0x01, 0x03]
# Table 2 (Red cleanup): pure VDHR red drive (0xff) in Group 0..6 (no Group 1 white clear)
_lutb_clean_red[160:168] = [0xff, 0x0a, 0x0a, 0x01, 0x0a, 0x0a, 0x01, 0x03]
_lutb_clean_red[168:176] = [0xff, 0x2d, 0x01, 0x01, 0x2d, 0x01, 0x01, 0x03]
for _g in range(7, 10):
    _off = 160 + _g * 8
    _lutb_clean_red[_off : _off + 8] = [0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01]
_LUTB_CLEANUP_RED = bytes(_lutb_clean_red)

_lutb_clean_nored = list(_lutb_clean_red)
for _g in range(5, 10):
    _off = 160 + _g * 8
    _lutb_clean_nored[_off : _off + 8] = [0x00, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01]
_LUTB_CLEANUP_NORED = bytes(_lutb_clean_nored)
del _lutb_p1_nored, _lutb_clean_red, _lutb_clean_nored

# Dirty masking also requires Table 3 to hold on driver 8. The source tables
# otherwise drive (1, 1) pixels black, including pixels outside the name band.
_lut8_red = list(lut8_red_clear)
_lut8_nored = list(lut8_nored_clear)
for _off in range(108, 144, 4):
    _lut8_red[_off] = 0x00
    _lut8_nored[_off] = 0x00
_LUT8_RED = bytes(_lut8_red)
_LUT8_NORED = bytes(_lut8_nored)
del _lut8_red, _lut8_nored


@micropython.viper
def _split_and_reverse_viper(
    fbk_buf,
    fbr_buf,
    prev_fbk_buf,
    prev_fbr_buf,
    bl_buf,
    br_buf,
    rl_buf,
    rr_buf,
    rev_lut,
    do_rev: int,
    use_dirty_mask: int,
    y_start: int,
    y_end: int,
) -> int:
    fbk = ptr8(fbk_buf)
    fbr = ptr8(fbr_buf)
    pfbk = ptr8(prev_fbk_buf)
    pfbr = ptr8(prev_fbr_buf)
    bl = ptr8(bl_buf)
    br = ptr8(br_buf)
    rl = ptr8(rl_buf)
    rr = ptr8(rr_buf)
    rev = ptr8(rev_lut)
    has_red = 0
    for row in range(display_height):
        src_l = row * display_width_bytes
        src_r = src_l + display_half_width_bytes
        dst = row * display_half_width_bytes
        if use_dirty_mask and (row < y_start or row >= y_end):
            # Outside the active vertical band: send (1, 1) -> Table 3 (0V Hold)
            # so banners/headers outside [y_start, y_end) receive 0V and never fade.
            for i in range(display_half_width_bytes):
                bl[dst + i] = 0xFF
                br[dst + i] = 0xFF
                rl[dst + i] = 0xFF
                rr[dst + i] = 0xFF
            continue
        for i in range(display_half_width_bytes):
            il = src_l + i
            ir = src_r + i
            b1 = fbk[il]
            b2 = fbk[ir]
            r1 = fbr[il]
            r2 = fbr[ir]
            if use_dirty_mask == 1:
                # Track exact dirty pixels where black or red changed from previous frame.
                # Non-dirty pixels are set to (b=1, r=1) -> Table 3 (0V Hold).
                # Dirty pixels keep their target (0,0)=White, (1,0)=Black, (0,1)=Red.
                dirty1 = (b1 ^ pfbk[il]) | (r1 ^ pfbr[il])
                dirty2 = (b2 ^ pfbk[ir]) | (r2 ^ pfbr[ir])
                pfbk[il] = b1
                pfbr[il] = r1
                pfbk[ir] = b2
                pfbr[ir] = r2
                has_red |= (r1 & dirty1) | (r2 & dirty2)
                nd1 = dirty1 ^ 0xFF
                nd2 = dirty2 ^ 0xFF
                b1 |= nd1
                r1 |= nd1
                b2 |= nd2
                r2 |= nd2
            else:
                pfbk[il] = b1
                pfbr[il] = r1
                pfbk[ir] = b2
                pfbr[ir] = r2
                has_red |= r1 | r2
            if do_rev:
                bl[dst + i] = rev[b1]
                br[dst + i] = rev[b2]
                rl[dst + i] = rev[r1]
                rr[dst + i] = rev[r2]
            else:
                bl[dst + i] = b1
                br[dst + i] = b2
                rl[dst + i] = r1
                rr[dst + i] = r2
    return has_red


@micropython.viper
def _viper_lv_flush(
    src: ptr8,
    fb_black: ptr8,
    fb_red: ptr8,
    x1: int,
    y1: int,
    w: int,
    h: int,
    stride: int,
):
    width = 960
    for row in range(h):
        base = (y1 + row) * width + x1
        row_off = row * stride
        for col in range(w):
            i = row_off + (col << 1)
            c = src[i] | (src[i + 1] << 8)  # RGB565, little-endian
            idx = base + col
            byte_idx = idx >> 3
            mask = 1 << (7 - (idx & 7))
            clear = mask ^ 0xFF
            bits = c & 0x8410
            if bits == 0x8000:
                fb_red[byte_idx] |= mask
                fb_black[byte_idx] &= clear
            elif bits == 0:
                fb_black[byte_idx] |= mask
                fb_red[byte_idx] &= clear
            else:
                fb_black[byte_idx] &= clear
                fb_red[byte_idx] &= clear


class EPaperDisplay:
    width = display_width
    height = display_height

    def __init__(self) -> None:
        print("Initializing E-Paper Display (Fast Hardware SPI + Viper)")
        EPD_DC_PIN.value(True)
        EPD_RST_PIN.value(True)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        try:
            self.spi = SPI(
                1,
                baudrate=12_000_000,
                sck=EPD_SCL_PIN,
                mosi=EPD_SDA_PIN,
                miso=EPD_SPI_MISO_PIN,
            )
        except Exception:
            self.spi = SoftSPI(
                baudrate=8_000_000,
                sck=EPD_SCL_PIN,
                mosi=EPD_SDA_PIN,
                miso=EPD_SPI_MISO_PIN,
            )
            self.spi.init()

        try:
            with open("/data/hwconf", "rb") as f:
                hwconf = btree.open(f)
                display_pn = hwconf.get(b"display_pn", b"B1")
                if display_pn == b"84":
                    self._reverse_fb_bits = False
                else:
                    self._reverse_fb_bits = True
        except OSError:
            self._reverse_fb_bits = True

        # Fast LUT refresh is default; pass slow=True to update() for factory OTP waveform.
        self.fast_mode = True
        self._next_slow = False
        self._next_fast_passes = 1
        self._has_prev = False
        self._y_start = 0
        self._y_end = display_height
        # Wait for both COG halves until a BUSY line proves unreliable.
        self._busy_both = True

        # The framebuffers are set and display should be updated.
        self._framebuffer_ready = aio.Event()
        # Panel capability stays stable while the worker sends prepared frames.
        self._supports_grayscale = self._reverse_fb_bits
        self._requested_grayscale = False
        self._requested_mixed = False
        self._render_grayscale = False
        self._render_mixed = False
        # E-paper keeps its image across reboot; its initial pigments are unknown.
        self._panel_monochrome = False
        self._png_blink = False
        self._refreshing_hw = False

        """Each byte is a mini-row of 8 bits for pixels of one color (1 sets the color, 0 is white)
        Bytes are in rows across the screen, as seen on the badge
        0 ------------------------> 59 [60 bytes per row]
        60 ----------------------> 119
        ...                        ...
        [672 rows of 60 bytes per row]
        """
        self._framebuffer_black = bytearray(half_frame_one_color_size * 2)
        self._framebuffer_red = bytearray(half_frame_one_color_size * 2)
        # Track previous frame's black/red bits so partial/incremental updates
        # route non-dirty pixels to Table 3 (0V Hold) and drive only dirty pixels.
        self._prev_black = bytearray(half_frame_one_color_size * 2)
        self._prev_red = bytearray(half_frame_one_color_size * 2)
        # Framebuffers that can be drawn to directly
        self.black = framebuf.FrameBuffer(
            self._framebuffer_black, display_width, display_height, framebuf.MONO_HLSB
        )
        self.red = framebuf.FrameBuffer(
            self._framebuffer_red, display_width, display_height, framebuf.MONO_HLSB
        )

        # Is the display done updating? Start set since the display is not updating before boot.
        self._display_updated = aio.Event()
        self._display_updated.set()
        # Held while something drives the panel: run() below, or an opt-in driver
        # of its own such as lib/epd_partial.py. Keeps the two from overlapping.
        self.lock = aio.Lock()
        gc.collect()

        # Set up lvgl (deinit first if soft-rebooted so C pointers don't corrupt)
        if hasattr(lvgl, "is_initialized") and lvgl.is_initialized():
            if hasattr(lvgl, "deinit"):
                lvgl.deinit()
        lvgl.init()
        # Give LVGL a buffer 2-bytes-per-pixel for RGB565 format
        self._lv_draw_buf = bytearray(display_width * display_height * 2)

        self.lv_display = lvgl.display_create(display_width, display_height)
        self.lv_display.set_color_format(lvgl.COLOR_FORMAT.RGB565)
        self.lv_display.set_buffers(
            self._lv_draw_buf,
            None,
            len(self._lv_draw_buf),
            lvgl.DISPLAY_RENDER_MODE.PARTIAL,
        )
        self.lv_display.set_flush_cb(self._lv_flush)
        self.lv_screen = lvgl.screen_active()
        self.lv_screen.set_size(display_width, display_height)

    def _lv_flush(self, disp, area, color_p):
        print("Calling EPaperDisplay._lv_flush (Viper)")
        leds[0] = (0, 0, 50)
        leds.write()
        x1, y1 = area.x1, area.y1
        w = area.x2 - x1 + 1
        h = area.y2 - y1 + 1
        try:
            stride = lvgl.draw_buf_width_to_stride(w, lvgl.COLOR_FORMAT.RGB565)
        except AttributeError:
            stride = w * 2
        src = color_p.__dereference__(stride * h)
        convert = _viper_lv_flush
        if (self._render_grayscale or self._render_mixed) and self.supports_grayscale:
            from lib import epd_grayscale
            convert = (epd_grayscale.rgb565_to_mixed if self._render_mixed
                       else epd_grayscale.rgb565_to_grayscale)
        convert(
            src,
            self._framebuffer_black,
            self._framebuffer_red,
            x1,
            y1,
            w,
            h,
            stride,
        )
        leds[0] = (0, 0, 0)
        leds.write()
        disp.flush_ready()

    @property
    def supports_grayscale(self):
        return self._supports_grayscale

    def _check_modes(self, grayscale, mixed):
        if grayscale and mixed:
            raise ValueError("Choose grayscale or mixed mode")
        if (grayscale or mixed) and not self.supports_grayscale:
            raise ValueError("Grayscale refresh requires a B1 panel")

    def refresh_lvgl(self, grayscale=False, mixed=False):
        """Render LVGL into the framebuffers; use the same mode in update().

        Default rendering keeps the original B/W/red thresholds. Grayscale
        produces B/W/gray; mixed preserves red and adds gray as a fourth color.
        """
        self._check_modes(grayscale, mixed)
        previous_grayscale = self._render_grayscale
        previous_mixed = self._render_mixed
        try:
            self._render_grayscale = grayscale
            self._render_mixed = mixed
            lvgl.refr_now(self.lv_display)
        finally:
            self._render_grayscale = previous_grayscale
            self._render_mixed = previous_mixed

    def update(
        self, slow: bool = False, y_start: int = 0,
        y_end: int = display_height, fast_passes: int = 1,
        grayscale: bool = False, mixed: bool = False,
    ):
        """Queue the latest frame. By default only the changed pixels get the
        fast LUT refresh.

        Repeated fast passes drive every pixel in the requested band, including
        unchanged white and ink. Each pass uses the same frozen image and holds
        the surroundings. slow=True uses one full factory refresh instead.

        Gray waveforms are opt-in, require B1 and always redraw the whole panel:
        grayscale=True uses 00 white, 01 black, 10 gray (no red).
        mixed=True uses 00 white, 01 black, 10 red, 11 gray in software.
        Match the mode used by refresh_lvgl()/draw_png().
        """
        self._check_modes(grayscale, mixed)
        if not 0 <= y_start < y_end <= display_height:
            raise ValueError("invalid display update band")
        if not isinstance(fast_passes, int) or not 1 <= fast_passes <= 5:
            raise ValueError("fast_passes must be an integer from 1 to 5")
        if self._framebuffer_ready.is_set():
            self._next_slow |= slow
            self._next_fast_passes = max(self._next_fast_passes, fast_passes)
            self._y_start = min(self._y_start, y_start)
            self._y_end = max(self._y_end, y_end)
        else:
            self._next_slow = slow
            self._next_fast_passes = fast_passes
            self._y_start = y_start
            self._y_end = y_end
        # The latest request names the mode its frame was drawn in.
        self._requested_grayscale = grayscale
        self._requested_mixed = mixed
        self._display_updated.clear()
        self._framebuffer_ready.set()

    def clear(self):
        """Clear framebuffers to full white"""
        self.black.fill(0)
        self.red.fill(0)
        self._has_prev = False

    def is_done_updating(self):
        """Is the display done updating?"""
        return self._display_updated.is_set()

    def draw_image(self, image, height: int, width: int, row: int = 0, col: int = 0):
        for img_r in range(height):
            for img_c in range(width):
                color = image[img_r][img_c]
                red = color[0] > 127
                black = color[0] < 128 and color[1] < 128 and color[2] < 128
                self.black.pixel((col + img_c), (row + img_r), int(black))
                self.red.pixel((col + img_c), (row + img_r), int(red))

    """
    Draw a PNG
    
    If drawing a PNG from a byte array, pass None to `filename` and pass the byte array in `data`
    """
    def draw_png(self, filename: str = None, row: int = 0, col: int = 0, grayscale: bool = False, mixed: bool = False, data: bytes = None):
        """Draw a PNG; use the same grayscale/mixed mode in update()."""
        self._check_modes(grayscale, mixed)
        gc.collect()
        self._png_blink = not self._png_blink
        leds[0] = (0, 0, 50) if self._png_blink else (0, 0, 10)
        leds.write()
        print(f"Loading {filename} This will take a while... {time.time()}")
        show_png(filename, self._framebuffer_black, self._framebuffer_red, row, col, grayscale, mixed, data)
        leds[0] = (0, 0, 0)
        leds.write()
        print("Done loading png")
        gc.collect()

    async def _blink_red_while_refreshing(self):
        state = False
        while self._refreshing_hw:
            state = not state
            leds[0] = (40, 0, 0) if state else (0, 0, 0)
            leds.write()
            await aio.sleep_ms(200)

    async def run(self):
        black_left = bytearray(half_frame_one_color_size)
        black_right = bytearray(half_frame_one_color_size)
        red_left = bytearray(half_frame_one_color_size)
        red_right = bytearray(half_frame_one_color_size)
        while True:
            if self._framebuffer_ready.is_set():
                self._display_updated.clear()
                grayscale = self._requested_grayscale
                mixed = self._requested_mixed
                gray = grayscale or mixed
                # Gray helpers consume logical-order planes and reverse on send.
                do_rev = 1 if self._reverse_fb_bits and not gray else 0
                slow = gray or self._next_slow or not self.fast_mode
                fast_passes = 1 if slow else self._next_fast_passes
                y_start = self._y_start
                y_end = self._y_end
                # Repeats must not be optimized to all-hold after the first
                # write: logical equality does not mean the pigment has settled.
                if slow:
                    use_dirty_mask = 0
                elif fast_passes > 1:
                    use_dirty_mask = 2
                else:
                    use_dirty_mask = 1 if self._has_prev else 0
                self._next_slow = False
                self._next_fast_passes = 1
                # A gray frame's second plane isn't red: drive all of the next one.
                self._has_prev = not gray
                self._y_start = 0
                self._y_end = display_height
                # Consume before awaiting hardware so a newer request isn't lost.
                self._framebuffer_ready.clear()
                t0 = time.ticks_ms()
                has_red = _split_and_reverse_viper(
                    self._framebuffer_black,
                    self._framebuffer_red,
                    self._prev_black,
                    self._prev_red,
                    black_left,
                    black_right,
                    red_left,
                    red_right,
                    _REV_LUT,
                    do_rev,
                    use_dirty_mask,
                    y_start,
                    y_end,
                )
                print("Viper split & reverse ms:", time.ticks_diff(time.ticks_ms(), t0))

                leds.clear()
                self._refreshing_hw = True
                _blink_task = aio.create_task(self._blink_red_while_refreshing())
                try:
                    async with self.lock:
                        if mixed:
                            from lib.epd_grayscale import mixed_update
                            await mixed_update(self, black_left, red_left, black_right, red_right)
                        elif grayscale:
                            await self.global_update(
                                black_left, red_left, black_right, red_right, grayscale=True)
                        else:
                            await self.global_update(
                                black_left,
                                red_left,
                                black_right,
                                red_right,
                                already_reversed=True,
                                has_red=bool(has_red),
                                slow=slow,
                                fast_passes=fast_passes,
                            )
                except BaseException:
                    self._panel_monochrome = False
                    self._has_prev = False
                    raise
                finally:
                    self._refreshing_hw = False
                    await aio.sleep_ms(50)

                leds[0] = (0, 40, 0)
                leds.write()
                await aio.sleep_ms(400)
                leds.clear()

                gc.collect()
                if not self._framebuffer_ready.is_set():
                    self._display_updated.set()
            await aio.sleep_ms(50)

    async def global_update(
        self,
        black_left: bytearray | bytes | memoryview[int],
        red_left: bytearray | bytes | memoryview[int],
        black_right: bytearray | bytes | memoryview[int],
        red_right: bytearray | bytes | memoryview[int],
        grayscale=False,
        already_reversed: bool = False,
        has_red=None,
        slow: bool = True,
        fast_passes: int = 1,
    ):
        """Show prepared half-frame planes.

        Planes alone get the factory (OTP) refresh, as epd_grayscale expects.
        run() asks for the fast LUT refresh with slow=False, passing planes it
        already reversed and whether any changed pixel is red.
        """
        if grayscale:
            self._check_modes(True, False)
            from lib.epd_grayscale import grayscale_update
            await grayscale_update(self, black_left, red_left, black_right, red_right)
            return
        if has_red is None:
            has_red = any(red_left) or any(red_right)
        self._panel_monochrome = False
        print("Updating display to new image")
        started = time.ticks_ms()
        passes = 1 if slow else fast_passes
        for index in range(passes):
            if not slow:
                print("Fast redraw", index + 1, "of", passes, "color" if has_red else "BW")
            # Restart for each complete fast cycle. A third refresh in one
            # powered session took ~36 s on this badge during measurement.
            await self._reset()
            self._send_images(
                black_left, red_left, black_right, red_right,
                already_reversed=already_reversed,
            )
            await self.cog_initial(None if slow else self._fast_lut(has_red))
            await self._dcdc_soft_start()
            await self._display_refresh()
            if not slow and self._reverse_fb_bits and has_red:
                # Red still needs the second color waveform. Monochrome cycles
                # stop after the balanced fast waveform, without an extra finish.
                await self._wait_ready()
                self._send_index_data(0xB0, _LUTB_CLEANUP_RED)
                await self._display_refresh()
            await self._dcdc_soft_shutdown()
        print("Done updating display")
        print("Refresh total ms", time.ticks_diff(time.ticks_ms(), started))
        # Only a full factory refresh of a frame without red leaves no red pigment.
        self._panel_monochrome = slow and not has_red

    async def cog_initial(self, lut=None):
        """Power up the CoG with its factory (OTP) waveform, or `lut` (0xB0)."""
        self._send_index_data(0x05, [0x7D])
        await aio.sleep_ms(200)
        self._send_index_data(0x05, [0x00])
        await aio.sleep_ms(10)
        self._send_index_data(0xD8, [0x80])  # MS_SYNC
        self._send_index_data(0xD6, [0x00])  # BVSS
        self._send_index_data(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data(0x03, [0x00, 0x11])  # OSC

        # Display 1 & Display 2 OTP load (both halves in parallel)
        self._send_index_data(0x44, [0x00])
        self._send_index_data(0x45, [0x80])
        self._send_index_data(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data(0xA7, [0x00])
        await aio.sleep_ms(100)
        self._send_index_data(0x44, [0x06])
        self._send_index_data(0x45, [0x82])  # Temperature 0x82@25C
        self._send_index_data(0xA7, [0x10])
        await aio.sleep_ms(100)
        self._send_index_data(0xA7, [0x00])
        await aio.sleep_ms(100)

        self._send_index_data(0x60, [0x25])  # TCON
        self._send_index_data_left(0x61, [0x01])  # STV_DIR for Display 1
        self._send_index_data(0x02, [0x00])  # VCOM

        if lut is not None:
            self._send_index_data(0xB0, lut)

    def _fast_lut(self, has_red):
        if self._reverse_fb_bits:
            return _LUTB_PASS1_RED if has_red else _LUTB_PASS1_NORED
        return _LUT8_RED if has_red else _LUT8_NORED

    async def cog_poweroff(self):
        """CoG shutdown function
        Shuts down the CoG and DC/DC circuit after all update functions
        """
        self._send_index_data(0x09, [0x7F, 0x7D, 0x00])
        await aio.sleep_ms(200)
        await self._wait_ready(1)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        await aio.sleep_ms(150)
        EPD_RST_PIN.value(False)

    def _send_index_data(self, index: int, data: bytes | bytearray | list):
        if isinstance(data, list):
            data = bytes(data)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes([index]))
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(False)
        self.spi.write(data)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)

    def _send_index_data_left(self, index: int, data: bytes | bytearray | list | memoryview[int]):
        if isinstance(data, list):
            data = bytes(data)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_CS2_PIN.value(True)
        self.spi.write(bytes([index]))
        EPD_CS1_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS1_PIN.value(False)
        self.spi.write(data)
        EPD_CS1_PIN.value(True)

    def _send_index_data_right(self, index: int, data: bytes | bytearray | list | memoryview[int]):
        if isinstance(data, list):
            data = bytes(data)
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(False)
        self.spi.write(bytes([index]))
        EPD_CS2_PIN.value(True)
        EPD_DC_PIN.value(True)
        EPD_CS2_PIN.value(False)
        self.spi.write(data)
        EPD_CS2_PIN.value(True)

    async def _wait_ready(self, poll_ms=20):
        """Wait until the CoGs finish the current step.

        Like upstream (e351eae), move on once either half is ready: one BUSY
        line can stay low on some badges, which hung repeated refreshes. Where
        both lines work, give the other half a moment too, so both finish a
        waveform before the next command or power-down. The first time it
        misses that grace, stop waiting for it.
        """
        while not EPD_BUSY1_PIN.value() and not EPD_BUSY2_PIN.value():
            await aio.sleep_ms(poll_ms)
        if not self._busy_both:
            return
        deadline = time.ticks_add(time.ticks_ms(), _BUSY_GRACE_MS)
        while not (EPD_BUSY1_PIN.value() and EPD_BUSY2_PIN.value()):
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                self._busy_both = False
                print("EPD: a BUSY line stays low, waiting for either half from now on")
                return
            await aio.sleep_ms(5)

    async def _display_refresh(self):
        await self._wait_ready()
        self._send_index_data(0x15, [0x3C])
        await aio.sleep_ms(5)

    async def _reset(self):
        """CoG driver power-on hard reset"""
        await aio.sleep_ms(50)
        EPD_RST_PIN.value(True)
        await aio.sleep_ms(20)
        EPD_RST_PIN.value(False)
        await aio.sleep_ms(100)
        EPD_RST_PIN.value(True)
        await aio.sleep_ms(100)
        EPD_CS1_PIN.value(True)
        EPD_CS2_PIN.value(True)
        await aio.sleep_ms(5)
        self._send_index_data(0x70, [0x71, 0x36])  # B1 booster config, itc97; Errata 9

    def _send_images(
        self,
        black_left: bytearray | bytes | memoryview[int],
        red_left: bytearray | bytes | memoryview[int],
        black_right: bytearray | bytes | memoryview[int],
        red_right: bytearray | bytes | memoryview[int],
        already_reversed: bool = False,
    ):
        assert len(black_left) == half_frame_one_color_size
        assert len(red_left) == half_frame_one_color_size
        assert len(black_right) == half_frame_one_color_size
        assert len(red_right) == half_frame_one_color_size

        if self._reverse_fb_bits and not already_reversed:
            black_left = bytes(_REV_LUT[b] for b in black_left)
            red_left = bytes(_REV_LUT[b] for b in red_left)
            black_right = bytes(_REV_LUT[b] for b in black_right)
            red_right = bytes(_REV_LUT[b] for b in red_right)

        # DUW for both left and right halves
        self._send_index_data(0x13, [0x00, 0x3B, 0x00, 0x00, 0x9F, 0x02])
        # DRFW for Both left and right halves
        self._send_index_data(0x90, [0x00, 0x3B, 0x00, 0xA9])
        ram_rw_data = [0x3B, 0x00, 0x14]
        self._send_index_data_left(0x12, ram_rw_data)  # RAM_RW for left half
        self._send_index_data_left(0x10, black_left)  # Black frame for left half
        self._send_index_data_left(0x12, ram_rw_data)  # RAM_RW for left half
        self._send_index_data_left(0x11, red_left)  # Red frame for left half
        self._send_index_data_right(0x12, ram_rw_data)  # RAM_RW for right half
        self._send_index_data_right(0x10, black_right)  # black frame for right half
        self._send_index_data_right(0x12, ram_rw_data)  # RAM_RW for right half
        self._send_index_data_right(0x11, red_right)  # Red frame for right half

    async def _dcdc_soft_start(self):
        """DC-DC soft-start command
        Implemented after image data are uploaded to CoG
        Specific to mid-sized EPDs only
        """
        self._send_index_data(0x51, [0x50, 0x01])

        for val in range(1, 5):
            self._send_index_data(0x09, [0x1F])
            self._send_index_data(0x51, [0x50, val])
            self._send_index_data(0x09, [0x9F])
            await aio.sleep_ms(2)

        for val in range(1, 11):
            self._send_index_data(0x09, [0x1F])
            self._send_index_data(0x51, [0x0A, val])
            self._send_index_data(0x09, [0x9F])
            await aio.sleep_ms(2)

        for val in range(3, 11):
            self._send_index_data(0x09, [0x7F])
            self._send_index_data(0x51, [0x0A, val])
            self._send_index_data(0x09, [0xFF])
            await aio.sleep_ms(2)

        for val in range(9, 1, -1):
            self._send_index_data(0x09, [0x7F])
            self._send_index_data(0x51, [val, 0x0A])
            self._send_index_data(0x09, [0xFF])
            await aio.sleep_ms(2)

        self._send_index_data(0x09, [0xFF])
        await aio.sleep_ms(10)

    async def _dcdc_soft_shutdown(self):
        """DC-DC soft-shutdown command
        Implemented after image data are uploaded to CoG
        Specific to mid-sized EPDs only
        """
        await self._wait_ready()
        self._send_index_data(0x09, [0x7F])
        self._send_index_data(0x05, [0x7D])
        self._send_index_data(0x09, [0x00])
        await aio.sleep_ms(100)
        await self._wait_ready()
        EPD_DC_PIN.value(False)
        EPD_CS1_PIN.value(False)
        EPD_RST_PIN.value(False)
        EPD_CS1_PIN.value(True)


display = EPaperDisplay()
