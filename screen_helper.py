"""
Учебный помощник на Gemini.

F12 - скриншот всего экрана -> по центру полупрозрачная серая панель с объяснением всех тем на экране
F9  - выделяешь область мышкой -> рядом с выделенным всплывает объяснение только по ней
В панели внизу есть поле: можно задать Gemini уточняющий вопрос (Enter - отправить).
Esc / крестик - закрыть

Установка:
    pip install PySide6 pynput google-genai
Запуск:
    python screen_helper.py
"""
import sys
import threading

from pynput import keyboard
from PySide6.QtCore import QBuffer, QIODevice, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget,
)
from google import genai
from google.genai import types

from config import API_KEY, MODEL  # config.py лежит рядом, в git не попадает


SYSTEM = "Ты репетитор для студента. Отвечай по-русски, простым языком, по делу. Используй markdown."
PROMPT_FULL = (
    "Это скриншот экрана студента. Найди все учебные темы, понятия, задачи и текст на экране "
    "и по очереди объясни каждую тему простым языком на русском. Если есть задание - объясни "
    "ход решения и дай ответ. Используй короткие абзацы и markdown-заголовки. "
    "Не описывай интерфейс программ, только учебное содержимое."
)
PROMPT_REGION = (
    "Это выделенный фрагмент экрана студента. Объясни только то, что на нём, простым языком "
    "на русском, кратко и по делу. Если это задача - дай решение и ответ."
)

client = genai.Client(api_key=API_KEY) if API_KEY else None


def pixmap_to_png(pm):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    pm.save(buf, "PNG")
    return bytes(buf.data())


class DragBar(QLabel):
    """Полоска сверху панели: потяни мышкой, чтобы переместить окно."""

    def __init__(self, get_pos, move_to):
        super().__init__("⠿  потяни, чтобы переместить")
        self.get_pos, self.move_to = get_pos, move_to
        self._press = None
        self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, e):
        self._press = e.globalPosition().toPoint()
        self._origin = self.get_pos()

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self.move_to(self._origin + e.globalPosition().toPoint() - self._press)

    def mouseReleaseEvent(self, e):
        self._press = None


# ---------------------------------------------------------------- окно с ответом и чатом
class ResultOverlay(QWidget):
    """full=True: затемнённый экран + панель по центру.
    full=False: только панель рядом с выделением."""
    ask = Signal(str)

    def __init__(self, screen_rect, full, anchor=None):
        super().__init__()
        self.full = full
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        panel_w, panel_h = (
            (min(900, int(screen_rect.width() * 0.7)), int(screen_rect.height() * 0.75))
            if full else (460, 400)
        )

        self.panel = QFrame(self)
        self.panel.setStyleSheet(
            "QFrame { background-color: rgba(55,55,58,215); border-radius: 14px; }"
            "QLabel { background: transparent; color: rgba(255,255,255,130); font-size: 12px; }"
            "QTextBrowser { background: transparent; color: #f2f2f2; border: none; font-size: 15px; }"
            "QLineEdit { background: rgba(255,255,255,30); color: white; border: none;"
            " border-radius: 8px; padding: 7px 10px; font-size: 14px; }"
            "QPushButton { background: rgba(255,255,255,40); color: white; border: none;"
            " border-radius: 10px; padding: 4px 10px; }"
            "QPushButton:hover { background: rgba(255,255,255,80); }"
        )
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(16, 10, 16, 14)

        top = QHBoxLayout()
        top.addWidget(DragBar(
            get_pos=lambda: self.panel.pos() if self.full else self.pos(),
            move_to=lambda p: self.panel.move(p) if self.full else self.move(p),
        ), 1)
        close = QPushButton("✕")
        close.clicked.connect(self.close)
        top.addWidget(close)
        lay.addLayout(top)

        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        lay.addWidget(self.text)

        bottom = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Не понял? Задай вопрос и нажми Enter…")
        self.input.returnPressed.connect(self._send)
        send = QPushButton("➤")
        send.clicked.connect(self._send)
        bottom.addWidget(self.input)
        bottom.addWidget(send)
        lay.addLayout(bottom)

        if full:
            self.setGeometry(screen_rect)
            self.panel.setGeometry(
                (screen_rect.width() - panel_w) // 2,
                (screen_rect.height() - panel_h) // 2,
                panel_w, panel_h,
            )
        else:
            x = anchor.right() + 12
            if x + panel_w > screen_rect.right():
                x = max(screen_rect.left() + 4, anchor.left() - panel_w - 12)
            y = min(max(screen_rect.top() + 4, anchor.top()), screen_rect.bottom() - panel_h - 4)
            self.setGeometry(x, y, panel_w, panel_h)
            self.panel.setGeometry(0, 0, panel_w, panel_h)

    def _send(self):
        q = self.input.text().strip()
        if q and self.input.isEnabled():
            self.input.clear()
            self.ask.emit(q)

    def set_markdown(self, md):
        self.text.setMarkdown(md)
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_busy(self, busy):
        self.input.setEnabled(not busy)
        if not busy:
            self.input.setFocus()

    def paintEvent(self, _):
        if self.full:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(0, 0, 0, 110))

    def mousePressEvent(self, e):
        if self.full and not self.panel.geometry().contains(e.pos()):
            self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()


# ---------------------------------------------------------------- выбор области
class Selector(QWidget):
    done = Signal(QRect)
    cancelled = Signal()

    def __init__(self, pixmap, screen_rect):
        super().__init__()
        self.pm = pixmap
        self.start = None
        self.sel = QRect()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen_rect)

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pm)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if not self.sel.isNull():
            dpr = self.pm.devicePixelRatio()
            src = QRect(int(self.sel.x() * dpr), int(self.sel.y() * dpr),
                        int(self.sel.width() * dpr), int(self.sel.height() * dpr))
            p.drawPixmap(self.sel, self.pm, src)
            p.setPen(QPen(QColor(120, 180, 255), 2))
            p.drawRect(self.sel)

    def mousePressEvent(self, e):
        self.start = e.pos()
        self.sel = QRect(self.start, self.start)

    def mouseMoveEvent(self, e):
        if self.start:
            self.sel = QRect(self.start, e.pos()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        r = self.sel
        self.close()
        if r.width() > 10 and r.height() > 10:
            self.done.emit(r)
        else:
            self.cancelled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            self.cancelled.emit()


# ---------------------------------------------------------------- логика
class Controller(QObject):
    full_hotkey = Signal()
    region_hotkey = Signal()
    answer = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.overlay = None
        self.selector = None
        self.chat = None
        self.turns = []
        self.job = 0
        self.full_hotkey.connect(self.start_full)
        self.region_hotkey.connect(self.start_region)
        self.answer.connect(self.on_answer)

    def screen(self):
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def close_overlay(self):
        if self.overlay:
            self.overlay.close()
            self.overlay = None
        self.chat = None
        self.turns = []

    def grab(self):
        scr = self.screen()
        return scr.geometry(), scr.grabWindow(0)

    def open_overlay(self, rect, full, anchor=None):
        self.chat = client.chats.create(
            model=MODEL, config=types.GenerateContentConfig(system_instruction=SYSTEM))
        self.turns = []
        self.overlay = ResultOverlay(QRect(rect), full=full, anchor=anchor)
        self.overlay.ask.connect(self.on_question)
        self.overlay.show()
        self.overlay.activateWindow()

    # --- F12
    def start_full(self):
        self.close_overlay()
        QTimer.singleShot(150, self._do_full)

    def _do_full(self):
        rect, pm = self.grab()
        self.open_overlay(rect, full=True)
        self.send([types.Part.from_bytes(data=pixmap_to_png(pm), mime_type="image/png"),
                   PROMPT_FULL])

    # --- F9
    def start_region(self):
        self.close_overlay()
        QTimer.singleShot(150, self._do_region)

    def _do_region(self):
        rect, pm = self.grab()
        self.selector = Selector(pm, QRect(rect))
        self.selector.done.connect(lambda r: self.region_selected(rect, pm, r))
        self.selector.show()
        self.selector.activateWindow()

    def region_selected(self, rect, pm, sel):
        dpr = pm.devicePixelRatio()
        crop = pm.copy(QRect(int(sel.x() * dpr), int(sel.y() * dpr),
                             int(sel.width() * dpr), int(sel.height() * dpr)))
        self.open_overlay(rect, full=False, anchor=QRect(sel.translated(rect.topLeft())))
        self.send([types.Part.from_bytes(data=pixmap_to_png(crop), mime_type="image/png"),
                   PROMPT_REGION])

    # --- чат
    def on_question(self, q):
        self.turns.append(f"**Ты:** {q}")
        self.send(q)

    def render(self, pending):
        md = "\n\n---\n\n".join(self.turns)
        if pending:
            md += ("\n\n" if md else "") + "*Думаю…*"
        self.overlay.set_markdown(md)

    def send(self, message):
        self.job += 1
        job, chat = self.job, self.chat
        self.overlay.set_busy(True)
        self.render(pending=True)

        def work():
            try:
                text = chat.send_message(message).text or "(пустой ответ)"
            except Exception as ex:  # noqa: BLE001
                text = f"**Ошибка:** {ex}"
            self.answer.emit(job, text)

        threading.Thread(target=work, daemon=True).start()

    def on_answer(self, job, text):
        if job == self.job and self.overlay:
            self.turns.append(text)
            self.render(pending=False)
            self.overlay.set_busy(False)


def main():
    if not API_KEY or API_KEY == "your_api_token":
        print("Впиши ключ в config.py (API_KEY)")
        sys.exit(1)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ctl = Controller()
    hk = keyboard.GlobalHotKeys({
        "<f12>": ctl.full_hotkey.emit,
        "<f9>": ctl.region_hotkey.emit,
    })
    hk.start()
    print("Готово. F12 - весь экран, F9 - выделить область. Ctrl+C в консоли - выход.")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()