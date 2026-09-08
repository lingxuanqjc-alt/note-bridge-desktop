"""Render the project's own geometric mark; no third-party image assets."""
from pathlib import Path

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
directory = root / "assets"
directory.mkdir(exist_ok=True)
image = Image.new("RGBA", (256, 256))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((5, 5, 251, 251), radius=55, fill="#7664df")
draw.rounded_rectangle((40, 40, 158, 188), radius=21, fill="#d9d1ff")
draw.rounded_rectangle((84, 68, 211, 217), radius=21, fill="white")
for points in [[(108, 119), (185, 119)], [(164, 98), (185, 119), (164, 140)], [(185, 174), (108, 174)], [(129, 153), (108, 174), (129, 195)]]:
    draw.line(points, fill="#7664df", width=11, joint="curve")
image.save(directory / "note-bridge.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.save(directory / "note-bridge.png")
