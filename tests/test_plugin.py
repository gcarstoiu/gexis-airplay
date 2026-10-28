# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import io

import pytest

from gexis_airplay import main
from gexis_airplay.main import Plugin, hello
from gexis_airplay.pipe import Item


class FakeCore:
    """What the plugin tells the core, in order."""

    def __init__(self, connected=True):
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []

    async def event(self, t, **fields):
        if not self.connected:
            raise ConnectionError("not connected")
        self.sent.append((t, fields))

    def kinds(self):
        return [t for t, _ in self.sent]


def plugin(core=None, dropped=None):
    async def drop():
        if dropped is not None:
            dropped.append(True)
        return True
    p = Plugin(drop_session=drop)
    p.core = core if core is not None else FakeCore()
    return p


def ssnc(code, text=""):
    return Item("ssnc", code, text.encode())


def test_hello_matches_the_manifest():
    import json
    from pathlib import Path
    manifest = json.loads((Path(__file__).parent.parent / "plugin.json").read_text())
    said = hello()
    assert (said["id"], said["unit"], said["kind"]) == (manifest["id"], manifest["unit"], manifest["kind"])
    # Decision 2: no transport controls until upstream's are shown to work.
    assert said["capabilities"]["controls"] == []


async def test_begin_waits_for_the_device_then_goes():
    p = plugin()
    begun = asyncio.ensure_future(p.begin())
    await asyncio.sleep(0.01)
    assert p.core.kinds() == ["acquire"] and not begun.done()
    assert await p.command("device_freed", {}) is True
    assert await begun == "go"
    assert p.active


async def test_begin_is_bounded_when_the_core_never_frees_it(monkeypatch):
    # The core refuses an acquire silently (a switched-off row, an update
    # under way): the hook must still return.
    monkeypatch.setattr(main, "BEGIN_S", 0.05)
    p = plugin()
    assert (await p.begin()).startswith("go: not freed")
    assert not p.active


async def test_begin_without_the_core_does_not_wait():
    p = plugin(core=FakeCore(connected=False))
    assert (await p.begin()).startswith("go: the core is not connected")


async def test_begin_when_already_ours_does_not_ask_again():
    p = plugin()
    p.active = True
    assert (await p.begin()).startswith("go: already")
    assert p.core.kinds() == []


async def test_the_cores_release_ends_the_session():
    dropped = []
    p = plugin(dropped=dropped)
    p.active = True
    assert await p.command("release", {}) is True
    assert dropped == [True] and not p.active
    # The session's end then arrives on the pipe, and is not a second release.
    await p.item(ssnc("pend"))
    assert "release" not in p.core.kinds()


async def test_the_senders_end_releases_the_device():
    p = plugin()
    p.active = True
    await p.item(ssnc("pbeg"))
    await p.item(ssnc("pend"))
    kinds = p.core.kinds()
    assert kinds[-1] == "release"
    assert p.core.sent[-2] == ("metadata", {"metadata": {"transport": "stopped"}})


async def test_a_release_during_begin_answers_the_hook():
    p = plugin()
    begun = asyncio.ensure_future(p.begin())
    await asyncio.sleep(0.01)
    await p.command("release", {})
    assert await begun == "released"


async def test_the_senders_level_waits_for_the_handed_one():
    p = plugin()
    begun = asyncio.ensure_future(p.begin())
    await asyncio.sleep(0.01)
    await p.command("device_freed", {})
    await begun
    await p.item(ssnc("pvol", "0.00,0.00,-60.00,0.00"))
    assert "volume" not in p.core.kinds()
    # The core hands the starting level; it is reported back as the level.
    await p.command("set_volume", {"value": 30, "steps": 100})
    assert p.core.sent[-1] == ("volume", {"value": 30, "steps": 100})
    await p.item(ssnc("pvol", "-15.00,-30.00,-60.00,0.00"))
    assert p.core.sent[-1] == ("volume", {"value": 50, "steps": 100})


async def test_fixed_output_does_not_follow_the_sender():
    p = plugin()
    await p.command("output", {"fixed": True})
    await p.command("set_volume", {"value": 30, "steps": 100})
    p.core.sent.clear()
    await p.item(ssnc("pvol", "-15.00,-30.00,-60.00,0.00"))
    assert p.core.sent == []


async def test_transport_is_refused():
    p = plugin()
    with pytest.raises(ValueError):
        await p.command("transport", {"command": "pause"})


async def test_the_cover_goes_before_the_metadata_that_names_it():
    pytest.importorskip("PIL")
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (1200, 1200), (10, 20, 30)).save(out, "PNG")
    p = plugin()
    await p.item(ssnc("pbeg"))
    p.core.sent.clear()
    await p.item(Item("ssnc", "PICT", out.getvalue()))
    kinds = p.core.kinds()
    assert kinds == ["artwork", "metadata"]
    art = p.core.sent[0][1]
    assert art["type"] == "image/jpeg"
    name = p.core.sent[1][1]["metadata"]["artwork"]
    assert name.startswith("/plugins/airplay/artwork/") and len(name.rsplit("/", 1)[1]) == 16
    # Resized to the contract's size on the way.
    import base64
    with Image.open(io.BytesIO(base64.b64decode(art["data"]))) as sent:
        assert max(sent.size) == 800


async def test_not_a_picture_is_no_cover():
    p = plugin()
    await p.item(ssnc("pbeg"))
    await p.item(Item("ssnc", "PICT", b"not an image"))
    assert "artwork" not in p.core.kinds()


async def test_a_reconnection_says_it_all_again():
    p = plugin()
    await p.item(ssnc("pbeg"))
    p.active = True
    fresh = FakeCore()
    await p.connected(fresh)
    assert not p.active
    assert fresh.kinds() == ["metadata"]
