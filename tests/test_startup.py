import os
import subprocess
import sys
import pytest
from pcb_viewer.startup import desktop_error


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='X11/Wayland startup check is Linux-specific')
def test_headless_startup_is_actionable(monkeypatch):
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
    assert 'No desktop display' in desktop_error()
    process = subprocess.run([sys.executable, '-m', 'pcb_viewer'], capture_output=True, text=True)
    assert process.returncode == 1  # no Qt abort / SIGABRT
    assert 'No desktop display' in process.stderr


def test_headless_help_never_initializes_qt():
    result = subprocess.run([sys.executable, '-m', 'pcb_viewer', '--help'], capture_output=True, text=True)
    assert result.returncode == 0
    assert '--diagnose' in result.stdout


def test_offscreen_is_explicitly_supported(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    assert desktop_error() is None
