"""Admin windows (UIPI) and the admin helper's pipe protocol. The helper runs without elevation here: same code
path, no UAC prompt."""
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_own_level_and_self_not_above_us():
    from deskpilot import elevation as e

    assert e.own_level() in (e.MEDIUM, e.HIGH, e.SYSTEM)
    assert e.process_level(os.getpid()) == e.own_level()
    assert not e.is_above_us(os.getpid())


def test_blocked_message_says_what_to_do():
    from deskpilot.elevation import blocked_message

    m = blocked_message("OCCT", "OCCT.exe", True)
    assert "ADMIN WINDOW" in m and "OCCT.exe" in m and 'screen_info(op="admin")' in m and "NOT done" in m
    off = blocked_message("OCCT", "OCCT.exe", False)
    assert "screen_info" not in off and "switched off" in off


def test_admin_needs_confirmation_by_policy():
    from deskpilot.policy import Policy

    d = Policy(rules=__import__("deskpilot.policy", fromlist=["DEFAULT_POLICY"]).DEFAULT_POLICY.copy()).check(
        {"kind": "admin"})
    assert d.level == "ask" and d.key == "admin"


def test_node_roundtrip():
    from deskpilot.elevation import node_from_dict, node_to_dict
    from deskpilot.perceive import Node

    n = Node("window", "OCCT", rect=(1, 2, 3, 4), children=[Node("button", "Start", rect=(5, 6, 7, 8))])
    back = node_from_dict(node_to_dict(n))
    assert back == n and isinstance(back.rect, tuple) and back.children[0].name == "Start"
    assert node_from_dict(None) is None


def test_helper_protocol_unelevated():
    from deskpilot import elevation as e

    h = e.AdminHelper()
    h.start(elevate=False, timeout=60)
    try:
        assert h.active and h.pid
        assert h.call("ping") == "pong"
        assert h.call("input", items=[]) is True  # nothing sent, but the round trip works
        f = h.call("focused")
        assert f is None or "role" in f
        with pytest.raises(e.HelperError):
            h.call("no_such_op")
        assert h.active  # an error answer doesn't kill the helper
    finally:
        h.stop()
    assert not h.active
    with pytest.raises(e.HelperError):
        h.call("ping")


def test_helper_refuses_a_pipe_owned_by_another_process():
    """The helper only serves the server named on its command line."""
    import win32file
    import win32pipe

    name = rf"\\.\pipe\deskpilot-test-{os.getpid()}"
    pipe = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX, 0, 1, 4096, 4096, 0, None)
    try:
        p = subprocess.run([sys.executable, "-I", "-m", "deskpilot.cli", "admin-helper", name, str(os.getpid() + 1)],
                           timeout=60)
        assert p.returncode == 3
    finally:
        win32file.CloseHandle(pipe)


def test_secure_desktop_check_returns_a_bool():
    from deskpilot.elevation import SECURE_DESKTOP_MSG, secure_desktop

    assert isinstance(secure_desktop(), bool)
    assert "UAC" in SECURE_DESKTOP_MSG and "user" in SECURE_DESKTOP_MSG


class _FakeAdmin:
    active = True

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True
        self.active = False


def _desk_stub(mode="ask", idle=300.0):
    """Just enough of a Desktop for the admin-mode lifetime logic."""
    from deskpilot.desktop import Desktop

    d = Desktop.__new__(Desktop)
    d.admin, d.admin_mode, d.admin_idle = _FakeAdmin(), mode, idle
    d._idle_timer = d._admin_timer = None
    d.idle_hide = 0
    d.ui_log = lambda *a, **k: None
    return d


def test_admin_mode_survives_idle_but_expires_later():
    d = _desk_stub(idle=0.2)
    d.idle()
    assert d._admin_timer is not None and d.admin.active  # the call ended; admin mode waits
    d.busy()
    assert d._admin_timer is None and d.admin.active  # a new call cancels the countdown
    d.idle()
    d._admin_timer.join(2)
    assert d.admin.stopped


def test_always_mode_has_no_admin_countdown():
    d = _desk_stub(mode="always", idle=0.2)
    d.idle()
    assert d._admin_timer is None


def test_needs_admin_reads_the_manifest():
    import shutil

    from deskpilot.elevation import launch_admin_note, needs_admin

    assert needs_admin(shutil.which("notepad")) is None
    assert needs_admin(os.path.join(os.environ["WINDIR"], "regedit.exe")) == "highestAvailable"
    assert needs_admin(os.path.join(os.environ["WINDIR"], "System32", "msconfig.exe")) == "requireAdministrator"
    assert needs_admin(None) is None and needs_admin(r"C:\nope\missing.exe") is None
    assert "UAC" in launch_admin_note("DDU", "requireAdministrator")


def test_app_id_path_resolves_known_folders():
    from deskpilot.elevation import app_id_path

    p = app_id_path(r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\msconfig.exe")  # System32
    assert p and p.lower().endswith(r"system32\msconfig.exe") and os.path.isfile(p)
    assert app_id_path("Microsoft.WindowsNotepad_8wekyb3d8bbwe!App") is None
    assert app_id_path(r"C:\Tools\x.exe") == r"C:\Tools\x.exe"


def test_landed_note_quiet_when_the_clicked_window_stays_in_front(monkeypatch):
    from deskpilot import desktop as dk, winutil

    d = dk.Desktop.__new__(dk.Desktop)
    monkeypatch.setattr(winutil, "foreground", lambda: 0)
    assert d._landed_note(1234) == ""  # no foreground window: nothing to say
    assert d._landed_note(0) == ""
    assert not dk._related(0, 0)


def test_refused_pointer_move_is_not_the_user(monkeypatch):
    from deskpilot import act

    monkeypatch.setattr(act, "remote_cursor", None)
    monkeypatch.setattr(act.user32, "SetCursorPos", lambda x, y: 0)
    with pytest.raises(act.CursorBlocked):
        act.set_cursor(10, 10)
    calls = []
    monkeypatch.setattr(act, "remote_cursor", lambda x, y: calls.append((x, y)))
    act.set_cursor(5, 6)  # admin mode: the helper moves it
    assert calls == [(5, 6)]


def test_cursor_blocked_message():
    from deskpilot.elevation import cursor_blocked_message

    m = cursor_blocked_message("HWiNFO", "HWiNFO64.EXE", True)
    assert "IN FRONT" in m and "window(" in m and "NOT done" in m and 'op="admin"' in m
