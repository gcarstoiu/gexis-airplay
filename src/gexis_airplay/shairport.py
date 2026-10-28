# SPDX-License-Identifier: GPL-3.0-or-later
"""**Shairport Sync's D-Bus interface**, through `busctl` (systemd's, on
every Gexis player), so the plugin needs no D-Bus library.

The name is `org.gnome.ShairportSync` on the system bus; its policy lets
only this plugin's user and root call it.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("gexis_airplay.shairport")

NAME = "org.gnome.ShairportSync"
PATH = "/org/gnome/ShairportSync"
#: A call that takes longer than this is a Shairport Sync that is not
#: answering; the core's ladder then takes it off the device.
TIMEOUT_S = 3.0


async def call(interface: str, method: str, *args: str) -> bool:
    """One method call; True when Shairport Sync answered without error."""
    command = ["busctl", "--system", f"--timeout={TIMEOUT_S:g}", "call", NAME, PATH, interface, method, *args]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(process.communicate(), TIMEOUT_S + 1)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("%s.%s: %s", interface.rsplit(".", 1)[-1], method, exc)
        return False
    if process.returncode != 0:
        logger.warning("%s.%s failed: %s", interface.rsplit(".", 1)[-1], method,
                       err.decode(errors="replace").strip()[:200])
        return False
    return True


async def drop_session() -> bool:
    """End the sender's session: Shairport Sync stops playing and closes the
    device, and the Apple device shows it disconnected."""
    return await call(NAME, "DropSession")
