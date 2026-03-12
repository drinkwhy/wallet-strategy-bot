from PIL import Image, ImageDraw
import math, os

os.makedirs("static", exist_ok=True)

def draw_icon(size, bg, fg, mono=False):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background circle
    pad = size * 0.04
    d.ellipse([pad, pad, size - pad, size - pad], fill=bg)

    # Solana-style lightning bolt / S shape
    cx, cy = size / 2, size / 2
    s = size * 0.28

    # Three parallel diagonal bars (Solana-inspired)
    bar_h = s * 0.22
    gap = s * 0.32
    angle = -25  # degrees

    for i, offset in enumerate([-gap, 0, gap]):
        y = cy + offset
        # Bar as polygon (parallelogram)
        rad = math.radians(angle)
        dx = bar_h * math.tan(abs(rad))
        pts = [
            (cx - s + (dx if i % 2 == 0 else 0), y - bar_h / 2),
            (cx + s + (dx if i % 2 == 0 else 0), y - bar_h / 2),
            (cx + s - (dx if i % 2 == 0 else 0), y + bar_h / 2),
            (cx - s - (dx if i % 2 == 0 else 0), y + bar_h / 2),
        ]
        d.polygon(pts, fill=fg)

    return img

# Color icon (512x512)
icon_color = draw_icon(512, (10, 10, 26, 255), (20, 200, 120, 255))
icon_color.save("static/icon-512.png")

# Smaller sizes
for sz in [192, 96, 48]:
    icon_color.resize((sz, sz), Image.LANCZOS).save(f"static/icon-{sz}.png")

# Monochrome icon (white on black)
icon_mono = draw_icon(512, (0, 0, 0, 255), (255, 255, 255, 255), mono=True)
icon_mono.save("static/icon-mono.png")

print("Icons generated: static/icon-512.png, icon-192.png, icon-96.png, icon-48.png, icon-mono.png")
