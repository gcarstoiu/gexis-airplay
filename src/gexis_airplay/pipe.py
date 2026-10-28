# SPDX-License-Identifier: GPL-3.0-or-later
"""**Shairport Sync's metadata pipe** (its `METADATA CODES.md`, and
`metadata/pipe.c` for the format).

Each item is written as

    <item><type>636f7265</type><code>6d696e6d</code><length>5</length>
    <data encoding="base64">
    SGVsbG8=</data></item>

- type and code are four ASCII letters as one hexadecimal number, the data
  base64 and only there when the length is not 0. `core` items come from the
  sender (title, artist...), `ssnc` ones from Shairport Sync (play began, a
  picture, the volume...).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("gexis_airplay.pipe")

ITEM = re.compile(
    rb"<item><type>([0-9a-f]{1,8})</type><code>([0-9a-f]{1,8})</code><length>(\d+)</length>"
    rb"(?:\n<data encoding=\"base64\">\n([A-Za-z0-9+/=\r\n]*)</data>)?</item>\n"
)
START = b"<item>"
#: A cover is the largest item; one far beyond any is a pipe gone wrong, and
#: what is held is dropped rather than grown without end.
MOST_BUFFERED = 16 * 1024 * 1024


@dataclass(frozen=True)
class Item:
    type: str
    code: str
    data: bytes

    def text(self) -> str:
        return self.data.decode("utf-8", "replace")


def _word(hexadecimal: bytes) -> str:
    return int(hexadecimal, 16).to_bytes(4, "big").decode("latin-1")


def parse(buffer: bytes) -> tuple[list[Item], bytes]:
    """The whole items at the front of `buffer`, and what is left of it: an
    item not finished yet, kept for the next read. Anything before an
    item's start is not ours and is dropped."""
    items: list[Item] = []
    at = 0
    while True:
        start = buffer.find(START, at)
        if start < 0:
            return items, b""
        match = ITEM.match(buffer, start)
        if match is None:
            # Either unfinished - more is coming - or malformed: an item that
            # starts again further on means this one never will finish.
            again = buffer.find(START, start + len(START))
            if again < 0:
                return items, buffer[start:]
            logger.warning("dropping a metadata item that did not parse")
            at = again
            continue
        kind, code, length, data = match.groups()
        try:
            payload = base64.b64decode(data, validate=False) if data else b""
        except binascii.Error:
            logger.warning("dropping a metadata item whose data was not base64")
            payload = b""
        if len(payload) != int(length):
            logger.debug("metadata item %s/%s: %d bytes, said %s", _word(kind), _word(code),
                         len(payload), length.decode())
        items.append(Item(_word(kind), _word(code), payload))
        at = match.end()


class Reader:
    """Reads items from the pipe for as long as it runs, and hands each to
    `on_item`. **Opened for reading and writing**, as a FIFO allows on Linux:
    a reader alone sees the end of the file whenever Shairport Sync is not
    holding it open, which would spin; with our own write end it never ends,
    and Shairport Sync, which writes only while a reader is there, can always
    open it."""

    def __init__(self, path: str | Path, on_item) -> None:
        self.path = Path(path)
        self.on_item = on_item

    def _open(self) -> int:
        if not self.path.exists():
            os.mkfifo(self.path, 0o600)
        if not stat.S_ISFIFO(self.path.stat().st_mode):
            raise OSError(f"{self.path} is not a pipe")
        return os.open(self.path, os.O_RDWR | os.O_NONBLOCK)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        fd = self._open()
        logger.info("reading metadata from %s", self.path)
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        loop.add_reader(fd, lambda: self._read(fd, queue))
        buffer = b""
        try:
            while True:
                buffer += await queue.get()
                items, buffer = parse(buffer)
                if len(buffer) > MOST_BUFFERED:
                    logger.warning("the metadata pipe held %d bytes without an item; dropped", len(buffer))
                    buffer = b""
                for item in items:
                    result = self.on_item(item)
                    if asyncio.iscoroutine(result):
                        await result
        finally:
            loop.remove_reader(fd)
            os.close(fd)

    @staticmethod
    def _read(fd: int, queue: asyncio.Queue) -> None:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return
        if chunk:
            queue.put_nowait(chunk)
