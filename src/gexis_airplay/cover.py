# SPDX-License-Identifier: GPL-3.0-or-later
"""**The cover the Apple device sent, made fit to send on**: at most
`SIDE` pixels a side, as JPEG, when Pillow is there (on a Gexis player it
is: `python3-pil`); as it came otherwise, if it is small enough for the
contract's one-line message (1 MiB, base64 included).
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging

logger = logging.getLogger("gexis_airplay.cover")

SIDE = 800
#: Raw bytes whose base64, in one JSON line with its envelope, stays under
#: the contract's 1 MiB.
MOST_RAW = 700 * 1024


def kind(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def fit(data: bytes) -> tuple[bytes, str] | None:
    """The picture to send and its type, or None when there is none to
    send (no picture, not a picture, or too large and no Pillow)."""
    if not data or kind(data) is None:
        return None
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        try:
            with Image.open(io.BytesIO(data)) as picture:
                if max(picture.size) > SIDE or len(data) > MOST_RAW:
                    picture.thumbnail((SIDE, SIDE))
                    out = io.BytesIO()
                    picture.convert("RGB").save(out, "JPEG", quality=85)
                    data = out.getvalue()
        except (OSError, ValueError) as exc:
            logger.warning("a cover that did not open: %s", exc)
            return None
    if len(data) > MOST_RAW:
        logger.info("a cover of %d bytes, too large to send without Pillow", len(data))
        return None
    return data, kind(data)


def key(data: bytes) -> str:
    """The name the core serves it under (the contract's `artwork` event):
    the first 16 hexadecimal digits of its sha256."""
    return hashlib.sha256(data).hexdigest()[:16]


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()
