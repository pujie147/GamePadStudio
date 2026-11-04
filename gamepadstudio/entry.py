import os
os.environ['SDL_JOYSTICK_RAWINPUT'] = '0'
import argparse
import json
from pathlib import Path
import sys


def run():
    if sys.platform == 'win32':
        try:
            import ctypes
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            hdesk = user32.OpenDesktopW('Default', 0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception:
            pass
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--agent',action='store_true')
    parser.add_argument('--agent-command',choices=['status','pause','resume','stop','capture'])
    parser.add_argument('--exit-ui',action='store_true')
    parser.add_argument('--uncloak-all',action='store_true')
    args,_=parser.parse_known_args()
    if args.uncloak_all:
        from .hidhide_control import HidHideController
        result = HidHideController().reveal_all()
        if sys.stdout:
            print(json.dumps({'ok': bool(result.get('ok')),
                              'message': result.get('message', '')}, ensure_ascii=False))
        return 0 if result.get('ok') else 1
    if args.agent or args.agent_command or args.exit_ui:
        from .ipc import default_root,request
        from PySide6.QtCore import QCoreApplication
        root=args.data_dir or default_root()
        if args.agent:
            from .agent import run as agent_run
            return agent_run(root)
        app=QCoreApplication(sys.argv[:1])
        result=request(root,'exit' if args.exit_ui else args.agent_command,role='ui' if args.exit_ui else 'agent')
        if sys.stdout:print(json.dumps(result,ensure_ascii=False))
        return 0 if result and result.get('ok') else 1
    from .studio import run as studio_run
    return studio_run()
