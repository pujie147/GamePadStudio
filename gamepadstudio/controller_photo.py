"""Unified standard XInput controller schematic rendered via offline SVG vector graphics.

All controllers share the single standard XInput layout schematic (`reference_xbox.svg`).
When a physical device is connected, the UI switches the displayed controller name and
reported capabilities without switching per-model product photos.
"""
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal, QByteArray
from PySide6.QtGui import QColor, QPainter, QPixmap, QPen, QFont, QPolygonF
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget, QSizePolicy

from .glass import TOKENS
from .controller_catalog import axis_labels


ASSETS = Path(__file__).resolve().parent / 'assets'
XINPUT_SVG_FILE = 'reference_xbox.svg'
XINPUT_VIEWBOX = (0, 0, 441, 383)

_DEFAULT_CAPTION = '通用 XInput 键位示意'
PHOTOS = {
    family: {'caption': _DEFAULT_CAPTION}
    for family in ('dualsense', 'dualshock4', 'xbox', 'switch', 'stadia', 'flightstick', 'generic')
}

# Standard button centers in the 441x383 XInput vector schematic.
XINPUT_SVG_CENTERS = {
    0: (330.0, 181.0),
    1: (348.0, 161.0),
    2: (310.0, 162.0),
    3: (329.0, 140.0),
    4: (188.0, 162.0),
    5: (220.5, 125.0),
    6: (253.0, 162.0),
    7: (113.0, 160.0),
    8: (278.0, 238.0),
    9: (138.5, 77.0),
    10: (302.5, 77.0),
    11: (166.0, 221.0),
    12: (166.0, 254.0),
    13: (149.0, 238.0),
    14: (183.0, 238.0),
    15: (220.5, 188.0),
}


# The two thumbsticks share the well centres drawn in reference_xbox.svg. Direction
# chips sit on the rim so they stay clear of the L3 / R3 press anchor at the centre.
XINPUT_STICK_CENTERS = {'LS': (113.0, 160.0), 'RS': (278.0, 238.0)}
STICK_DIRECTION_VECTORS = {'up': (0.0, -1.0), 'right': (1.0, 0.0),
                           'down': (0.0, 1.0), 'left': (-1.0, 0.0)}
STICK_RIM = 30.0


def tint_xbox(svg_bytes):
    s = svg_bytes.decode('utf-8')
    # 1. Outer body: dark stone-800 with clean stroke-stone-700
    s = s.replace(
        'fill:rgb(245, 245, 244);stroke:rgb(214, 211, 209);stroke-width:3px',
        'fill:#292524;stroke:#57534e;stroke-width:3px'
    )
    # 2. Bumpers & triggers
    s = s.replace(
        'fill:rgb(231, 229, 228);stroke:rgb(214, 211, 209);stroke-width:2px',
        'fill:#23201e;stroke:#44403c;stroke-width:2px'
    )
    # 3. Stick wells & dishes
    s = s.replace(
        'fill:rgb(250, 250, 249);stroke:rgb(231, 229, 228);stroke-width:2px',
        'fill:#1c1917;stroke:#44403c;stroke-width:2px'
    )
    # 4. Stick heads
    s = s.replace(
        'fill:rgb(214, 211, 209);stroke:rgb(255, 255, 255);stroke-width:2px',
        'fill:#383533;stroke:#57534e;stroke-width:2px'
    )
    # 5. Buttons & D-pad cross
    s = s.replace('fill:rgb(231, 229, 228);', 'fill:#383533;')
    s = s.replace('stroke:rgb(168, 162, 158);', 'stroke:#a8a29e;')
    # 6. Face button caps (Y, X, B, A) with dark tactile caps & crisp colored rings
    s = s.replace(
        'fill:none;stroke:rgb(234, 179, 8);stroke-opacity:0.5;stroke-width:2px',
        'fill:#1f1c1a;stroke:#eab308;stroke-opacity:0.9;stroke-width:2px'
    )
    s = s.replace(
        'fill:none;stroke:rgb(59, 130, 246);stroke-opacity:0.5;stroke-width:2px',
        'fill:#1f1c1a;stroke:#3b82f6;stroke-opacity:0.9;stroke-width:2px'
    )
    s = s.replace(
        'fill:none;stroke:rgb(239, 68, 68);stroke-opacity:0.5;stroke-width:2px',
        'fill:#1f1c1a;stroke:#ef4444;stroke-opacity:0.9;stroke-width:2px'
    )
    s = s.replace(
        'fill:none;stroke:rgb(22, 163, 74);stroke-opacity:0.5;stroke-width:2px',
        'fill:#1f1c1a;stroke:#22c55e;stroke-opacity:0.9;stroke-width:2px'
    )
    return s.encode('utf-8')


# Compatibility aliases
tint_playstation = tint_xbox
tint_switch = tint_xbox

_renderer = None


def _get_renderer():
    global _renderer
    if _renderer is None:
        raw = (ASSETS / XINPUT_SVG_FILE).read_bytes()
        tinted = tint_xbox(raw)
        _renderer = QSvgRenderer(QByteArray(tinted))
    return _renderer


def controller_art_rect(rect, family='generic'):
    """Use the same proportional stage fit for the unified XInput schematic and interactive anchors."""
    _, _, width, height = XINPUT_VIEWBOX
    margin = max(8., min(24., min(rect.width(), rect.height()) * .035))
    area = rect.adjusted(margin, margin, -margin, -margin)
    if area.width() <= 0 or area.height() <= 0:
        return QRectF()
    scale = min(area.width() / width, area.height() / height)
    target = QRectF(0, 0, width * scale, height * scale)
    target.moveCenter(rect.center())
    return target


def draw_controller_svg(painter: QPainter, rect: QRectF, family: str = 'generic', led_color: str = None):
    """Render the unified standard XInput vector graphic with crisp button engravings."""
    target = controller_art_rect(rect, family)
    if target.isEmpty():
        return
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    _get_renderer().render(painter, target)

    _, _, vw, vh = XINPUT_VIEWBOX
    sx = target.width() / vw
    sy = target.height() / vh
    scale = min(sx, sy)

    def _pt(x, y, w, h):
        return QRectF(
            target.left() + (x - w / 2.0) * sx,
            target.top() + (y - h / 2.0) * sy,
            w * sx,
            h * sy,
        )

    # Face button engravings (A / B / X / Y)
    face_font = QFont('Segoe UI')
    face_font.setPixelSize(max(8, round(12.0 * scale)))
    face_font.setWeight(QFont.Bold)
    painter.setFont(face_font)
    for cx, cy, text, color in (
        (330.0, 181.0, 'A', '#4ade80'),
        (348.0, 161.0, 'B', '#f87171'),
        (310.0, 162.0, 'X', '#60a5fa'),
        (329.0, 140.0, 'Y', '#facc15'),
    ):
        painter.setPen(QColor(color))
        painter.drawText(_pt(cx, cy, 24.0, 24.0), Qt.AlignCenter, text)

    painter.restore()


def draw_controller_contour(painter, rect, family='generic', led_color=None):
    draw_controller_svg(painter, rect, family, led_color)


@lru_cache(maxsize=2)
def product_pixmap(family='generic'):
    pix = QPixmap(800, 500)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(20, 20, 760, 460)
    draw_controller_svg(painter, rect, family, TOKENS['accent'])
    painter.end()
    return pix


def photo_health():
    exists = (ASSETS / XINPUT_SVG_FILE).is_file()
    return {family: exists for family in PHOTOS}


class ControllerPhoto(QWidget):
    """Unified XInput Controller Schematic Widget."""

    def __init__(self, family='generic', parent=None):
        super().__init__(parent)
        self.family = None
        self.led = TOKENS['accent']
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.set_family(family)

    def sizeHint(self):
        return QSize(320, 160)

    def set_family(self, family):
        family = family if family in PHOTOS else 'generic'
        if family == self.family:
            return
        self.family = family
        self.info = dict(PHOTOS[family])
        self.setAccessibleName(self.info['caption'])
        self.setToolTip(self.info['caption'])
        self.update()

    def set_led(self, color_hex):
        self.led = color_hex
        self.update()

    def product_rect(self):
        stage = self.product_stage()
        return controller_art_rect(stage, self.family)

    def product_stage(self):
        return QRectF(self.rect()).adjusted(1, 1, -1, -1)

    def trigger_anchors(self):
        rect = self.product_rect()
        if rect.isEmpty():
            return {}
        _, _, vw, vh = XINPUT_VIEWBOX
        return {'LT': (138.5 / vw, 35.0 / vh), 'RT': (302.5 / vw, 35.0 / vh)}

    def stick_direction_anchors(self):
        """The four rim points of each thumbstick, as `LS:up` style tokens."""
        _, _, vw, vh = XINPUT_VIEWBOX
        result = {}
        for stick, (cx, cy) in XINPUT_STICK_CENTERS.items():
            for direction, (dx, dy) in STICK_DIRECTION_VECTORS.items():
                result[f'{stick}:{direction}'] = ((cx + dx * STICK_RIM) / vw,
                                                  (cy + dy * STICK_RIM) / vh)
        return result

    def input_is_available(self, key):
        available = getattr(self, 'available', None)
        if key in ('LT', 'RT'):
            if available is not None and (not available or any(isinstance(item, str) for item in available)):
                return key in available
            axes = getattr(self, 'available_axes', None)
            return axes is None or (4 if key == 'LT' else 5) in axes
        if isinstance(key, str) and key.count(':') == 1 and key.split(':')[0] in XINPUT_STICK_CENTERS:
            axes = getattr(self, 'available_axes', None)
            if axes is None:
                return available is None or key in available or str(key) in available
            return (0 if key.startswith('LS:') else 2) in axes
        return available is None or key in available or str(key) in available

    def trigger_regions(self):
        rect = self.product_rect()
        return {key: QRectF(rect.left() + x * rect.width() - 23,
                            rect.top() + y * rect.height() - 11, 46, 22)
                for key, (x, y) in self.trigger_anchors().items()}

    def draw_trigger_labels(self, painter):
        labels = axis_labels(self.family)[-2:]
        font = QFont('Segoe UI')
        font.setPixelSize(11)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        selected = getattr(self, 'selected_buttons', set())
        hovered = getattr(self, 'hovered_key', None)
        interactive = getattr(self, 'is_interactive_input', False)
        axes = getattr(self, 'axes', [])
        for index, (key, region) in enumerate(self.trigger_regions().items()):
            if not self.input_is_available(key):
                continue
            amount = max(0., min(1., axes[index + 4])) if len(axes) > index + 4 else 0.
            active = key in selected or amount > .01
            is_hovered = key == hovered
            if not active and not is_hovered and not interactive:
                continue
            if active or is_hovered:
                color = QColor(TOKENS['accent'])
                if is_hovered and not active:
                    color.setAlpha(210)
                painter.setPen(color)
                painter.drawText(region, Qt.AlignCenter, labels[index])
                width = 22 if (key in selected or is_hovered) else 22 * amount
                painter.setPen(QPen(color, 2. if active else 1.5))
                painter.drawLine(QPointF(region.center().x() - width / 2, region.bottom()),
                                 QPointF(region.center().x() + width / 2, region.bottom()))
            else:
                painter.setPen(QColor(168, 162, 158, 105))
                painter.drawText(region, Qt.AlignCenter, labels[index])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        stage = self.product_stage()
        draw_controller_svg(painter, stage, self.family, self.led)
        self.draw_trigger_labels(painter)


class ControllerInput(ControllerPhoto):
    """Live interactive standard XInput diagram with clickable button anchors and glow nodes."""
    button_clicked = Signal(int)
    input_clicked = Signal(str)

    def __init__(self, parent=None):
        self.is_interactive_input = True
        self.hovered_key = None
        super().__init__(parent=parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)
        self.buttons = set()
        self.axes = [0.] * 6
        self.available = None
        self.available_axes = None
        self.selected = None
        self.selected_buttons = set()

    def select_button(self, key):
        self.select_buttons([] if key is None else [key])

    def select_buttons(self, keys):
        normalized = {int(key) if str(key).isdigit() else str(key) for key in keys}
        selected_buttons = {key for key in normalized if key in self.anchors()
                            and self.input_is_available(key)}
        if selected_buttons != self.selected_buttons:
            self.selected_buttons = selected_buttons
            self.selected = min(selected_buttons, key=lambda key: (1, key) if isinstance(key, str) else (0, key)) if selected_buttons else None
            self.update()

    def anchors(self):
        return {**self.button_anchors(), **self.trigger_anchors(), **self.stick_direction_anchors()}

    def button_anchors(self):
        _, _, vw, vh = XINPUT_VIEWBOX
        return {key: (x / vw, y / vh) for key, (x, y) in XINPUT_SVG_CENTERS.items()}

    def update_state(self, state):
        previous_available = self.available
        previous_axes = self.available_axes
        if state:
            self.set_family(state.get('family', 'generic'))
        if state:
            self.available = set(state.get('available_buttons', []))
            available_axes = state.get('available_axes')
            self.available_axes = set(available_axes) if available_axes is not None else set(range(len(state.get('axes', []))))
        buttons = set(state['buttons']) if state else set()
        axes = state['axes'] if state else [0.] * 6
        if (buttons != self.buttons or axes != self.axes
                or previous_available != self.available or previous_axes != self.available_axes):
            self.buttons = buttons
            self.axes = axes[:]
            self.update()
        if state:
            self.select_buttons(self.selected_buttons)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.product_rect()
        if rect.isEmpty():
            return
        active_keys = self.selected_buttons | {k for k in self.buttons if isinstance(k, int)}
        anchors = self.anchors()

        # Subtle interactive shoulder bumper micro-captions (LB / RB) inside the shoulder ridge
        bumper_names = ('L1', 'R1') if self.family in ('dualsense', 'dualshock4', 'stadia') else (
            ('L', 'R') if self.family == 'switch' else ('LB', 'RB'))
        b_font = QFont('Segoe UI')
        b_font.setPixelSize(10)
        b_font.setWeight(QFont.DemiBold)
        painter.setFont(b_font)
        for idx, b_key in enumerate((9, 10)):
            if b_key not in anchors or not self.input_is_available(b_key):
                continue
            bx, by = anchors[b_key]
            b_pt = QPointF(rect.left() + bx * rect.width(), rect.top() + by * rect.height())
            b_rect = QRectF(b_pt.x() - 18, b_pt.y() - 22, 36, 16)
            if b_key in active_keys or b_key == self.hovered_key:
                painter.setPen(QColor(TOKENS['accent']))
            else:
                painter.setPen(QColor(168, 162, 158, 115))
            painter.drawText(b_rect, Qt.AlignCenter, bumper_names[idx])

        radius = max(9., min(20., rect.width() * .026))
        if isinstance(self.hovered_key, int) and self.hovered_key not in active_keys and self.input_is_available(self.hovered_key):
            hx, hy = anchors[self.hovered_key]
            h_stroke = QColor(TOKENS['accent'])
            h_stroke.setAlpha(165)
            h_fill = QColor(TOKENS['accent'])
            h_fill.setAlpha(26)
            painter.setPen(QPen(h_stroke, 1.5))
            painter.setBrush(h_fill)
            painter.drawEllipse(QPointF(rect.left() + hx * rect.width(), rect.top() + hy * rect.height()), radius, radius)

        # Thumbstick rim chips: one chevron per direction, so the eight stick
        # directions are reachable on the art and not only in the list beside it.
        chip = max(4.5, min(9., rect.width() * .014))
        for key, (x, y) in self.stick_direction_anchors().items():
            if not self.input_is_available(key):
                continue
            ux, uy = STICK_DIRECTION_VECTORS[key.split(':')[1]]
            center = QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())
            chosen = key in self.selected_buttons
            color = QColor(TOKENS['accent']) if (chosen or key == self.hovered_key) else QColor(168, 162, 158, 130)
            if chosen or key == self.hovered_key:
                ring = QColor(color)
                ring.setAlpha(60 if chosen else 26)
                painter.setPen(QPen(color, 2. if chosen else 1.5))
                painter.setBrush(ring)
                painter.drawEllipse(center, chip * 1.9, chip * 1.9)
            tip = center + QPointF(ux * chip, uy * chip)
            base = center - QPointF(ux * chip * .6, uy * chip * .6)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([
                tip,
                base + QPointF(-uy * chip * .75, ux * chip * .75),
                base - QPointF(-uy * chip * .75, ux * chip * .75)]))

        if not active_keys:
            return
        stroke = QColor(TOKENS['accent'])
        fill = QColor(stroke)
        fill.setAlpha(48)
        painter.setPen(QPen(stroke, 2.))
        painter.setBrush(fill)
        for key, (x, y) in anchors.items():
            if isinstance(key, int) and key in active_keys and self.input_is_available(key):
                painter.drawEllipse(QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height()), radius, radius)

    def _hit_key_at(self, pos):
        for key, region in self.trigger_regions().items():
            if region.contains(pos):
                return key if self.input_is_available(key) else None
        rect = self.product_rect()

        def distance_squared(point):
            delta = point - pos
            return delta.x() ** 2 + delta.y() ** 2

        def project(anchors):
            return {k: QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())
                    for k, (x, y) in anchors.items()}

        # Rim chips are tested first: the L3 / R3 press anchor keeps a generous radius
        # below, and would otherwise swallow every click aimed at a direction.
        sticks = project(self.stick_direction_anchors())
        if sticks:
            key = min(sticks, key=lambda k: distance_squared(sticks[k]))
            if self.input_is_available(key) and distance_squared(sticks[key]) < max(12., rect.width() * .035) ** 2:
                return key
        points = project(self.button_anchors())
        if not points:
            return None
        key = min(points, key=lambda k: distance_squared(points[k]))
        if self.input_is_available(key) and distance_squared(points[key]) < max(20, rect.width() * .075) ** 2:
            return key
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit_key_at(event.position())
        if hit != self.hovered_key:
            self.hovered_key = hit
            self.setCursor(Qt.PointingHandCursor if hit is not None else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self.hovered_key is not None:
            self.hovered_key = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        hit = self._hit_key_at(event.position())
        if hit is None:
            return
        if isinstance(hit, str):
            self.input_clicked.emit(hit)
        else:
            self.button_clicked.emit(hit)
            self.input_clicked.emit(str(hit))
