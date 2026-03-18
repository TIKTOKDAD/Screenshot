from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPaintEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


@dataclass(slots=True)
class PreviewEditResult:
    pixmap: QPixmap
    has_edit: bool
    crop_rect: QRect | None
    mark_rects: list[QRect]


class PreviewCanvas(QWidget):
    def __init__(
        self,
        pixmap: QPixmap,
        parent: QWidget | None = None,
        initial_crop_rect: QRect | None = None,
        initial_mark_rects: list[QRect] | None = None,
    ) -> None:
        super().__init__(parent)
        self._source = pixmap
        self._mode = "crop"
        self._crop_rect: QRect | None = QRect(initial_crop_rect) if initial_crop_rect is not None else None
        self._mark_rects: list[QRect] = [QRect(rect) for rect in (initial_mark_rects or [])]
        self._active_mark_index: int | None = None

        self._drawing = False
        self._drag_start: QPoint | None = None
        self._drag_current: QPoint | None = None
        self._interaction = "none"
        self._resize_handle: str | None = None
        self._interaction_start: QPoint | None = None
        self._interaction_rect: QRect | None = None

        self._zoom = 1.0
        self._min_zoom = 0.25
        self._max_zoom = 6.0
        self._pan = QPointF(0.0, 0.0)
        self._panning = False
        self._pan_start: QPoint | None = None

        self._min_crop_size = 8
        self._handle_size = 6
        self._handle_hit_radius = 10

        if self._mark_rects:
            self._active_mark_index = len(self._mark_rects) - 1

        self.setMinimumSize(680, 400)
        self.setMouseTracking(True)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in {"crop", "mark"} else "crop"
        self.update()

    def undo_last(self) -> None:
        if self._mode == "crop":
            self._crop_rect = None
        elif self._mark_rects:
            if self._active_mark_index is not None and 0 <= self._active_mark_index < len(self._mark_rects):
                self._mark_rects.pop(self._active_mark_index)
            else:
                self._mark_rects.pop()
            if not self._mark_rects:
                self._active_mark_index = None
            else:
                self._active_mark_index = len(self._mark_rects) - 1
        self.update()

    def clear_all(self) -> None:
        self._crop_rect = None
        self._mark_rects = []
        self._active_mark_index = None
        self.update()

    def has_edit(self) -> bool:
        return self._crop_rect is not None or len(self._mark_rects) > 0

    def build_result(self) -> PreviewEditResult:
        if self._source.isNull():
            return PreviewEditResult(pixmap=QPixmap(), has_edit=False, crop_rect=None, mark_rects=[])

        image = self._source.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        source_bounds = QRect(0, 0, image.width(), image.height())

        crop_rect = source_bounds
        if self._crop_rect is not None:
            crop_rect = self._crop_rect.intersected(source_bounds)
            if crop_rect.width() <= 1 or crop_rect.height() <= 1:
                crop_rect = source_bounds

        result = image.copy(crop_rect)

        painter = QPainter(result)
        pen_width = max(2, min(result.width(), result.height()) // 180)
        pen = QPen(QColor("#d00000"), pen_width)
        painter.setPen(pen)

        for rect in self._mark_rects:
            draw_rect = rect
            if crop_rect != source_bounds:
                draw_rect = draw_rect.translated(-crop_rect.x(), -crop_rect.y())
            draw_rect = draw_rect.intersected(QRect(0, 0, result.width(), result.height()))
            if draw_rect.width() > 1 and draw_rect.height() > 1:
                painter.drawRect(draw_rect)

        painter.end()

        return PreviewEditResult(
            pixmap=QPixmap.fromImage(result),
            has_edit=self.has_edit(),
            crop_rect=QRect(self._crop_rect) if self._crop_rect is not None else None,
            mark_rects=[QRect(rect) for rect in self._mark_rects],
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        _ = event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0f172a"))

        if self._source.isNull():
            return

        display_rect = self._image_display_rect()
        painter.drawPixmap(display_rect, self._source, self._source.rect())

        if self._crop_rect is not None:
            self._draw_overlay_rect(painter, self._crop_rect, QColor("#16a34a"), dashed=True)
            self._draw_crop_handles(painter, self._crop_rect)

        for rect in self._mark_rects:
            self._draw_overlay_rect(painter, rect, QColor("#dc2626"), dashed=False)

        if self._active_mark_index is not None and 0 <= self._active_mark_index < len(self._mark_rects):
            self._draw_box_handles(painter, self._mark_rects[self._active_mark_index], QColor("#dc2626"), QColor("#fee2e2"))

        if self._drawing and self._drag_start is not None and self._drag_current is not None:
            draft = QRect(self._drag_start, self._drag_current).normalized()
            color = QColor("#16a34a") if self._mode == "crop" else QColor("#dc2626")
            self._draw_overlay_rect(painter, draft, color, dashed=True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        point = self._to_image_point(event.position().toPoint())
        if point is None:
            return

        self._interaction = "draw"
        self._resize_handle = None

        if self._mode == "crop" and self._crop_rect is not None:
            handle = self._hit_test_crop_handle(event.position().toPoint())
            if handle is not None:
                self._interaction = "resize_crop"
                self._resize_handle = handle
                self._interaction_start = point
                self._interaction_rect = QRect(self._crop_rect)
                return

            if self._crop_rect.contains(point):
                self._interaction = "move_crop"
                self._interaction_start = point
                self._interaction_rect = QRect(self._crop_rect)
                return

        if self._mode == "mark" and self._mark_rects:
            handle = self._hit_test_mark_handle(event.position().toPoint())
            if handle is not None and self._active_mark_index is not None:
                self._interaction = "resize_mark"
                self._resize_handle = handle
                self._interaction_start = point
                self._interaction_rect = QRect(self._mark_rects[self._active_mark_index])
                return

            mark_index = self._hit_test_mark_rect(point)
            if mark_index is not None:
                self._active_mark_index = mark_index
                self._interaction = "move_mark"
                self._interaction_start = point
                self._interaction_rect = QRect(self._mark_rects[mark_index])
                self.update()
                return

            self._active_mark_index = None

        self._drawing = True
        self._drag_start = point
        self._drag_current = point
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._panning and self._pan_start is not None:
            current = event.position().toPoint()
            delta = current - self._pan_start
            self._pan_start = current
            self._pan = QPointF(self._pan.x() + delta.x(), self._pan.y() + delta.y())
            self._clamp_pan()
            self.update()
            return

        if self._interaction == "move_crop" and self._interaction_start is not None and self._interaction_rect is not None:
            point = self._to_image_point(event.position().toPoint())
            if point is None:
                return
            delta = point - self._interaction_start
            moved = QRect(self._interaction_rect)
            moved.translate(delta)
            self._crop_rect = self._bounded_rect(moved)
            self.update()
            return

        if self._interaction == "resize_crop" and self._interaction_rect is not None and self._resize_handle:
            point = self._to_image_point(event.position().toPoint())
            if point is None:
                return
            resized = self._resize_rect(self._interaction_rect, self._resize_handle, point)
            if resized is not None:
                self._crop_rect = resized
                self.update()
            return

        if self._interaction == "move_mark" and self._interaction_start is not None and self._interaction_rect is not None:
            point = self._to_image_point(event.position().toPoint())
            if point is None:
                return
            if self._active_mark_index is None or not (0 <= self._active_mark_index < len(self._mark_rects)):
                return
            delta = point - self._interaction_start
            moved = QRect(self._interaction_rect)
            moved.translate(delta)
            self._mark_rects[self._active_mark_index] = self._bounded_rect(moved)
            self.update()
            return

        if self._interaction == "resize_mark" and self._interaction_rect is not None and self._resize_handle:
            point = self._to_image_point(event.position().toPoint())
            if point is None:
                return
            if self._active_mark_index is None or not (0 <= self._active_mark_index < len(self._mark_rects)):
                return
            resized = self._resize_rect(self._interaction_rect, self._resize_handle, point)
            if resized is not None:
                self._mark_rects[self._active_mark_index] = resized
                self.update()
            return

        if not self._drawing:
            return
        point = self._to_image_point(event.position().toPoint())
        if point is None:
            return
        self._drag_current = point
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._panning = False
            self._pan_start = None
            self.unsetCursor()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._interaction in {"move_crop", "resize_crop", "move_mark", "resize_mark"}:
            self._interaction = "none"
            self._resize_handle = None
            self._interaction_start = None
            self._interaction_rect = None
            self.update()
            return

        if event.button() != Qt.MouseButton.LeftButton or not self._drawing:
            return

        self._drawing = False
        self._interaction = "none"
        end_point = self._to_image_point(event.position().toPoint())
        if end_point is None or self._drag_start is None:
            self._drag_start = None
            self._drag_current = None
            self.update()
            return

        rect = QRect(self._drag_start, end_point).normalized()
        self._drag_start = None
        self._drag_current = None

        if rect.width() <= 2 or rect.height() <= 2:
            self.update()
            return

        if self._mode == "crop":
            self._crop_rect = self._bounded_rect(rect)
        else:
            self._mark_rects.append(self._bounded_rect(rect))
            self._active_mark_index = len(self._mark_rects) - 1
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0 or self._source.isNull():
            return

        factor = 1.12 if delta > 0 else (1.0 / 1.12)
        old_zoom = self._zoom
        new_zoom = max(self._min_zoom, min(self._max_zoom, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-6:
            return

        cursor_widget = event.position().toPoint()
        old_rect = self._image_display_rect()
        focus_x = self._source.width() / 2.0
        focus_y = self._source.height() / 2.0

        if not old_rect.isNull() and old_rect.contains(cursor_widget):
            rel_x = (cursor_widget.x() - old_rect.x()) / max(1.0, old_rect.width())
            rel_y = (cursor_widget.y() - old_rect.y()) / max(1.0, old_rect.height())
            focus_x = rel_x * self._source.width()
            focus_y = rel_y * self._source.height()

        self._zoom = new_zoom

        draw_w = self._source.width() * self._fit_scale() * self._zoom
        draw_h = self._source.height() * self._fit_scale() * self._zoom
        base_x = (self.width() - draw_w) / 2.0
        base_y = (self.height() - draw_h) / 2.0

        pan_x = cursor_widget.x() - base_x - focus_x * (draw_w / max(1.0, self._source.width()))
        pan_y = cursor_widget.y() - base_y - focus_y * (draw_h / max(1.0, self._source.height()))
        self._pan = QPointF(pan_x, pan_y)
        self._clamp_pan()
        self.update()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._clamp_pan()

    def _fit_scale(self) -> float:
        source_w = self._source.width()
        source_h = self._source.height()
        if source_w <= 0 or source_h <= 0:
            return 1.0
        return min(self.width() / source_w, self.height() / source_h)

    def _image_display_rect(self) -> QRectF:
        source_w = self._source.width()
        source_h = self._source.height()
        if source_w <= 0 or source_h <= 0:
            return QRectF()

        scale = self._fit_scale() * self._zoom
        draw_w = max(1.0, source_w * scale)
        draw_h = max(1.0, source_h * scale)
        x = (self.width() - draw_w) / 2.0 + self._pan.x()
        y = (self.height() - draw_h) / 2.0 + self._pan.y()
        return QRectF(x, y, draw_w, draw_h)

    def _to_image_point(self, widget_point: QPoint) -> QPoint | None:
        display_rect = self._image_display_rect()
        if display_rect.isNull() or not display_rect.contains(QPointF(widget_point)):
            return None

        rel_x = (widget_point.x() - display_rect.x()) / max(1.0, display_rect.width())
        rel_y = (widget_point.y() - display_rect.y()) / max(1.0, display_rect.height())

        img_x = int(rel_x * self._source.width())
        img_y = int(rel_y * self._source.height())

        img_x = max(0, min(self._source.width() - 1, img_x))
        img_y = max(0, min(self._source.height() - 1, img_y))
        return QPoint(img_x, img_y)

    def _draw_overlay_rect(self, painter: QPainter, image_rect: QRect, color: QColor, dashed: bool) -> None:
        display = self._image_rect_to_display_rect(image_rect)
        if display.width() <= 1 or display.height() <= 1:
            return

        width = max(2, int(max(1.0, min(display.width(), display.height()) / 80.0)))
        pen = QPen(color, width)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(display)

    def _draw_crop_handles(self, painter: QPainter, image_rect: QRect) -> None:
        self._draw_box_handles(painter, image_rect, QColor("#16a34a"), QColor("#dcfce7"))

    def _draw_box_handles(self, painter: QPainter, image_rect: QRect, line_color: QColor, fill_color: QColor) -> None:
        painter.save()
        painter.setPen(QPen(line_color, 1))
        painter.setBrush(fill_color)
        for _, point in self._rect_handle_points(image_rect).items():
            rect = QRectF(
                point.x() - self._handle_size,
                point.y() - self._handle_size,
                self._handle_size * 2.0,
                self._handle_size * 2.0,
            )
            painter.drawRect(rect)
        painter.restore()

    def _rect_handle_points(self, image_rect: QRect) -> dict[str, QPointF]:
        display = self._image_rect_to_display_rect(image_rect)
        cx = display.center().x()
        cy = display.center().y()
        return {
            "nw": QPointF(display.left(), display.top()),
            "n": QPointF(cx, display.top()),
            "ne": QPointF(display.right(), display.top()),
            "e": QPointF(display.right(), cy),
            "se": QPointF(display.right(), display.bottom()),
            "s": QPointF(cx, display.bottom()),
            "sw": QPointF(display.left(), display.bottom()),
            "w": QPointF(display.left(), cy),
        }

    def _hit_test_crop_handle(self, widget_point: QPoint) -> str | None:
        if self._crop_rect is None:
            return None
        probe = QPointF(widget_point)
        for name, center in self._rect_handle_points(self._crop_rect).items():
            if abs(probe.x() - center.x()) <= self._handle_hit_radius and abs(probe.y() - center.y()) <= self._handle_hit_radius:
                return name
        return None

    def _hit_test_mark_handle(self, widget_point: QPoint) -> str | None:
        if self._active_mark_index is None or not (0 <= self._active_mark_index < len(self._mark_rects)):
            return None
        rect = self._mark_rects[self._active_mark_index]
        probe = QPointF(widget_point)
        for name, center in self._rect_handle_points(rect).items():
            if abs(probe.x() - center.x()) <= self._handle_hit_radius and abs(probe.y() - center.y()) <= self._handle_hit_radius:
                return name
        return None

    def _hit_test_mark_rect(self, point: QPoint) -> int | None:
        for idx in range(len(self._mark_rects) - 1, -1, -1):
            if self._mark_rects[idx].contains(point):
                return idx
        return None

    def _resize_rect(self, origin: QRect, handle: str, point: QPoint) -> QRect | None:
        left = origin.left()
        right = origin.right()
        top = origin.top()
        bottom = origin.bottom()

        px = point.x()
        py = point.y()

        if "w" in handle:
            left = px
        if "e" in handle:
            right = px
        if "n" in handle:
            top = py
        if "s" in handle:
            bottom = py

        rect = QRect(QPoint(left, top), QPoint(right, bottom)).normalized()
        rect = self._bounded_rect(rect)
        if rect.width() < self._min_crop_size or rect.height() < self._min_crop_size:
            return None
        return rect

    def _bounded_rect(self, rect: QRect) -> QRect:
        bounds = QRect(0, 0, self._source.width(), self._source.height())
        bounded = rect.intersected(bounds)
        if bounded.width() <= 0 or bounded.height() <= 0:
            return QRect(0, 0, 1, 1)
        return bounded

    def _clamp_pan(self) -> None:
        if self._source.isNull():
            self._pan = QPointF(0.0, 0.0)
            return

        draw_w = self._source.width() * self._fit_scale() * self._zoom
        draw_h = self._source.height() * self._fit_scale() * self._zoom

        pan_x = self._pan.x()
        pan_y = self._pan.y()

        if draw_w <= self.width():
            pan_x = 0.0
        else:
            limit_x = (draw_w - self.width()) / 2.0
            pan_x = max(-limit_x, min(limit_x, pan_x))

        if draw_h <= self.height():
            pan_y = 0.0
        else:
            limit_y = (draw_h - self.height()) / 2.0
            pan_y = max(-limit_y, min(limit_y, pan_y))

        self._pan = QPointF(pan_x, pan_y)

    def _image_rect_to_display_rect(self, image_rect: QRect) -> QRectF:
        display_rect = self._image_display_rect()
        if display_rect.isNull() or self._source.width() <= 0 or self._source.height() <= 0:
            return QRectF()

        scale_x = display_rect.width() / self._source.width()
        scale_y = display_rect.height() / self._source.height()

        x = display_rect.x() + image_rect.x() * scale_x
        y = display_rect.y() + image_rect.y() * scale_y
        w = max(1.0, image_rect.width() * scale_x)
        h = max(1.0, image_rect.height() * scale_y)
        return QRectF(x, y, w, h)


class PreviewEditorDialog(QDialog):
    def __init__(
        self,
        pixmap: QPixmap,
        parent: QWidget | None = None,
        initial_crop_rect: QRect | None = None,
        initial_mark_rects: list[QRect] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("截图编辑")
        self.resize(980, 700)

        self._canvas = PreviewCanvas(
            pixmap,
            self,
            initial_crop_rect=initial_crop_rect,
            initial_mark_rects=initial_mark_rects,
        )
        self._result: PreviewEditResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        tip = QLabel(
            "左键拖拽可框选区域；滚轮可缩放，右键拖拽可平移。"
            "裁剪模式下绿色框支持拖动和八方向拉伸调节；红框模式下红框同样支持拖动和八方向拉伸调节。"
        )
        tip.setWordWrap(True)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        toolbar.addWidget(QLabel("编辑模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("裁剪关注区", "crop")
        self.mode_combo.addItem("红框标注", "mark")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        undo_btn = QPushButton("撤销当前模式")
        clear_btn = QPushButton("清空所有标注")
        apply_btn = QPushButton("应用")
        cancel_btn = QPushButton("取消")

        undo_btn.clicked.connect(self._canvas.undo_last)
        clear_btn.clicked.connect(self._canvas.clear_all)
        apply_btn.clicked.connect(self._on_apply)
        cancel_btn.clicked.connect(self.reject)

        toolbar.addWidget(self.mode_combo)
        toolbar.addSpacing(10)
        toolbar.addWidget(undo_btn)
        toolbar.addWidget(clear_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(apply_btn)
        toolbar.addWidget(cancel_btn)

        root.addWidget(tip)
        root.addLayout(toolbar)
        root.addWidget(self._canvas, 1)

    @property
    def result(self) -> PreviewEditResult | None:
        return self._result

    def _on_mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData() or "crop")
        self._canvas.set_mode(mode)

    def _on_apply(self) -> None:
        self._result = self._canvas.build_result()
        self.accept()
