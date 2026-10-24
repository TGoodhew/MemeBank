"""Image download, normalising for Claude, and a perceptual hash to catch reposts."""
from __future__ import annotations

import io

import requests
from PIL import Image, ImageOps

MAX_EDGE = 1568  # Claude doesn't gain anything from larger images
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp", "BMP": "image/bmp"}
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp"}


class NotAnImage(Exception):
    pass


def fetch(url: str, bot_token: str | None = None, timeout: int = 30) -> bytes:
    headers = {"Authorization": f"Bearer {bot_token}"} if bot_token else {}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    if "text/html" in ctype:
        # Slack serves its sign-in page instead of the file when the token lacks files:read
        raise NotAnImage(f"Got a web page instead of an image from {url.split('?')[0]} (check the files:read scope)")
    return r.content


def sniff(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as im:
            return _MIME.get(im.format or "", "image/png")
    except Exception as e:  # noqa: BLE001
        raise NotAnImage(str(e)) from e


def for_claude(data: bytes) -> tuple[bytes, str]:
    """First frame, upright, at most MAX_EDGE px, as PNG or JPEG under the API's size limit."""
    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((MAX_EDGE, MAX_EDGE))
        out = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P"):
            im.convert("RGBA").save(out, "PNG", optimize=True)
            if out.tell() < 4_500_000:
                return out.getvalue(), "image/png"
            out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=88)
        return out.getvalue(), "image/jpeg"


def dhash(data: bytes, size: int = 8) -> str:
    with Image.open(io.BytesIO(data)) as im:
        im.seek(0)
        g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(g.tobytes())  # one byte per pixel in "L" mode
    bits = 0
    for row in range(size):
        for col in range(size):
            i = row * (size + 1) + col
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
