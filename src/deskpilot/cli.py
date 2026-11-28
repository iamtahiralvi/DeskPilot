"""`deskpilot serve | pick | policy | doctor | unlock | timeline` (+ the internal `admin-helper`)."""
from __future__ import annotations

import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        from .server import run

        run()
    elif cmd == "pick":
        from .config import Session
        from .picker import run_picker

        res = run_picker()
        if res:
            s = Session(monitor=res["monitor"], mode=res["mode"], remember=res["remember"])
            s.save()
            print(f"saved: {res}")
        else:
            print("cancelled")
    elif cmd == "policy":
        from .config import POLICY_FILE
        from .policy import load_policy

        load_policy()
        print(POLICY_FILE)
    elif cmd == "doctor":
        from .config import monitors, set_dpi_awareness

        set_dpi_awareness()
        for i, m in enumerate(monitors()):
            print(f"screen {i + 1}: {m}")
        try:
            from .perceive import ocr

            print("ocr lines on screen 1:", len(ocr(monitors()[0])))
        except Exception as e:
            print("ocr unavailable:", e)
        from .elevation import LEVEL_NAMES, own_level

        print("deskpilot runs at:", LEVEL_NAMES.get(own_level(), hex(own_level())), "level")
        print("ok")
    elif cmd == "unlock":
        from .killswitch import clear_flag, killed

        was = killed()
        clear_flag()
        print("kill switch cleared" if was else "kill switch was not set")
    elif cmd == "admin-helper":  # started by the server with a UAC prompt; see elevation.py
        from .elevation import serve_helper

        if len(sys.argv) != 4:
            sys.exit(2)
        sys.exit(serve_helper(sys.argv[2], int(sys.argv[3])))
    elif cmd == "timeline":
        from .timeline import open_html

        print(open_html())
    else:
        print("usage: deskpilot [serve|pick|policy|doctor|unlock|timeline]")
        sys.exit(2)


if __name__ == "__main__":
    main()
