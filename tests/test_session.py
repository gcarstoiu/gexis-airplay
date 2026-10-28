# SPDX-License-Identifier: GPL-3.0-or-later
from gexis_airplay.pipe import Item
from gexis_airplay.session import Session, volume_steps


def ssnc(code, text=""):
    return Item("ssnc", code, text.encode())


def core(code, data):
    return Item("core", code, data if isinstance(data, bytes) else data.encode())


def test_a_track_is_shown_only_when_its_set_is_whole():
    s = Session()
    s.take(ssnc("mdst"))
    assert not s.take(core("minm", "Title")).metadata
    s.take(core("asar", "Artist"))
    s.take(core("asal", "Album"))
    s.take(core("astm", (215_000).to_bytes(4, "big")))
    assert s.title is None
    assert s.take(ssnc("mden")).metadata
    assert (s.title, s.artist, s.album, s.duration) == ("Title", "Artist", "Album", 215.0)


def test_the_next_track_drops_what_it_does_not_say():
    s = Session()
    for i in (ssnc("mdst"), core("minm", "One"), core("asal", "Album"), ssnc("mden")):
        s.take(i)
    for i in (ssnc("mdst"), core("minm", "Two"), ssnc("mden")):
        s.take(i)
    assert (s.title, s.album) == ("Two", None)


def test_progress_and_the_playhead():
    s = Session()
    s.take(ssnc("sdsc", "AAC/48000/F32_LE/2"))
    s.take(ssnc("pbeg"), now=100.0)
    s.take(ssnc("prgr", f"{1000}/{1000 + 48000 * 30}/{1000 + 48000 * 200}"), now=100.0)
    assert s.duration == 200.0
    assert s.position(now=105.0) == 35.0
    s.take(ssnc("paus"), now=110.0)
    assert s.transport == "paused"
    assert s.position(now=500.0) == 40.0
    s.take(ssnc("prsm"), now=600.0)
    assert s.position(now=601.0) == 41.0


def test_progress_across_the_rtp_clock_wrapping():
    s = Session()
    start = 2 ** 32 - 44100 * 10
    s.take(ssnc("prgr", f"{start}/{(start + 44100 * 20) % 2 ** 32}/{(start + 44100 * 60) % 2 ** 32}"), now=0.0)
    assert s.duration == 60.0
    assert s.position(now=0.0) == 20.0


def test_play_end_stops():
    s = Session()
    s.take(ssnc("pbeg"))
    changes = s.take(ssnc("pend"))
    assert changes.ended and s.transport == "stopped"


def test_the_senders_slider():
    assert volume_steps(0.0) == 100
    assert volume_steps(-30.0) == 0
    assert volume_steps(-15.0) == 50
    assert volume_steps(-144.0) == 0
    assert volume_steps(-60.0) == 0
    s = Session()
    assert s.take(ssnc("pvol", "-15.00,-30.00,-60.00,0.00")).volume == 50
    assert s.take(ssnc("pvol", "garbage")).volume is None


def test_the_metadata_names_only_what_is_known():
    s = Session()
    assert s.metadata() == {"transport": "stopped"}
    s.take(ssnc("sdsc", "ALAC/44100/S16_LE/2"))
    assert s.metadata()["codec"] == "ALAC"
