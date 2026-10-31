from __future__ import annotations
import math

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QPushButton, QTabWidget, QMessageBox, QComboBox,
)
from play_stats import PlayStatsTracker

# Series colours, used in this fixed order and never cycled (the chart caps at
# 8 series). One set for dark surfaces, one for light.
_SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500",
                "#d55181", "#008300", "#9085e9", "#e66767")
_SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                 "#e87ba4", "#008300", "#4a3aa7", "#e34948")
_DAY_STEPS = (1, 2, 3, 7, 14, 30, 60, 90, 180, 365, 730)


def _fmt_duration(ms: int) -> str:
    """Elapsed time in its two largest units, from seconds up to years (listening totals run for months)."""
    total_s = ms // 1000
    years, rem = divmod(total_s, 31_536_000)     # 365-day year
    months, rem = divmod(rem, 2_592_000)         # 30-day month
    days, rem = divmod(rem, 86_400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)

    if years:
        return f"{years}y {months}mo"
    if months:
        return f"{months}mo {days}d"
    if days:
        return f"{days}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _nice_ticks(vmax: float, target: int, integer: bool) -> tuple[float, int]:
    """Round step and tick count so 0..step*count just covers vmax."""
    raw = vmax / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    if integer:
        step = max(1, step)
    return step, max(1, math.ceil(vmax / step - 1e-9))


class _HistoryChart(QWidget):
    """Cumulative multi-line chart of per-day history, painted by hand.

    Colour follows the series name, not its rank, so a line keeps its colour
    when the range or the ranking changes.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(160)
        self._dates: list = []
        self._series: list[tuple[str, list[float]]] = []
        self._measure = "plays"
        self._empty_msg = ""
        self._slots: dict[str, int] = {}   # series name -> palette slot
        self._hover: tuple[int, QPointF] | None = None
        self._plot = QRectF()

    def set_data(self, dates, series, measure: str, empty_msg: str):
        self._dates, self._series = dates, series
        self._measure, self._empty_msg = measure, empty_msg
        self._hover = None
        # A name keeps its slot; a new one takes the lowest slot nobody holds.
        # Only when all 8 are held does it take over one from a name that is
        # not on screen right now.
        shown = {name for name, _ in series}
        for name, _ in series:
            if name in self._slots:
                continue
            held = set(self._slots.values())
            live = {self._slots[n] for n in shown if n in self._slots}
            slot = next((i for i in range(8) if i not in held),
                        next(i for i in range(8) if i not in live))
            for stale in [n for n, i in self._slots.items() if i == slot]:
                del self._slots[stale]
            self._slots[name] = slot
        self.update()

    # drawing helpers
    def _ink(self, alpha: int = 255) -> QColor:
        c = QColor(self.palette().text().color())
        c.setAlpha(alpha)
        return c

    def _fmt_value(self, v: float) -> str:
        return f"{int(v):,}" if self._measure == "plays" else _fmt_duration(int(v * 60000))

    def _fmt_day(self, d, full: bool = False) -> str:
        if full:
            return f"{d:%b} {d.day}, {d.year}"
        return f"{d:%b %Y}" if len(self._dates) > 366 else f"{d:%b} {d.day}"

    def _x(self, i: int) -> float:
        return self._plot.left() + i * self._plot.width() / max(1, len(self._dates) - 1)

    def _y(self, v: float) -> float:
        return self._plot.bottom() - v / self._div / self._ymax * self._plot.height()

    def _layout(self):
        fm = self.fontMetrics()
        w, h = self.width(), self.height()
        n = len(self._series)
        vmax = max(v[-1] for _, v in self._series)
        timed = self._measure == "time"
        self._div = 60 if timed and vmax >= 120 else 1   # minutes, or hours once past two
        unit = " h" if self._div == 60 else " min"

        self._row_h = fm.height() + 4
        self._cols = 2 if n > 1 and (w >= 520 or n * self._row_h > h - 130) else 1
        self._rows = -(-n // self._cols)
        legend_h = self._rows * self._row_h + 8

        day_w = max(fm.horizontalAdvance(self._fmt_day(d)) for d in (self._dates[0], self._dates[-1]))
        top = fm.height() // 2 + 4
        bottom = h - legend_h - fm.height() - 10
        plot_h = max(20, bottom - top)

        step, count = _nice_ticks(vmax / self._div, max(2, min(6, int(plot_h // 40))), not timed)
        self._ymax = step * count
        self._yticks = [(i * step, f"{i * step:g}{unit}" if timed and i else f"{i * step:,.0f}")
                        for i in range(count + 1)]
        left = max(fm.horizontalAdvance(t) for _, t in self._yticks) + 14
        right = day_w // 2 + 4
        self._plot = QRectF(left, top, max(20, w - left - right), plot_h)

        fit = max(2, min(6, int(self._plot.width() // (day_w + 18))))
        last = len(self._dates) - 1
        every = next((c for c in _DAY_STEPS if last // c + 1 <= fit), _DAY_STEPS[-1])
        self._xticks = list(range(last, -1, -every))   # anchored on today

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self.palette().base())
        if not self._series:
            p.setPen(self._ink(150))
            p.drawText(self.rect().adjusted(16, 16, -16, -16),
                       Qt.AlignCenter | Qt.TextWordWrap, self._empty_msg)
            return
        self._layout()
        fm = self.fontMetrics()
        w = self.width()
        plot = self._plot
        colors = [QColor(c) for c in (
            _SERIES_DARK if self.palette().base().color().lightness() < 128 else _SERIES_LIGHT)]
        series = [(name, vals, colors[self._slots[name]]) for name, vals in self._series]

        # grid and axes
        for v, text in self._yticks:
            y = self._y(v * self._div)
            p.setPen(QPen(self._ink(90 if v == 0 else 36), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(self._ink(150))
            p.drawText(QRectF(0, y - fm.height() / 2, plot.left() - 8, fm.height()),
                       Qt.AlignRight | Qt.AlignVCenter, text)
        for i in self._xticks:
            x = self._x(i)
            p.setPen(QPen(self._ink(90), 1))
            p.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() + 3))
            text = self._fmt_day(self._dates[i])
            tw = fm.horizontalAdvance(text)
            p.setPen(self._ink(150))
            p.drawText(QPointF(min(max(x - tw / 2, 2), w - 2 - tw),
                               plot.bottom() + 5 + fm.ascent()), text)

        # lines, best series last so it sits on top
        for name, vals, color in reversed(series):
            path = QPainterPath(QPointF(self._x(0), self._y(vals[0])))
            for i in range(1, len(vals)):
                path.lineTo(self._x(i), self._y(vals[i]))
            p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        self._paint_legend(p, fm, series)
        if self._hover is not None:
            self._paint_hover(p, fm, series)

    def _paint_legend(self, p, fm, series):
        # With one series the legend is the only place its name shows up.
        vals_w = max(fm.horizontalAdvance(self._fmt_value(v[-1])) for _, v, _ in series)
        names_w = max(fm.horizontalAdvance(name) for name, _, _ in series)
        cell_w = min(self.width() / self._cols, 52 + names_w + vals_w)   # columns pack to their content
        top = self.height() - self._rows * self._row_h - 4
        for j, (name, vals, color) in enumerate(series):
            col, row = divmod(j, self._rows)
            x = 12 + col * cell_w
            y = top + row * self._row_h
            mid = y + self._row_h / 2
            p.setPen(QPen(color, 3, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x + 2, mid), QPointF(x + 12, mid))
            p.setPen(self._ink())
            name_w = cell_w - 52 - vals_w
            p.drawText(QRectF(x + 20, y, name_w, self._row_h), Qt.AlignVCenter,
                       fm.elidedText(name, Qt.ElideRight, int(name_w)))
            p.setPen(self._ink(150))
            p.drawText(QRectF(x + cell_w - 24 - vals_w, y, vals_w, self._row_h),
                       Qt.AlignRight | Qt.AlignVCenter, self._fmt_value(vals[-1]))

    def _paint_hover(self, p, fm, series):
        i, cursor = self._hover
        plot = self._plot
        x = self._x(i)
        p.setPen(QPen(self._ink(110), 1))
        p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        base = self.palette().base()
        p.setPen(Qt.NoPen)
        for _, vals, color in series:
            c = QPointF(x, self._y(vals[i]))
            p.setBrush(base)
            p.drawEllipse(c, 7, 7)   # surface ring around the dot
            p.setBrush(color)
            p.drawEllipse(c, 5, 5)

        rows = sorted(((vals[i], name, color) for name, vals, color in series), key=lambda r: -r[0])
        bold = QFont(self.font())
        bold.setBold(True)
        bfm = QFontMetrics(bold)
        pad, row_h = 8, self._row_h
        val_w = max(bfm.horizontalAdvance(self._fmt_value(r[0])) for r in rows)
        name_w = min(max(fm.horizontalAdvance(r[1]) for r in rows), 200)
        head = self._fmt_day(self._dates[i], full=True)
        box_w = 2 * pad + max(fm.horizontalAdvance(head), 14 + val_w + 8 + name_w)
        box_h = 2 * pad + (len(rows) + 1) * row_h
        bx, by = cursor.x() + 14, cursor.y() + 14
        if bx + box_w > self.width() - 2:
            bx = cursor.x() - 14 - box_w
        if by + box_h > self.height() - 2:
            by = cursor.y() - 14 - box_h
        bx = min(max(bx, 2), self.width() - 2 - box_w)
        by = min(max(by, 2), self.height() - 2 - box_h)

        box = QRectF(bx, by, box_w, box_h)
        p.setBrush(base)
        p.drawRoundedRect(box, 4, 4)
        p.setBrush(self._ink(18))
        p.setPen(QPen(self._ink(70), 1))
        p.drawRoundedRect(box, 4, 4)
        y = by + pad
        p.setPen(self._ink())
        p.drawText(QRectF(bx + pad, y, box_w - 2 * pad, row_h), Qt.AlignVCenter, head)
        for value, name, color in rows:
            y += row_h
            mid = y + row_h / 2
            p.setPen(QPen(color, 3, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(bx + pad + 2, mid), QPointF(bx + pad + 10, mid))
            p.setFont(bold)
            p.setPen(self._ink())
            p.drawText(QRectF(bx + pad + 14, y, val_w, row_h),
                       Qt.AlignRight | Qt.AlignVCenter, self._fmt_value(value))
            p.setFont(self.font())
            p.setPen(self._ink(170))
            p.drawText(QRectF(bx + pad + 14 + val_w + 8, y, name_w, row_h), Qt.AlignVCenter,
                       fm.elidedText(name, Qt.ElideRight, name_w))

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if self._series and self._plot.adjusted(-6, -6, 6, 6).contains(pos):
            last = len(self._dates) - 1
            i = round((pos.x() - self._plot.left()) / self._plot.width() * last)
            self._hover = (min(max(i, 0), last), pos)
        else:
            self._hover = None
        self.update()

    def leaveEvent(self, _):
        self._hover = None
        self.update()


class StatsWidget(QWidget):
    """Top tracks, top artists, total time and an over-time chart per artist or track.

    Reads PlayStatsTracker, which keeps everything in a local JSON file in the config folder.
    """

    def __init__(self, stats: PlayStatsTracker, parent=None):
        super().__init__(parent)
        self._stats = stats
        self._build()
        stats.stats_changed.connect(self._refresh)
        self._refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._summary = QLabel("")
        self._summary.setObjectName("track_sub")
        self._summary.setWordWrap(True)
        root.addWidget(self._summary)

        self._tabs = QTabWidget()
        self._tracks_list = QListWidget()
        self._tracks_list.setAlternatingRowColors(True)
        self._artists_list = QListWidget()
        self._artists_list.setAlternatingRowColors(True)
        self._tabs.addTab(self._tracks_list, "Top tracks")
        self._tabs.addTab(self._artists_list, "Top artists")

        # The chart re-reads the history only while its tab is showing, at most
        # every 2 s while music plays, and straight away on a tab or combo change.
        self._over_time = QWidget()
        col = QVBoxLayout(self._over_time)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(6)
        controls = QHBoxLayout()
        self._view, self._measure, self._range = QComboBox(), QComboBox(), QComboBox()
        for combo, tip, items in (
            (self._view, "View", (("Artists", "artist"), ("Tracks", "track"))),
            (self._measure, "Measure", (("Plays", "plays"), ("Listening time", "time"))),
            (self._range, "Range", (("7 days", 7), ("30 days", 30), ("90 days", 90),
                                    ("1 year", 365), ("All time", None))),
        ):
            for text, data in items:
                combo.addItem(text, data)
            combo.setToolTip(tip)
            controls.addWidget(combo)
        self._range.setCurrentIndex(1)
        controls.addStretch()
        col.addLayout(controls)
        self._chart = _HistoryChart()
        col.addWidget(self._chart, 1)
        self._tabs.addTab(self._over_time, "Over time")
        for combo in (self._view, self._measure, self._range):
            combo.currentIndexChanged.connect(self._refresh_chart)
        self._chart_timer = QTimer(self)
        self._chart_timer.setSingleShot(True)
        self._chart_timer.setInterval(2000)
        self._chart_timer.timeout.connect(self._refresh_chart)
        self._tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self._tabs, 1)

        row = QHBoxLayout()
        row.addStretch()
        clear_btn = QPushButton("Clear stats…")
        clear_btn.setFlat(True)
        clear_btn.clicked.connect(self._on_clear)
        row.addWidget(clear_btn)
        root.addLayout(row)

    def _refresh(self, *_):
        total = self._stats.total_listened_ms()
        n = self._stats.track_count()
        self._summary.setText(
            f"{_fmt_duration(total)} total listening time, {n} track{'s' if n != 1 else ''} tracked"
        )

        self._tracks_list.clear()
        for i, e in enumerate(self._stats.top_tracks(100), start=1):
            label = f"{e['artist']} - {e['title']}" if e["artist"] else e["title"]
            plays = e["play_count"]
            item = QListWidgetItem(f"{i:>3}.  {label}  ({plays} play{'s' if plays != 1 else ''})")
            self._tracks_list.addItem(item)

        self._artists_list.clear()
        for i, e in enumerate(self._stats.top_artists(100), start=1):
            plays = e["play_count"]
            item = QListWidgetItem(
                f"{i:>3}.  {e['artist']}  ({plays} play{'s' if plays != 1 else ''}, {_fmt_duration(e['listened_ms'])})"
            )
            self._artists_list.addItem(item)

        if self._chart_showing() and not self._chart_timer.isActive():
            self._chart_timer.start()

    def _chart_showing(self) -> bool:
        return self.isVisible() and self._tabs.currentWidget() is self._over_time

    def _on_tab(self, _):
        if self._chart_showing():
            self._refresh_chart()

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._chart_showing():
            self._refresh_chart()

    def _refresh_chart(self, *_):
        self._chart_timer.stop()
        measure = self._measure.currentData()
        dates, series = self._stats.history(self._view.currentData(), measure,
                                            self._range.currentData())
        self._chart.set_data(dates, series, measure, (
            "No history yet. Plays are charted from today onwards." if self._stats.enabled
            else "Play stats are off. Turn them on in Settings to chart your listening."))

    def _on_clear(self):
        if QMessageBox.question(
            self, "Clear listening stats",
            "Permanently delete all locally tracked play counts and listening time?"
        ) == QMessageBox.Yes:
            self._stats.clear()
            self._refresh_chart()
