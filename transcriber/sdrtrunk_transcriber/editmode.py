"""edit-mode.bat: swaps the no-window SDRTrunk (boot-on.bat) for the normal window, then back.

    python -m sdrtrunk_transcriber.editmode --config config.toml

While edit-mode.flag exists the watchdog leaves SDRTrunk alone, and the PC speakers player goes quiet
(the window plays the calls itself). When the window closes the flag goes and the watchdog starts the
no-window copy again at its next check.
"""

import argparse
import ntpath
import os
import subprocess
import time
from pathlib import Path

from . import watchdog


def find_bat(configured: str, found) -> str:
    if configured.strip():
        return configured.strip()
    for _, _, java in found:
        bat = ntpath.join(ntpath.dirname(ntpath.normpath(java)), "sdr-trunk.bat")
        if os.path.exists(bat):
            return bat
    return ""


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.editmode")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    from .config import load_config

    config = load_config(args.config if args.config.exists() else None)
    system = watchdog.System()
    found = system.sdrtrunk_processes() or []
    bat = find_bat(config.watchdog.sdrtrunk, found)
    if not bat:
        print("Can't tell where SDRTrunk is. Set [watchdog] sdrtrunk in config.toml to its sdr-trunk.bat.")
        return

    watchdog.EDIT_FLAG.write_text("SDRTrunk's window is open for changes (edit-mode.bat)\n", encoding="utf-8")
    try:
        if found:
            print("Stopping the no-window SDRTrunk...")
            for pid, _, _ in found:
                system.kill_tree(pid)
            time.sleep(watchdog.SETTLE_S)  # let the dongles go
        print("Opening SDRTrunk. Make your changes (they save by themselves), then close SDRTrunk's window.")
        env = dict(os.environ)
        env.pop("JAVA_OPTS", None)
        subprocess.run(["cmd", "/c", bat], cwd=ntpath.dirname(bat), env=env)
    finally:
        watchdog.EDIT_FLAG.unlink(missing_ok=True)
    print("Done. The no-window SDRTrunk starts again within a minute.")


if __name__ == "__main__":
    main()
