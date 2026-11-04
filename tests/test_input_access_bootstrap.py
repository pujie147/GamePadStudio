"""Device access is prepared before SDL discovers devices in a fresh process."""
from gamepadstudio.device import Device


class FakeFunction:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    def __call__(self, *args):
        self.calls.append(self.name)
        return 0


class FakeSDL:
    def __init__(self, calls):
        self.calls, self.functions = calls, {}

    def __getattr__(self, name):
        return self.functions.setdefault(name, FakeFunction(name, self.calls))


def test_device_bootstrap_runs_before_sdl_init(monkeypatch):
    calls = []
    monkeypatch.setattr('gamepadstudio.device.C.CDLL', lambda path: FakeSDL(calls))
    monkeypatch.setattr('gamepadstudio.hidhide.ensure_self_access',
                        lambda: calls.append('bootstrap') or (True, ''))
    monkeypatch.setattr('gamepadstudio.device.sys.platform', 'win32')
    device = Device()
    try:
        assert calls.index('bootstrap') < calls.index('SDL_Init')
        assert not device.access_warning and not device.error
    finally:
        device.close()


def test_bootstrap_failure_keeps_sdl_running_and_exposes_warning(monkeypatch):
    calls = []
    warning = '当前程序未获得隐藏手柄的访问权限。'
    monkeypatch.setattr('gamepadstudio.device.C.CDLL', lambda path: FakeSDL(calls))
    monkeypatch.setattr('gamepadstudio.hidhide.ensure_self_access',
                        lambda: (False, warning))
    monkeypatch.setattr('gamepadstudio.device.sys.platform', 'win32')
    device = Device()
    try:
        assert 'SDL_Init' in calls
        assert device.access_warning == warning and device.error == warning
    finally:
        device.close()
