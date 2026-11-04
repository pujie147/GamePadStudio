"""
Pytest configuration and headless environment initialization.
Ensures Qt and SDL run safely in headless CI and container environments.
"""
import os
import sys
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')


@pytest.fixture(autouse=True)
def block_real_hidhide_driver(monkeypatch):
    """测试绝不能读写这台机器的 HidHide 配置。

    控制设备是独占的，而且配置持久保存：一次漏网的写入会留下真实的隐藏条目，
    把开发机的手柄藏起来。这里给出一个良性假会话：读一律“未加载”，
    任何写操作直接失败，所以意外触碰驱动的测试仍然会被抓到。
    """
    from gamepadstudio import hidhide

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self._handle = None
            self.occupied = False
            self.holders = ()
            self.error = '测试环境不提供 HidHide 驱动'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def open(self):
            return False

        def close(self):
            pass

        def read(self):
            return hidhide.HidHideState(loaded=False, error=self.error)

        def apply(self, change):
            raise AssertionError('tests must not write the HidHide configuration')

    monkeypatch.setattr(hidhide, 'HidHideSession', FakeSession)
    monkeypatch.setattr(hidhide, 'running_hidhide_clients', lambda: [])


@pytest.fixture(autouse=True)
def isolate_workspace_output(monkeypatch):
    """UI regression tests must never send synthetic events to the desktop."""
    from gamepadstudio.i18n import set_language
    set_language('zh')
    if sys.platform == 'darwin':
        from tests.test_unified_mapping import Actions
        from types import SimpleNamespace
        from gamepadstudio.voice_entry import VoiceEntryService
        monkeypatch.setattr('gamepadstudio.studio.create_actions', Actions)
        class Recording:
            def __init__(self, *args, **kwargs):
                self.running = False
            def status(self):
                return {'running':self.running,'phase':'recording' if self.running else 'idle', 'last_error':''}
            def start(self):
                self.running = True
                return True
            def request_stop(self):
                self.running = False
                return True
            def stop(self):
                self.running = False
        monkeypatch.setattr('gamepadstudio.agent.ManualRecording', Recording)
        monkeypatch.setattr('gamepadstudio.studio.ManualRecording', Recording)
        # New voice controls must also stay isolated when old UI fixtures
        # construct a full Studio; never inspect or activate desktop apps.
        monkeypatch.setattr('gamepadstudio.voice_entry_ui.create_voice_service',
                            lambda: VoiceEntryService(target_backend=SimpleNamespace(
                                list_running_targets=lambda: [])))
