# SPDX-License-Identifier: GPL-3.0-or-later
"""**The plugin**: Shairport Sync on one side, the Gexis contract on the
other (ADR-0167 in gexis-player).

Its one judgement is when AirPlay takes the device and when it gives it up:

- **taken before Shairport Sync opens it.** Its active-state hook
  (`gexis_airplay.hook begin`) asks us; we `acquire`, and answer the hook
  when the core says the device is free (`device_freed`);
- **given up when the Apple device's session ends** (the pipe's `pend`):
  `release`. A pause keeps it, as a paused renderer still means to play;
- **taken away by the core**: its polite `release` ends the session over
  D-Bus (`DropSession`), and its ladder does the rest.

Everything else is translation: the pipe's items into `metadata`, the cover
into an `artwork` event, the sender's slider into `volume`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from gexis_airplay import cover, shairport
from gexis_airplay.contract import RECONNECT_S, Core, Refused
from gexis_airplay.pipe import Item, Reader
from gexis_airplay.session import STEPS, Session

logger = logging.getLogger("gexis_airplay")

ID = "airplay"
UNIT = "gexis-shairport-sync.service"

#: How long the begin hook waits for the device: the longest release the
#: other renderers take, with room. Past it the hook is answered anyway, and
#: Shairport Sync tries the device as it finds it.
BEGIN_S = 8.0

#: `DropSession` returns once the session is ended, and Shairport Sync then
#: closes the device; a second is the room left for that before the ladder.
RELEASE_LADDER = {"polite_grace": 1.0, "sigterm_grace": 2.0, "sigkill_grace": 2.0}

CAPABILITIES = {
    "audio_connection": "output",
    "acquisition_events": ["an Apple device starts playing to the player"],
    "supports_artwork": True,
    # AirPlay sends its stream's rate, never the file's (44.1 or 48 kHz).
    "supports_sample_rate": False,
    # **The sender's slider moves the player's level**, as Spotify's does
    # (ADR-0167 decision 3): reported to the core, which applies its curve.
    # Shairport Sync itself plays at full scale (`ignore_volume_control`).
    "volume_managed": True,
    "volume_mechanism": "software_api",
    "dummy_mixer_card": None,
    "volume_over_bluealsa": False,
    # At a takeover the Apple device's level is its own leftover - another
    # speaker's, perhaps; the core hands the starting level instead.
    "volume_handed": True,
    # ADR-0167 decision 2: none until upstream's AirPlay 2 remote control is
    # shown to work with an Apple device.
    "controls": [],
}


def hello() -> dict:
    return {
        "id": ID,
        "kind": "renderer",
        "name": "AirPlay",
        "unit": UNIT,
        "release_action": "disconnect",
        "release_ladder": RELEASE_LADDER,
        "capabilities": CAPABILITIES,
    }


class Plugin:
    """Lives for the process; the connection to the core comes and goes."""

    def __init__(self, drop_session=shairport.drop_session) -> None:
        self.core: Core | None = None
        self.session = Session()
        self._drop_session = drop_session
        #: The device is ours: the core said so (`device_freed`) and has not
        #: taken it back.
        self.active = False
        self._fixed = False
        #: ADR-0054 §5: from our acquisition until the core hands a level,
        #: the sender's own is not reported.
        self._volume_held = False
        self._volume: int | None = None
        self._waiting: list[asyncio.Future] = []
        self._artwork: str | None = None
        self._artwork_message: dict | None = None

    # --- to the core --------------------------------------------------------

    async def _say(self, t: str, **fields) -> bool:
        if self.core is None or not self.core.connected:
            return False
        try:
            await self.core.event(t, **fields)
            return True
        except (ConnectionError, OSError) as exc:
            logger.info("could not tell the core %s: %s", t, exc)
            return False

    async def _metadata(self) -> None:
        metadata = self.session.metadata()
        if self._artwork and self.session.transport != "stopped":
            metadata["artwork"] = self._artwork
        await self._say("metadata", metadata=metadata)

    async def begin(self) -> str:
        """The hook: Shairport Sync is about to go active."""
        if self.active:
            return "go: already ours"
        if self.core is None or not self.core.connected:
            return "go: the core is not connected"
        waiter = asyncio.get_running_loop().create_future()
        self._waiting.append(waiter)
        self._volume_held = True
        await self._say("acquire")
        try:
            return await asyncio.wait_for(waiter, BEGIN_S)
        except asyncio.TimeoutError:
            logger.warning("the core did not free the device within %.0fs; Shairport Sync goes on", BEGIN_S)
            return "go: not freed in time"
        finally:
            if waiter in self._waiting:
                self._waiting.remove(waiter)

    def _answer_waiting(self, answer: str) -> None:
        for waiter in self._waiting:
            if not waiter.done():
                waiter.set_result(answer)
        self._waiting.clear()

    async def item(self, item: Item) -> None:
        changes = self.session.take(item)
        if changes.artwork is not None:
            await self._cover(changes.artwork)
        if changes.volume is not None:
            await self._sender_volume(changes.volume)
        if changes.began and not self.active and not self._waiting:
            # The hook did not run (or ran before we were connected): late,
            # but the core is still told.
            logger.info("play began without the hook; acquiring now")
            self._volume_held = True
            await self._say("acquire")
        if changes.ended:
            await self._ended()
            return
        if changes.metadata or changes.artwork is not None:
            await self._metadata()

    async def _ended(self) -> None:
        logger.info("the Apple device ended its session")
        was_active = self.active
        self.active = False
        self.session.clear()
        self._artwork = self._artwork_message = None
        await self._metadata()
        if was_active:
            await self._say("release")

    async def _cover(self, data: bytes) -> None:
        if not data:
            self._artwork = self._artwork_message = None
            return
        fitted = await asyncio.to_thread(cover.fit, data)
        if fitted is None:
            self._artwork = self._artwork_message = None
            return
        picture, kind = fitted
        name = cover.key(picture)
        self._artwork_message = {"type": kind, "data": cover.encoded(picture)}
        # The picture first, then the metadata that names it: one connection
        # is read in order.
        if await self._say("artwork", **self._artwork_message):
            self._artwork = f"/plugins/{ID}/artwork/{name}"

    async def _sender_volume(self, steps: int) -> None:
        self._volume = steps
        if self._fixed:
            return
        if self._volume_held:
            logger.debug("the sender's level held back until the core hands one")
            return
        await self._say("volume", value=steps, steps=STEPS)

    # --- from the core ------------------------------------------------------

    async def command(self, t: str, message: dict):
        if t == "device_freed":
            self.active = True
            self._answer_waiting("go")
            return True
        if t == "release":
            # The polite stop: the session ends, and with it the device.
            self.active = False
            self._answer_waiting("released")
            return await self._drop_session()
        if t == "signal_stop":
            self.active = False
            self._answer_waiting("stopped")
            return True
        if t == "restart_after_release":
            return True
        if t == "set_volume":
            # The panel's slider, or the level handed at a takeover. Shairport
            # Sync cannot be trusted to move the Apple device's slider yet
            # (decision 3), so the level is the player's: reported back for
            # the core to apply.
            self._volume_held = False
            try:
                value, steps = int(message["value"]), int(message.get("steps") or STEPS)
            except (KeyError, TypeError, ValueError):
                raise ValueError("a volume without a usable value")
            await self._say("volume", value=value, steps=steps)
            return True
        if t == "output":
            self._fixed = bool(message.get("fixed"))
            logger.info("output is %s", "fixed: the sender's level is not followed" if self._fixed else "variable")
            return True
        if t == "setting":
            return True
        if t == "transport":
            raise ValueError("AirPlay is controlled from the Apple device")
        raise ValueError(f"{t} is not something this plugin does")

    async def connected(self, core: Core) -> None:
        """A new connection: the core knows nothing of us yet."""
        self.core = core
        self.active = False
        self._answer_waiting("go: reconnected")
        if self._artwork_message is not None:
            await self._say("artwork", **self._artwork_message)
        if self.session.transport != "stopped":
            await self._metadata()

    # --- the hook's socket --------------------------------------------------

    async def _hook(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await reader.readline()).strip().decode(errors="replace")
            if line == "begin":
                answer = await self.begin()
                logger.info("begin hook: %s", answer)
                writer.write(answer.encode() + b"\n")
                await writer.drain()
            elif line == "end":
                logger.info("Shairport Sync went inactive")
        except (ConnectionError, OSError) as exc:
            logger.info("the hook went away: %s", exc)
        finally:
            writer.close()

    async def serve_hook(self, path: str) -> asyncio.base_events.Server:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self._hook, path)
        os.chmod(path, 0o600)
        return server


async def run(socket_path: str, pipe_path: str, hook_path: str) -> None:
    plugin = Plugin()
    hook_server = await plugin.serve_hook(hook_path)
    reading = asyncio.ensure_future(Reader(pipe_path, plugin.item).run())
    try:
        while True:
            core = Core(socket_path)
            try:
                await core.connect(hello())
            except Refused as exc:
                logger.error("the core refused us: %s", exc)
                return
            except (ConnectionError, OSError) as exc:
                logger.info("cannot reach the core (%s); retrying", exc)
                await asyncio.sleep(RECONNECT_S)
                continue
            await plugin.connected(core)
            try:
                await core.serve(plugin.command)
            finally:
                await core.close()
                plugin.core = None
            logger.info("disconnected; reconnecting in %.0fs", RECONNECT_S)
            await asyncio.sleep(RECONNECT_S)
    finally:
        reading.cancel()
        hook_server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AirPlay as a Gexis plugin")
    parser.add_argument("--socket", default=os.environ.get("GEXIS_SOCKET", "/run/gexis/plugins.sock"))
    parser.add_argument("--pipe", default="/run/gexis-airplay/metadata")
    parser.add_argument("--hook", default="/run/gexis-airplay/hook.sock")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(name)s %(levelname)s %(message)s")
    Path(args.hook).parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(run(args.socket, args.pipe, args.hook))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
