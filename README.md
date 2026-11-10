# aw-watcher-afk: Windows x86_64 build

Unpack `aw-watcher-afk-windows-x86_64.zip` and run `aw-watcher-afk\aw-watcher-afk.exe`.

- Source: `master` at commit 5d0e49d (sleep-gap detection, reason=sleep tagging)
- Build date: 2026-11-10
- Built with PyInstaller 6.11.0 and Python 3.9.13 (x64) on Linux, under Wine.
- Unsigned. The Python installer's signature could not be verified during the build.
- Not run on real Windows. Wine's GetLastInputInfo and GetTickCount64 do not
  reflect real Windows input or sleep, so AFK detection and sleep handling are
  unverified on Windows. `--help` exits 0 under Wine.
