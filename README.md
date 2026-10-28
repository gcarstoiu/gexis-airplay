# gexis-airplay

AirPlay 2 as a [Gexis Player](https://github.com/gcarstoiu/gexis-player)
plugin: an iPhone, iPad or Mac plays to the player, which is in its AirPlay
list under the player's own name.

The receiver is [Shairport Sync](https://github.com/mikebrady/shairport-sync)
with [NQPTP](https://github.com/mikebrady/nqptp), built for AirPlay 2 by
Gexis Player (its ADR-0167). This plugin is what sits between it and the
player, over the plugin contract (`docs/PLUGIN-CONTRACT.md` there, version 1):

- **Takes the audio device before Shairport Sync opens it.** Shairport
  Sync's active-state hook (`python3 -m gexis_airplay.hook begin`) asks the
  plugin, which sends `acquire` and answers the hook once the core says the
  device is free (`device_freed`), or after a bound.
- **Gives it up when the Apple device ends its session**, and ends the
  session when the core takes the device for another source (D-Bus
  `DropSession`, then the core's release ladder).
- **Now Playing** from Shairport Sync's metadata pipe: title, artist, album,
  cover, progress, whether it plays. Nothing of what plays is logged.
- **The Apple device's slider moves the player's level**, reported as
  `volume`; the core hands the starting level at a takeover, and a Fixed
  output does not follow it. Shairport Sync itself plays at full scale.
- **No transport controls yet**: AirPlay is controlled from the Apple
  device until Shairport Sync's AirPlay 2 remote control is shown to work.

No runtime dependencies; Pillow, which a Gexis player has, makes covers
800 px at most.

## Running it

On a Gexis player it is installed by the `gexis-airplay` package and runs
as `gexis-airplay.service`, bound to `gexis-shairport-sync.service`.

## Tests

```
python -m pytest
```

AirPlay is a trademark of Apple Inc., registered in the U.S. and other
countries and regions. This project is not affiliated with Apple.

Licence: GPL-3.0-or-later.
