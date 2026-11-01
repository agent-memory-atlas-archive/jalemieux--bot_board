"""Bounded raster images; never accept active formats such as SVG."""
import base64
import binascii
import io

from PIL import Image, UnidentifiedImageError

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
FORMATS = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"),
           "GIF": ("image/gif", "gif"), "WEBP": ("image/webp", "webp")}


def decode(value: str) -> tuple[bytes, str, str]:
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image_attachment must be base64-encoded image bytes") from exc
    if not content or len(content) > MAX_BYTES:
        raise ValueError("image attachment must be between 1 byte and 10 MiB")
    try:
        # only these formats' parsers are tried, so a decoder bug in another cannot be reached. The check
        # stays: a JPEG holding several pictures comes back as MPO
        with Image.open(io.BytesIO(content), formats=tuple(FORMATS)) as image:
            if image.format not in FORMATS:
                raise ValueError("only PNG, JPEG, GIF and WebP images are supported")
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("image attachment exceeds 20 million pixels")
            media_type, extension = FORMATS[image.format]
            image.verify()
        with Image.open(io.BytesIO(content), formats=tuple(FORMATS)) as image:
            image.load()
    except UnidentifiedImageError as exc:
        raise ValueError("only PNG, JPEG, GIF and WebP images are supported") from exc
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"invalid image attachment: {exc}") from exc
    return content, media_type, f"image.{extension}"
