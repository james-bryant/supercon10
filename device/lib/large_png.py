import io
import gc

import deflate
import micropython
from micropython import const

SCREEN_WIDTH = const(960)
SCREEN_HEIGHT = const(672)
WHITE = const(0)
BLACK = const(1)
RED = const(2)
GRAY = const(3)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

"""
Draw a PNG either from a `path` or a byte array in `data`
"""
def show_png(path: str | None, black_framebuffer: bytearray, red_framebuffer: bytearray, row: int = 0, col: int = 0, grayscale: bool = False, mixed: bool = False, data: bytes = None):
    """Draw a PNG; mixed keeps red and marks gray in both software planes."""
    image_width, image_height, kinds, buf, bit_depth = _read_palette_png(
        path, data, grayscale, mixed
    )
    _blit_all(
        buf,
        kinds,
        bit_depth,
        image_width,
        image_height,
        row,
        col,
        black_framebuffer,
        red_framebuffer,
    )


@micropython.viper
def _color_kind(red_value: int, green_value: int, blue_value: int) -> int:
    if red_value >= 128 and green_value < 128 and blue_value < 128:
        return RED
    if red_value < 128 and green_value < 128 and blue_value < 128:
        return BLACK
    return WHITE


@micropython.viper
def _unfilter_all(buf, height: int, row_bytes: int):
    data = ptr8(buf)
    stride = row_bytes + 1
    for y in range(height):
        row_off = y * stride
        ft = data[row_off]
        if ft == 0:
            continue
        if ft < 1 or ft > 4:
            raise ValueError("unreadable png filter")
        p_off = row_off - stride
        for i in range(1, stride):
            left = data[row_off + i - 1] if i > 1 else 0
            up = data[p_off + i] if y > 0 else 0
            up_left = data[p_off + i - 1] if (y > 0 and i > 1) else 0
            extra = 0
            if ft == 1:
                extra = left
            elif ft == 2:
                extra = up
            elif ft == 3:
                extra = (left + up) >> 1
            else:
                est = left + up - up_left
                dl = est - left
                if dl < 0:
                    dl = 0 - dl
                du = est - up
                if du < 0:
                    du = 0 - du
                dul = est - up_left
                if dul < 0:
                    dul = 0 - dul
                if dl <= du and dl <= dul:
                    extra = left
                elif du <= dul:
                    extra = up
                else:
                    extra = up_left
            data[row_off + i] = (data[row_off + i] + extra) & 255


@micropython.viper
def _blit_all(
    buf,
    kinds,
    bit_depth: int,
    img_w: int,
    img_h: int,
    row0: int,
    col0: int,
    black,
    red,
):
    data = ptr8(buf)
    kind = ptr8(kinds)
    black_buf = ptr8(black)
    red_buf = ptr8(red)
    row_bytes = (img_w * bit_depth + 7) >> 3
    stride = row_bytes + 1
    start = 0 - col0 if col0 < 0 else 0
    end = SCREEN_WIDTH - col0
    if end > img_w:
        end = img_w
    if start >= end:
        return
    mask = (1 << bit_depth) - 1
    if col0 == 0 and start == 0 and (end & 7) == 0 and bit_depth == 2:
        n_bytes = end >> 3
        for y in range(img_h):
            sy = row0 + y
            if sy < 0 or sy >= SCREEN_HEIGHT:
                continue
            r_off = y * stride + 1
            out_off = sy * 120
            for bx in range(n_bytes):
                b0 = data[r_off + (bx << 1)]
                b1 = data[r_off + (bx << 1) + 1]
                k0 = kind[(b0 >> 6) & 3]
                k1 = kind[(b0 >> 4) & 3]
                k2 = kind[(b0 >> 2) & 3]
                k3 = kind[b0 & 3]
                k4 = kind[(b1 >> 6) & 3]
                k5 = kind[(b1 >> 4) & 3]
                k6 = kind[(b1 >> 2) & 3]
                k7 = kind[b1 & 3]
                # A kind is its plane bits: 1 black, 2 red, GRAY (3) sets both.
                blk = (
                    ((k0 & 1) << 7)
                    | ((k1 & 1) << 6)
                    | ((k2 & 1) << 5)
                    | ((k3 & 1) << 4)
                    | ((k4 & 1) << 3)
                    | ((k5 & 1) << 2)
                    | ((k6 & 1) << 1)
                    | (k7 & 1)
                )
                rd = (
                    ((k0 & 2) << 6)
                    | ((k1 & 2) << 5)
                    | ((k2 & 2) << 4)
                    | ((k3 & 2) << 3)
                    | ((k4 & 2) << 2)
                    | ((k5 & 2) << 1)
                    | (k6 & 2)
                    | ((k7 & 2) >> 1)
                )
                black_buf[out_off + bx] = blk
                red_buf[out_off + bx] = rd
        return
    if col0 == 0 and start == 0 and (end & 7) == 0 and bit_depth == 1:
        n_bytes = end >> 3
        k0 = kind[0]
        k1 = kind[1]
        for y in range(img_h):
            sy = row0 + y
            if sy < 0 or sy >= SCREEN_HEIGHT:
                continue
            r_off = y * stride + 1
            out_off = sy * 120
            for bx in range(n_bytes):
                b0 = data[r_off + bx]
                inv = b0 ^ 255
                blk = 0
                rd = 0
                if k0 & 1:
                    blk |= inv
                if k0 & 2:
                    rd |= inv
                if k1 & 1:
                    blk |= b0
                if k1 & 2:
                    rd |= b0
                black_buf[out_off + bx] = blk
                red_buf[out_off + bx] = rd
        return
    for y in range(img_h):
        sy = row0 + y
        if sy < 0 or sy >= SCREEN_HEIGHT:
            continue
        r_off = y * stride + 1
        y_base = sy * SCREEN_WIDTH
        for x in range(start, end):
            if bit_depth == 8:
                sample = data[r_off + x]
            else:
                bi = x * bit_depth
                sh = 8 - bit_depth - (bi & 7)
                sample = (data[r_off + (bi >> 3)] >> sh) & mask
            pixel = kind[sample]
            i = y_base + col0 + x
            byte_idx = i >> 3
            bit = 128 >> (i & 7)
            cleared = 255 ^ bit
            if pixel & 1:
                black_buf[byte_idx] |= bit
            else:
                black_buf[byte_idx] &= cleared
            if pixel & 2:
                red_buf[byte_idx] |= bit
            else:
                red_buf[byte_idx] &= cleared


def _read_palette_png(
    path: str = None, data: bytes = None, grayscale=False, mixed=False
):
    if grayscale and mixed:
        raise ValueError("Choose grayscale or mixed mode")
    if grayscale or mixed:
        from lib.epd_grayscale import grayscale_kind, mixed_kind
    if path:
        with open(path, "rb") as image_file:
            data = image_file.read()
    elif data:
        pass
    else:
        raise ValueError("missing one of: path, data")
    if data[:8] != _PNG_SIGNATURE:
        raise ValueError("not a png: " + path)

    palette = None
    compressed = bytearray()
    image_width = image_height = bit_depth = color_type = interlaced = 0
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        name = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if name == b"IHDR":
            image_width = int.from_bytes(chunk[0:4], "big")
            image_height = int.from_bytes(chunk[4:8], "big")
            bit_depth = chunk[8]
            color_type = chunk[9]
            interlaced = chunk[12]
        elif name == b"PLTE":
            palette = [
                (chunk[i], chunk[i + 1], chunk[i + 2])
                for i in range(0, len(chunk), 3)
            ]
        elif name == b"IDAT":
            compressed.extend(chunk)
        elif name == b"IEND":
            break

    if interlaced:
        raise ValueError("turn off interlacing when saving the png")
    if color_type == 0 and bit_depth in (1, 2, 4, 8):
        kinds = bytearray(1 << bit_depth)
        if grayscale or mixed:
            maximum = (1 << bit_depth) - 1
            for sample in range(len(kinds)):
                kind = grayscale_kind(sample * 255 // maximum)
                kinds[sample] = GRAY if mixed and kind == RED else kind
        else:
            # Preserve the original B/W mapping: only sample 0 is black.
            kinds[0] = BLACK
    elif color_type != 3 or palette is None:
        raise ValueError("save the png as a palette image")
    else:
        count = 1 << bit_depth if bit_depth in (1, 2, 4, 8) else len(palette)
        kinds = bytearray(count)
        for index, color in enumerate(palette):
            if mixed:
                kinds[index] = mixed_kind(color[0], color[1], color[2])
            else:
                kinds[index] = (grayscale_kind(color[1]) if grayscale
                                else _color_kind(color[0], color[1], color[2]))

    data = None
    gc.collect()
    row_bytes = (image_width * bit_depth + 7) // 8
    buf = bytearray((row_bytes + 1) * image_height)
    stream = deflate.DeflateIO(io.BytesIO(compressed), deflate.ZLIB)
    try:
        stream.readinto(buf)
    finally:
        stream.close()
    compressed = None
    _unfilter_all(buf, image_height, row_bytes)
    return image_width, image_height, kinds, buf, bit_depth
