# SPDX-License-Identifier: GPL-3.0-or-later
"""**What is playing, from the pipe's items**: one AirPlay session's track,
progress, state and the sender's volume, in the contract's words.

Nothing here talks to anything; `Session.take(item)` says what changed, and
`main` decides what to send.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from gexis_airplay.pipe import Item

logger = logging.getLogger("gexis_airplay.session")

#: AirPlay's volume, in its own dB: -30 is the bottom of the sender's slider,
#: 0 the top, -144 muted (Shairport Sync's `pvol`).
LOWEST, HIGHEST, MUTED = -30.0, 0.0, -144.0
STEPS = 100
#: The RTP clock of a stream whose format has not been said yet.
DEFAULT_RATE = 44100


def volume_steps(airplay_db: float) -> int:
    """The sender's slider, 0-100. Its scale is the slider's position, not a
    loudness: the core puts its own curve on it, as on Spotify's."""
    if airplay_db <= MUTED or airplay_db < LOWEST:
        return 0
    share = (min(airplay_db, HIGHEST) - LOWEST) / (HIGHEST - LOWEST)
    return round(share * STEPS)


@dataclass
class Changes:
    metadata: bool = False
    artwork: bytes | None = None
    volume: int | None = None
    began: bool = False
    ended: bool = False


@dataclass
class Session:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration: float | None = None
    transport: str = "stopped"
    codec: str | None = None
    rate: int = DEFAULT_RATE
    #: Progress as Shairport Sync last said it, and when (monotonic).
    _start: int | None = None
    _current: int | None = None
    _end: int | None = None
    _at: float | None = None
    _pending: dict = field(default_factory=dict)

    def position(self, now: float | None = None) -> float | None:
        if self._start is None or self._current is None:
            return None
        played = ((self._current - self._start) % 2 ** 32) / self.rate
        if self.transport == "playing" and self._at is not None:
            played += (now if now is not None else time.monotonic()) - self._at
        if self.duration is not None:
            played = min(played, self.duration)
        return max(0.0, played)

    def metadata(self) -> dict:
        """The contract's fields, only those known (absent draws nothing)."""
        out: dict = {"transport": self.transport}
        for key in ("title", "artist", "album", "duration", "codec"):
            value = getattr(self, key)
            if value:
                out[key] = value
        position = self.position()
        if position is not None:
            out["position"] = round(position, 3)
        return out

    def take(self, item: Item, now: float | None = None) -> Changes:
        now = now if now is not None else time.monotonic()
        changes = Changes()
        kind, code = item.type, item.code
        if kind == "core":
            if code in ("minm", "asar", "asal"):
                # A track's fields arrive between `mdst` and `mden`, and are
                # kept until the whole set is in, so a screen never shows the
                # new title over the old artist.
                self._pending[{"minm": "title", "asar": "artist", "asal": "album"}[code]] = item.text() or None
            elif code == "astm" and len(item.data) == 4:
                milliseconds = int.from_bytes(item.data, "big")
                self._pending["duration"] = milliseconds / 1000 if milliseconds else None
            return changes
        if kind != "ssnc":
            return changes
        if code == "mdst":
            self._pending = {}
        elif code == "mden":
            if self._pending:
                for key in ("title", "artist", "album"):
                    setattr(self, key, self._pending.get(key))
                if "duration" in self._pending:
                    self.duration = self._pending["duration"]
                self._pending = {}
                changes.metadata = True
        elif code == "PICT":
            changes.artwork = item.data
        elif code == "pvol":
            try:
                airplay_db = float(item.text().split(",")[0])
            except (ValueError, IndexError):
                logger.warning("a volume that did not parse")
            else:
                changes.volume = volume_steps(airplay_db)
        elif code == "prgr":
            try:
                start, current, end = (int(x) for x in item.text().split("/"))
            except ValueError:
                logger.warning("progress that did not parse")
            else:
                self._start, self._current, self._end, self._at = start, current, end, now
                # The track's own length, which a sender may not say in
                # `astm`; when both come, this one is the stream's.
                length = ((end - start) % 2 ** 32) / self.rate
                if length > 0:
                    self.duration = round(length, 3)
                changes.metadata = True
        elif code == "sdsc":
            # "ALAC/44100/S16_LE/2": the codec and the RTP clock's rate.
            parts = item.text().split("/")
            if parts and parts[0]:
                self.codec = parts[0]
            if len(parts) > 1 and parts[1].isdigit():
                self.rate = int(parts[1])
            changes.metadata = True
        elif code in ("pbeg", "prsm"):
            changes.began = code == "pbeg"
            self._playing(now)
            changes.metadata = True
        elif code in ("paus", "pfls"):
            self._paused(now)
            changes.metadata = True
        elif code == "pend":
            self._paused(now)
            self.transport = "stopped"
            changes.ended = True
            changes.metadata = True
        return changes

    def _playing(self, now: float) -> None:
        if self.transport != "playing":
            self._at = now
        self.transport = "playing"

    def _paused(self, now: float) -> None:
        if self.transport == "playing":
            # Hold the position where it stopped.
            position = self.position(now)
            if position is not None and self._start is not None:
                self._current = (self._start + round(position * self.rate)) % 2 ** 32
            self._at = now
        self.transport = "paused"

    def clear(self) -> None:
        """The session ended: nothing is playing, nothing is shown."""
        rate = self.rate
        self.__init__()
        self.rate = rate
