"""Генерация минималистичных аватарок для бота (512x512 PNG).

Запуск: python tools/generate_avatar.py
Результат: assets/avatar-*.png — загрузить через @BotFather /setuserpic.

Дизайн рассчитан на то, что Telegram обрежет картинку в круг и покажет
её размером с ноготь: только крупные формы, высокая контрастность,
ничего важного ближе 8% к краю.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 512
SCALE = 4
N = SIZE * SCALE
C = N // 2  # центр

BG = (10, 17, 30, 255)
CYAN = (34, 211, 238, 255)
CYAN_SOFT = (125, 240, 255, 255)
LINE = (28, 52, 88, 255)

OUT = Path(__file__).resolve().parent.parent / "assets"


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def _circle(draw: ImageDraw.ImageDraw, radius: int, color, width: int) -> None:
    draw.ellipse([C - radius, C - radius, C + radius, C + radius], outline=color, width=width)


def _point(angle_deg: float, radius: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    return C + radius * math.cos(angle), C + radius * math.sin(angle)


def _wedge(draw: ImageDraw.ImageDraw, radius: int, start: float, end: float, fill) -> None:
    points = [(C, C)]
    steps = 48
    for step in range(steps + 1):
        points.append(_point(start + (end - start) * step / steps, radius))
    draw.polygon(points, fill=fill)


def _save(image: Image.Image, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"avatar-{name}.png"
    image.convert("RGB").save(path, "PNG", optimize=True)
    return path


def radar() -> Image.Image:
    """Радар: кольцо, луч, отметка цели. Совпадает с названием бота."""
    image = Image.new("RGBA", (N, N), BG)
    outer = int(N * 0.36)

    sweep = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    sweep_draw = ImageDraw.Draw(sweep)
    _wedge(sweep_draw, outer, -95, -20, (34, 211, 238, 28))
    _wedge(sweep_draw, outer, -65, -20, (34, 211, 238, 55))
    image = Image.alpha_composite(image, sweep)

    draw = ImageDraw.Draw(image)
    _circle(draw, int(outer * 0.55), LINE, int(N * 0.012))
    _circle(draw, outer, CYAN, int(N * 0.022))

    # Луч и точка на нём.
    end = _point(-30, outer * 0.82)
    draw.line([(C, C), end], fill=CYAN_SOFT, width=int(N * 0.014))
    dot = int(N * 0.05)
    draw.ellipse([end[0] - dot, end[1] - dot, end[0] + dot, end[1] + dot], fill=CYAN_SOFT)
    draw.ellipse([C - dot, C - dot, C + dot, C + dot], fill=CYAN)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def percent() -> Image.Image:
    """Крупное «-100%»: читается даже в 40 пикселей."""
    image = Image.new("RGBA", (N, N), BG)
    draw = ImageDraw.Draw(image)
    _circle(draw, int(N * 0.40), (34, 211, 238, 60), int(N * 0.014))
    font = _font(int(N * 0.27))
    draw.text((C, C), "-100%", font=font, fill=CYAN, anchor="mm")
    return image.resize((SIZE, SIZE), Image.LANCZOS)


def gift() -> Image.Image:
    """Подарок: крышка, коробка, лента, бант."""
    image = Image.new("RGBA", (N, N), BG)
    draw = ImageDraw.Draw(image)
    line = int(N * 0.024)
    stroke = int(N * 0.022)

    body_w, body_top, body_bottom = int(N * 0.42), C + int(N * 0.02), C + int(N * 0.30)
    lid_w, lid_top, lid_bottom = int(N * 0.50), C - int(N * 0.12), C + int(N * 0.00)

    draw.rounded_rectangle(
        [C - body_w // 2, body_top, C + body_w // 2, body_bottom],
        radius=int(N * 0.02), outline=CYAN, width=line,
    )
    draw.rounded_rectangle(
        [C - lid_w // 2, lid_top, C + lid_w // 2, lid_bottom],
        radius=int(N * 0.02), outline=CYAN, width=line,
    )

    # Лента поверх контуров — иначе она «рвётся» на границе крышки.
    draw.rectangle([C - stroke, lid_top - stroke, C + stroke, body_bottom], fill=CYAN_SOFT)

    # Бант: два кольца и узел над крышкой.
    bow_r = int(N * 0.078)
    bow_cy = lid_top - int(bow_r * 0.55)
    for direction in (-1, 1):
        cx = C + direction * bow_r
        draw.ellipse(
            [cx - bow_r, bow_cy - int(bow_r * 0.62), cx + bow_r, bow_cy + int(bow_r * 0.62)],
            outline=CYAN_SOFT, width=line,
        )
    knot = int(N * 0.024)
    draw.ellipse([C - knot, bow_cy - knot, C + knot, bow_cy + knot], fill=CYAN_SOFT)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    for name, image in {"radar": radar(), "percent": percent(), "gift": gift()}.items():
        path = _save(image, name)
        print(f"{path}  {image.size[0]}x{image.size[1]}")


if __name__ == "__main__":
    main()
