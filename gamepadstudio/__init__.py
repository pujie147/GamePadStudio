import os
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
os.environ['SDL_JOYSTICK_RAWINPUT'] = '0'

__all__ = [
    "config",
    "controller_service",
    "screenshot_service",
    "cli",
]

__version__ = "2.3.0"


