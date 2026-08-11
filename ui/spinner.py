# ui/spinner.py
#
# SpinnerWidget — animated arc + status text.
# Call start(text) to show; stop() to hide.

from PySide6.QtWidgets import QWidget, QSizePolicy
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QPen, QColor


class SpinnerWidget(QWidget):

    def __init__(self, color: QColor | None = None, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._text  = ""
        self._color = color or QColor(90, 170, 255)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(30)
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setVisible(False)

    def start(self, text: str = ""):
        self._text = text
        self.setVisible(True)
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.setVisible(False)

    def _tick(self):
        self._angle = (self._angle + 8) % 360
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r    = (self.height() - 4) // 2
        cx   = r + 2
        cy   = self.height() // 2
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)

        pen = QPen(QColor(220, 220, 220), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawEllipse(rect)

        pen = QPen(self._color, 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int((90 - self._angle) * 16), int(-270 * 16))

        if self._text:
            p.setPen(QColor(130, 130, 130))
            tx = cx + r + 7
            p.drawText(tx, 0, self.width() - tx - 2, self.height(),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self._text)
