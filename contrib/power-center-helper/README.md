# Power Center helper

`pwrcntr-control.exe` is an optional 32-bit Win32 command-line helper for
Jandy's legacy Alwin32 `Pwrcntr.exe` Power Center emulator. It selects standard
Windows menu commands by their displayed text, avoiding AutoHotkey, mouse
coordinates, and direct calls into proprietary DLLs.

The helper source is part of AqualinkD Validator. Jandy executables, DLLs, and
data files are not included or redistributed.

## Install a prebuilt helper

Tagged validator releases publish `pwrcntr-control.exe` and
`pwrcntr-control.exe.sha256`. Download both from the repository's
[latest release](https://github.com/ballle98/aqualinkd-validator/releases/latest)
and verify the executable before using it:

```sh
sha256sum --check pwrcntr-control.exe.sha256
```

The prebuilt executable is the normal user path. A Windows compiler is needed
only when changing the helper.

## Build from Linux or WSL

On Debian or Ubuntu, install the MinGW cross-compiler and build:

```sh
sudo apt-get install gcc-mingw-w64-i686
make -C contrib/power-center-helper package
```

The results are `contrib/power-center-helper/build/pwrcntr-control.exe` and its
SHA-256 checksum file.

## Use under Wine

Start `Pwrcntr.exe` in its configured Wine prefix, then invoke the helper in
that same prefix:

```sh
export WINEPREFIX="$HOME/.wine-aqualink"
wine contrib/power-center-helper/build/pwrcntr-control.exe status
wine contrib/power-center-helper/build/pwrcntr-control.exe list
wine contrib/power-center-helper/build/pwrcntr-control.exe \
  model "E260808 (PD 8 Combo)"
wine contrib/power-center-helper/build/pwrcntr-control.exe port COM3
wine contrib/power-center-helper/build/pwrcntr-control.exe mode service
wine contrib/power-center-helper/build/pwrcntr-control.exe mode timeout
wine contrib/power-center-helper/build/pwrcntr-control.exe mode auto
wine contrib/power-center-helper/build/pwrcntr-control.exe power toggle
```

`model` and `port` require the exact displayed menu text after removing the
Windows mnemonic `&`. `list` prints the available text and command identifiers
from the running executable's menu resource. The resource state is useful for
discovery but does not necessarily reflect the application's current checked
item under Wine.

`mode` drives Power Center's Mode key and verifies the resulting Auto,
Service, or Time-Out indicator. The helper waits for the emulator to rebuild
its controls after each transition, so it does not skip a mode during a slow
Wine redraw.

Power Center exposes `Switch Power` as a toggle and does not publish a reliable
menu-level on/off state. The helper therefore calls the operation `power
toggle`; it does not present a blind toggle as idempotent `power on` or `power
off`. AqualinkD probe traffic and successful PDA initialization are the
authoritative verification that emulated power is on.

When a `power_center` section is present in `aqualinkd-validator.yaml`, the
validator invokes these commands itself. It observes the configured serial
device before and after toggling, establishes a verified off-to-on power cycle,
and records every helper result in `power-center.json`. If the validator runs
as root, Wine is automatically executed as the owner of the configured Wine
prefix.

## Scope and limitations

- The helper attaches to an already running visible Power Center window.
- It does not call `NetIO.dll`, `iodll.dll`, or any other proprietary API.
- It does not use image recognition or window coordinates.
- Menu text can change in a different vendor release; failed lookup returns a
  nonzero exit status rather than silently selecting another command.
- A stale Wine COM mapping can open a modal error before the helper attaches.
  Close that dialog, repair the `dosdevices/comN` link, and restart
  `Pwrcntr.exe`.
- Wine/PTTY timing is useful for functional validation, not a physical RS485
  performance baseline.
