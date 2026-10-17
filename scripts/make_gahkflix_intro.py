"""Render GAHKFlix's intro, the few seconds that play before every film (app/static/gahkflix/intro.mp4).

    uv run python scripts/make_gahkflix_intro.py [--font PATH] [--out PATH]

The red GAHKFLIX wordmark lands on black, holds while a light sweeps across it, then the camera dives
into it, over a synthesised "ta-dum". Pillow draws the frames and ffmpeg (the imageio-ffmpeg binary
the app already uses) encodes them with the sound. It is a committed static file, so this only runs
when someone wants a different intro; replacing the MP4 with one a resident made works just as well,
as long as it stays a few seconds of H.264 + AAC with +faststart.
"""

import argparse
import math
import subprocess
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT, FPS, SECONDS = 1280, 720, 30, 4.0
RED = (229, 9, 20)
WORD = "GAHKFLIX"
FONTS = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# The sound, as an ffmpeg aevalsrc expression of t: a short thump ("ta"), a long low note ("dum")
# and a soft chord swelling under it, faded out before the end.
TA = "if(gte(t,0.30),(sin(2*PI*72*(t-0.30))*0.9+sin(2*PI*144*(t-0.30))*0.3)*exp(-(t-0.30)*22),0)"
DUM = (
    "if(gte(t,0.72),(sin(2*PI*49*(t-0.72))*0.9+sin(2*PI*98*(t-0.72))*0.45"
    "+sin(2*PI*147*(t-0.72))*0.2)*exp(-(t-0.72)*1.6),0)"
)
PAD = (
    "if(gte(t,0.72),(sin(2*PI*220*t)+0.8*sin(2*PI*277.18*t)+0.7*sin(2*PI*329.63*t))"
    "*0.06*(1-exp(-(t-0.72)*6))*exp(-(t-0.72)*0.9),0)"
)
# Quoted: the commas inside it would otherwise split the filter.
SOUND = f"aevalsrc='0.8*({TA}+{DUM}+{PAD})':s=48000:d={SECONDS}"


def ease_out(x: float) -> float:
    return 1 - (1 - min(max(x, 0.0), 1.0)) ** 3


def ease_in(x: float) -> float:
    return min(max(x, 0.0), 1.0) ** 3


def wordmark(font_path: str) -> Image.Image:
    """The word in red on transparency, large enough to zoom into without going soft."""
    font = ImageFont.truetype(font_path, 420)
    left, top, right, bottom = font.getbbox(WORD)
    image = Image.new("RGBA", (right - left + 80, bottom - top + 80), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((40 - left, 40 - top), WORD, font=font, fill=(*RED, 255))
    return image


def frame(word: Image.Image, glow: Image.Image, t: float) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    if t < 0.25 or t > 3.35:
        return canvas
    land = ease_out((t - 0.25) / 0.75)  # 0.25–1.0 s: lands, from big and transparent
    dive = ease_in((t - 2.6) / 0.75)  # 2.6–3.35 s: dives in and fades
    scale = (
        (1.7 - 0.7 * land)
        * (1 + 0.05 * min(max((t - 1.0) / 1.6, 0), 1))
        * (1 + 6 * dive)
    )
    opacity = land * (1 - dive)
    target_width = int(WIDTH * 0.62 * scale)
    size = (target_width, int(word.height * target_width / word.width))
    for layer, alpha in ((glow, 0.55), (word, 1.0)):
        scaled = layer.resize(size, Image.Resampling.LANCZOS)
        mask = scaled.getchannel("A").point(
            lambda a, alpha=alpha: int(a * opacity * alpha)
        )
        canvas.paste(
            scaled.convert("RGB"),
            ((WIDTH - size[0]) // 2, (HEIGHT - size[1]) // 2),
            mask,
        )
    # A light sweeping across the letters while they hold.
    sweep = (t - 1.25) / 0.9
    if 0 <= sweep <= 1 and dive == 0:
        band = Image.new("L", (WIDTH, HEIGHT), 0)
        x = int(-WIDTH * 0.3 + sweep * WIDTH * 1.6)
        ImageDraw.Draw(band).polygon(
            [(x, 0), (x + 120, 0), (x - 140, HEIGHT), (x - 260, HEIGHT)], fill=110
        )
        band = band.filter(ImageFilter.GaussianBlur(40))
        letters = word.resize(size, Image.Resampling.LANCZOS).getchannel("A")
        placed = Image.new("L", (WIDTH, HEIGHT), 0)
        placed.paste(letters, ((WIDTH - size[0]) // 2, (HEIGHT - size[1]) // 2))
        light = Image.new("RGB", (WIDTH, HEIGHT), (255, 220, 220))
        canvas.paste(
            light, (0, 0), Image.composite(band, Image.new("L", band.size, 0), placed)
        )
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--font", default=next((f for f in FONTS if Path(f).exists()), None)
    )
    parser.add_argument(
        "--out",
        default=str(
            Path(__file__).resolve().parent.parent / "app/static/gahkflix/intro.mp4"
        ),
    )
    args = parser.parse_args()
    if not args.font:
        parser.error("no bold font found; pass --font")
    word = wordmark(args.font)
    glow = word.filter(ImageFilter.GaussianBlur(28))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-v", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
            "-f", "lavfi", "-i", SOUND,
            "-af", f"afade=t=out:st={SECONDS - 0.7}:d=0.7,aformat=channel_layouts=stereo",
            "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out),
        ],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    assert encoder.stdin is not None
    for index in range(math.ceil(SECONDS * FPS)):
        encoder.stdin.write(frame(word, glow, index / FPS).tobytes())
    encoder.stdin.close()
    if encoder.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
