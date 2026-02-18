"""Generate the JJP Asset Decryptor icon (jjp_decryptor/icon.ico).

Pure Python — no PIL/Pillow dependency. Creates a multi-size ICO file
with PNG-encoded images. Design: open padlock on a gradient background.
"""

import math
import struct
import zlib


def create_png(width, height, rgba_data):
    """Create a PNG file from raw RGBA byte data."""
    def chunk(ctype, data):
        c = ctype + data
        crc = struct.pack('>I', zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack('>I', len(data)) + c + crc

    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    raw = b''
    for y in range(height):
        raw += b'\x00'
        raw += rgba_data[y * width * 4:(y + 1) * width * 4]
    return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def lerp(a, b, t):
    return a + (b - a) * max(0.0, min(1.0, t))


def clamp(v, lo=0, hi=255):
    return max(lo, min(hi, int(v)))


def blend(bg, fg, alpha):
    """Blend fg over bg with given alpha (0-1)."""
    return clamp(bg * (1 - alpha) + fg * alpha)


def sdf_rounded_rect(x, y, cx, cy, hw, hh, radius):
    """Signed distance to a rounded rectangle. Negative = inside."""
    dx = abs(x - cx) - hw + radius
    dy = abs(y - cy) - hh + radius
    outside = math.sqrt(max(dx, 0) ** 2 + max(dy, 0) ** 2) - radius
    inside = min(max(dx, dy), 0)
    return outside + inside


# Bitmap font for J and P (5 wide x 7 tall, 1 = filled)
_FONT = {
    'J': [
        "00111",
        "00010",
        "00010",
        "00010",
        "10010",
        "10010",
        "01100",
    ],
    'P': [
        "11110",
        "10001",
        "10001",
        "11110",
        "10000",
        "10000",
        "10000",
    ],
}


def _jjp_label(px, py, body_cx, body_cy, s):
    """Return alpha (0-1) for 'JJP' text centered on the padlock body."""
    # Total label size: 3 chars * 5 wide + 2 gaps = 17 columns, 7 rows
    char_w, char_h, gap = 5, 7, 1
    total_w = char_w * 3 + gap * 2  # 17
    cell = s * 0.018  # size of each pixel cell, scales with icon size
    label_w = total_w * cell
    label_h = char_h * cell
    label_x0 = body_cx - label_w / 2
    label_y0 = body_cy - label_h / 2

    # Check if pixel is within label bounds
    lx = (px - label_x0) / cell
    ly = (py - label_y0) / cell
    if lx < 0 or lx >= total_w or ly < 0 or ly >= char_h:
        return 0.0

    # Determine which character and which cell within it
    col = int(lx)
    row = int(ly)
    if row < 0 or row >= char_h:
        return 0.0

    # Map column to character index and local column
    if col < char_w:
        char, local_col = 'J', col
    elif col < char_w + gap:
        return 0.0  # gap
    elif col < char_w * 2 + gap:
        char, local_col = 'J', col - char_w - gap
    elif col < char_w * 2 + gap * 2:
        return 0.0  # gap
    elif col < total_w:
        char, local_col = 'P', col - char_w * 2 - gap * 2
    else:
        return 0.0

    # Look up the bitmap
    if _FONT[char][row][local_col] == '1':
        # Smooth edges using sub-pixel distance to cell center
        cx_cell = int(lx) + 0.5
        cy_cell = int(ly) + 0.5
        dx = abs(lx - cx_cell)
        dy = abs(ly - cy_cell)
        edge = max(dx, dy)
        return max(0.0, min(1.0, (0.5 - edge) * 3 + 0.5))
    return 0.0


def render_icon(size):
    """Render a decryptor-themed icon at the given size."""
    pixels = bytearray(size * size * 4)
    s = size  # shorthand
    cx, cy = s / 2, s / 2
    aa = 1.2  # anti-aliasing width in pixels

    for y in range(size):
        for x in range(size):
            off = (y * size + x) * 4
            px, py = x + 0.5, y + 0.5  # pixel center

            # --- Background: rounded rectangle with gradient ---
            bg_dist = sdf_rounded_rect(px, py, cx, cy, s * 0.44, s * 0.44, s * 0.15)
            bg_alpha = max(0.0, min(1.0, 0.5 - bg_dist / aa))

            if bg_alpha <= 0:
                pixels[off:off + 4] = bytes([0, 0, 0, 0])
                continue

            # Gradient: deep teal to dark blue
            t = py / s
            bg_r = lerp(15, 20, t)
            bg_g = lerp(45, 25, t)
            bg_b = lerp(75, 55, t)

            # Subtle radial vignette
            vd = math.sqrt((px - cx) ** 2 + (py - cy) ** 2) / (s * 0.5)
            vignette = 1.0 - vd * 0.25
            bg_r *= vignette
            bg_g *= vignette
            bg_b *= vignette

            r, g, b = bg_r, bg_g, bg_b

            # --- Padlock body ---
            body_cx = cx
            body_cy = cy + s * 0.1
            body_hw = s * 0.19
            body_hh = s * 0.16
            body_r = s * 0.05
            body_dist = sdf_rounded_rect(px, py, body_cx, body_cy, body_hw, body_hh, body_r)
            body_alpha = max(0.0, min(1.0, 0.5 - body_dist / aa))

            if body_alpha > 0:
                # Gold/amber metallic gradient
                bt = (py - (body_cy - body_hh)) / (body_hh * 2)
                lr = lerp(220, 180, bt)
                lg = lerp(175, 130, bt)
                lb = lerp(50, 30, bt)

                # Slight horizontal shading for 3D effect
                bx_t = (px - (body_cx - body_hw)) / (body_hw * 2)
                shade = 0.8 + 0.4 * (0.5 - abs(bx_t - 0.5))
                lr *= shade
                lg *= shade
                lb *= shade

                # Subtle top edge highlight
                if bt < 0.15:
                    hl = 1.0 - bt / 0.15
                    lr = lerp(lr, 255, hl * 0.3)
                    lg = lerp(lg, 230, hl * 0.3)
                    lb = lerp(lb, 150, hl * 0.3)

                r = blend(r, lr, body_alpha)
                g = blend(g, lg, body_alpha)
                b = blend(b, lb, body_alpha)

            # --- "JJP" label on padlock body ---
            if body_alpha > 0.3:
                label_alpha = _jjp_label(px, py, body_cx, body_cy, s)
                if label_alpha > 0:
                    # Dark engraved text
                    r = blend(r, 40, label_alpha * 0.8)
                    g = blend(g, 30, label_alpha * 0.8)
                    b = blend(b, 10, label_alpha * 0.8)

            # --- Shackle (unlocked - lifted up and shifted right) ---
            shackle_cx = cx + s * 0.15  # shifted right significantly
            shackle_top = cy - s * 0.34  # lifted up above the body
            shackle_bot = body_cy - body_hh - s * 0.02  # ends above body top
            shackle_outer_r = s * 0.135
            shackle_inner_r = s * 0.075
            shackle_thickness = shackle_outer_r - shackle_inner_r

            # Only draw the right bar (left bar is "inside" the body hole)
            in_shackle = False
            shackle_t = 0.5  # for shading

            # Right bar only (the visible lifted part)
            right_x = shackle_cx + shackle_outer_r - shackle_thickness / 2
            if (abs(px - right_x) < shackle_thickness / 2 and
                    shackle_top + shackle_outer_r * 0.3 < py < shackle_bot):
                in_shackle = True
                shackle_t = (px - right_x + shackle_thickness / 2) / shackle_thickness

            # Left bar (shorter, goes into the body)
            left_x = shackle_cx - shackle_outer_r + shackle_thickness / 2
            left_bot = body_cy - body_hh + s * 0.05  # extends into body
            if (abs(px - left_x) < shackle_thickness / 2 and
                    shackle_top + shackle_outer_r * 0.3 < py < left_bot):
                in_shackle = True
                shackle_t = (px - left_x + shackle_thickness / 2) / shackle_thickness

            # Semicircle top
            sc_cy = shackle_top + shackle_outer_r
            sc_dist = math.sqrt((px - shackle_cx) ** 2 + (py - sc_cy) ** 2)
            if (py <= sc_cy and
                    shackle_inner_r <= sc_dist <= shackle_outer_r):
                in_shackle = True
                shackle_t = (sc_dist - shackle_inner_r) / shackle_thickness

            if in_shackle:
                # SDF for anti-aliased shackle
                if py <= sc_cy:
                    # Arc region
                    d_outer = sc_dist - shackle_outer_r
                    d_inner = shackle_inner_r - sc_dist
                    dist = max(d_outer, d_inner)
                else:
                    # Bar region
                    if abs(px - left_x) < abs(px - right_x):
                        dist = abs(px - left_x) - shackle_thickness / 2
                    else:
                        dist = abs(px - right_x) - shackle_thickness / 2

                s_alpha = max(0.0, min(1.0, 0.5 - dist / aa))

                if s_alpha > 0:
                    # Steel/silver metallic
                    shade = 0.7 + 0.6 * (0.5 - abs(shackle_t - 0.5))
                    sr = clamp(170 * shade)
                    sg = clamp(180 * shade)
                    sb = clamp(195 * shade)

                    # Top highlight on the arc
                    if py < sc_cy - shackle_outer_r * 0.5:
                        hl = 1.0 - (py - shackle_top) / (shackle_outer_r * 0.5)
                        sr = clamp(lerp(sr, 240, hl * 0.4))
                        sg = clamp(lerp(sg, 245, hl * 0.4))
                        sb = clamp(lerp(sb, 255, hl * 0.4))

                    r = blend(r, sr, s_alpha)
                    g = blend(g, sg, s_alpha)
                    b = blend(b, sb, s_alpha)

            # Finalize
            final_a = clamp(bg_alpha * 255)
            pixels[off:off + 4] = bytes([clamp(r), clamp(g), clamp(b), final_a])

    return bytes(pixels)


def create_ico(filename, sizes=(16, 32, 48, 64, 256)):
    """Create a multi-size ICO file."""
    images = []
    for size in sizes:
        print(f"  Rendering {size}x{size}...")
        rgba = render_icon(size)
        png_data = create_png(size, size, rgba)
        images.append((size, png_data))

    header = struct.pack('<HHH', 0, 1, len(images))
    data_offset = 6 + 16 * len(images)
    directory = b''
    for size, png_data in images:
        w = size if size < 256 else 0
        h = size if size < 256 else 0
        directory += struct.pack('<BBBBHHII', w, h, 0, 0, 1, 32, len(png_data), data_offset)
        data_offset += len(png_data)

    with open(filename, 'wb') as f:
        f.write(header)
        f.write(directory)
        for _, png_data in images:
            f.write(png_data)

    print(f"  Icon saved to: {filename}")


if __name__ == "__main__":
    import os
    icon_path = os.path.join(os.path.dirname(__file__), "jjp_decryptor", "icon.ico")
    print("Generating JJP Asset Decryptor icon...")
    create_ico(icon_path)
    print("Done!")
