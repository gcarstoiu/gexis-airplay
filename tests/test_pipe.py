# SPDX-License-Identifier: GPL-3.0-or-later
import asyncio
import base64
import os

from gexis_airplay.pipe import Item, Reader, parse


def item(kind: str, code: str, data: bytes = b"") -> bytes:
    """An item as Shairport Sync's metadata/pipe.c writes it."""
    head = f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code><length>{len(data)}</length>"
    if data:
        head += '\n<data encoding="base64">\n' + base64.b64encode(data).decode() + "</data>"
    return (head + "</item>\n").encode()


def test_items_are_read_with_and_without_data():
    items, rest = parse(item("core", "minm", "Sång".encode()) + item("ssnc", "pbeg"))
    assert items == [Item("core", "minm", "Sång".encode()), Item("ssnc", "pbeg", b"")]
    assert items[0].text() == "Sång"
    assert rest == b""


def test_an_unfinished_item_waits_for_the_rest():
    whole = item("ssnc", "PICT", bytes(range(256)) * 40)
    items, rest = parse(whole[:500])
    assert items == [] and rest == whole[:500]
    items, rest = parse(rest + whole[500:])
    assert items[0].data == bytes(range(256)) * 40 and rest == b""


def test_what_is_not_an_item_is_dropped():
    items, rest = parse(b"noise" + item("ssnc", "pend") + b"<item><type>zz</type>" + item("ssnc", "prsm"))
    assert [i.code for i in items] == ["pend", "prsm"]
    assert rest == b""


def test_a_code_with_a_leading_zero_byte_keeps_four_letters():
    # %x drops leading zeros: a code whose first byte were 0 would be short.
    items, _ = parse(b"<item><type>73736e63</type><code>616263</code><length>0</length></item>\n")
    assert items[0].code == "\x00abc"


async def test_the_reader_reads_a_pipe_written_after_it_opened(tmp_path):
    path = tmp_path / "metadata"
    got: list[Item] = []
    reading = asyncio.ensure_future(Reader(path, got.append).run())
    await asyncio.sleep(0.05)
    # As Shairport Sync does: it opens the pipe for writing once a reader is
    # there, writes, and may close it again.
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, item("ssnc", "pbeg") + item("core", "asar", b"Someone"))
    os.close(fd)
    for _ in range(50):
        if len(got) == 2:
            break
        await asyncio.sleep(0.02)
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, item("ssnc", "pend"))
    os.close(fd)
    for _ in range(50):
        if len(got) == 3:
            break
        await asyncio.sleep(0.02)
    reading.cancel()
    assert [i.code for i in got] == ["pbeg", "asar", "pend"]
