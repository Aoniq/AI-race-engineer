"""
Box-Box in-game team-radio overlay (PC).

Draws the /overlay page in a transparent, click-through window on top of F1 25, plays the
engineer's voice, and lets you talk back with a keyboard key or a button on your wheel —
while the game keeps focus.

  python overlay.py                 # start (the Box-Box server must be running)
  python overlay.py --list-buttons  # find the number of a wheel/controller button
  python overlay.py --demo          # sample messages, to check the position

Run F1 25 in "Borderless" / windowed display mode: an exclusive-fullscreen game hides every
window on top of it. The overlay is a separate window; it does not touch the game process.

Settings come from .env (see .env.example, section "Radio overlay").
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser
from urllib.parse import urlencode

from dotenv import load_dotenv

load_dotenv()

PORT = int(os.getenv("PORT", "8000"))
BASE_URL = os.getenv("BOXBOX_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
PTT_KEY = os.getenv("OVERLAY_PTT_KEY", "F9").strip()
PTT_BUTTON = os.getenv("OVERLAY_PTT_BUTTON", "").strip()        # e.g. "5"
PTT_DEVICE = os.getenv("OVERLAY_PTT_DEVICE", "").strip().lower()  # part of the wheel's name
SCREEN_INDEX = int(os.getenv("OVERLAY_SCREEN", "0") or 0)

IS_WINDOWS = sys.platform == "win32"


def overlay_url(demo: bool = False) -> str:
  params = {
    "pos": os.getenv("OVERLAY_POSITION", "bottom-left"),
    "x": os.getenv("OVERLAY_X", ""),
    "y": os.getenv("OVERLAY_Y", ""),
    "scale": os.getenv("OVERLAY_SCALE", ""),
    "audio": "1" if os.getenv("OVERLAY_AUDIO", "1") != "0" else "0",
    "name": os.getenv("OVERLAY_DRIVER_NAME", ""),
    "accent": os.getenv("OVERLAY_ACCENT", "").lstrip("#"),
    "linger": os.getenv("OVERLAY_LINGER", ""),
    "engineer": os.getenv("OVERLAY_ENGINEER_NAME", ""),
  }
  if demo:
    params["demo"] = "1"
  return f"{BASE_URL}/overlay?" + urlencode({k: v for k, v in params.items() if v})


# ---------------------------------------------------------------------------------------
# Push-to-talk inputs (run in their own threads, report through callbacks)
# ---------------------------------------------------------------------------------------

_VK_NAMED = {
  **{f"F{i}": 0x6F + i for i in range(1, 25)},
  **{f"NUMPAD{i}": 0x60 + i for i in range(10)},
  "SPACE": 0x20, "TAB": 0x09, "CAPSLOCK": 0x14, "INSERT": 0x2D, "DELETE": 0x2E,
  "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22, "PAUSE": 0x13,
  "SCROLLLOCK": 0x91, "MULTIPLY": 0x6A, "ADD": 0x6B, "SUBTRACT": 0x6D, "DIVIDE": 0x6F,
  "DECIMAL": 0x6E,
}
_MODS = {"ALT": 0x0001, "CTRL": 0x0002, "CONTROL": 0x0002, "SHIFT": 0x0004, "WIN": 0x0008}


def parse_hotkey(spec: str) -> tuple[int, int]:
  """'ctrl+shift+F9' -> (modifier flags, virtual-key code) for RegisterHotKey."""
  mods, vk = 0, None
  for part in [p.strip().upper() for p in spec.replace("-", "+").split("+") if p.strip()]:
    if part in _MODS:
      mods |= _MODS[part]
    elif part in _VK_NAMED:
      vk = _VK_NAMED[part]
    elif len(part) == 1 and part.isalnum():
      vk = ord(part)
    else:
      raise ValueError(f"Unknown key '{part}' in OVERLAY_PTT_KEY={spec!r}")
  if vk is None:
    raise ValueError(f"OVERLAY_PTT_KEY={spec!r} has no key, only modifiers")
  return mods, vk


def run_hotkey(spec: str, on_down, on_up):
  """Global hotkey (Windows): works while the game has focus. Hold to talk, tap to toggle."""
  if not IS_WINDOWS:
    print("[overlay] Global push-to-talk key is Windows-only; use a wheel button or the dashboard.")
    return
  import ctypes
  from ctypes import wintypes
  user32 = ctypes.windll.user32
  try:
    mods, vk = parse_hotkey(spec)
  except ValueError as e:
    print(f"[overlay] {e}")
    return
  MOD_NOREPEAT, WM_HOTKEY = 0x4000, 0x0312
  if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
    print(f"[overlay] Couldn't claim push-to-talk key {spec} — another program uses it. "
          f"Pick another with OVERLAY_PTT_KEY in .env.")
    return
  print(f"[overlay] Push-to-talk key: {spec} (hold to talk, or tap to start and tap again to send)")
  msg = wintypes.MSG()
  while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
    if msg.message == WM_HOTKEY:
      on_down()
      while user32.GetAsyncKeyState(vk) & 0x8000:   # wait for release
        time.sleep(0.02)
      on_up()


def run_wheel_button(button: int, device_filter: str, on_down, on_up, list_only: bool = False):
  """A button on a wheel, button box or controller (any device SDL can see)."""
  os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")  # keep working when the game has focus
  os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
  os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
  os.environ.setdefault("SDL_NO_SIGNAL_HANDLERS", "1")  # leave Ctrl+C to Python
  try:
    import pygame
  except ImportError:
    print("[overlay] Wheel button needs pygame: pip install -r requirements-overlay.txt")
    return
  pygame.display.init()
  pygame.joystick.init()
  sticks = {}

  def attach(index):
    js = pygame.joystick.Joystick(index)
    sticks[js.get_instance_id()] = js
    print(f"[overlay] Controller found: {js.get_name()} ({js.get_numbuttons()} buttons)")

  for i in range(pygame.joystick.get_count()):
    attach(i)
  if list_only:
    print("Press buttons on your wheel or controller (Ctrl+C to stop)…")
  elif not sticks:
    print("[overlay] No wheel/controller found yet — plug it in, it will be picked up.")

  while True:
    for ev in pygame.event.get():
      if ev.type == pygame.QUIT:
        return
      if ev.type == pygame.JOYDEVICEADDED:
        attach(ev.device_index)
      elif ev.type == pygame.JOYDEVICEREMOVED:
        sticks.pop(ev.instance_id, None)
      elif ev.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP):
        js = sticks.get(ev.instance_id)
        name = js.get_name() if js else "?"
        if list_only:
          if ev.type == pygame.JOYBUTTONDOWN:
            print(f"  {name}: button {ev.button}   ->  OVERLAY_PTT_BUTTON={ev.button}"
                  f"  OVERLAY_PTT_DEVICE={name.split()[0]}")
          continue
        if ev.button != button or (device_filter and device_filter not in name.lower()):
          continue
        (on_down if ev.type == pygame.JOYBUTTONDOWN else on_up)()
    time.sleep(0.01)


# ---------------------------------------------------------------------------------------
# The overlay window
# ---------------------------------------------------------------------------------------

def main():
  ap = argparse.ArgumentParser(description="Box-Box team-radio overlay")
  ap.add_argument("--demo", action="store_true", help="loop sample messages to check the position")
  ap.add_argument("--list-buttons", action="store_true", help="show button numbers of your wheel")
  ap.add_argument("--url", help="overlay URL (default: built from .env)")
  args = ap.parse_args()

  if args.list_buttons:
    try:
      run_wheel_button(-1, "", lambda: None, lambda: None, list_only=True)
    except KeyboardInterrupt:
      pass
    return

  try:
    from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
    from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
  except ImportError:
    print("The overlay needs PyQt6 and PyQt6-WebEngine:\n  pip install -r requirements-overlay.txt")
    sys.exit(1)

  # Chromium: play the radio without a click, and don't throttle while in the background
  os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                        "--autoplay-policy=no-user-gesture-required --disable-background-timer-throttling")

  app = QApplication(sys.argv)
  app.setQuitOnLastWindowClosed(False)
  app.setApplicationName("Box-Box radio overlay")

  class Bridge(QObject):
    down = pyqtSignal()
    up = pyqtSignal()

  bridge = Bridge()

  class OverlayPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
      if level != QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
        print(f"[overlay page] {message}")

  view = QWebEngineView()
  page = OverlayPage(view)
  view.setPage(page)
  page.setBackgroundColor(QColor(0, 0, 0, 0))
  page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)

  # Microphone for push-to-talk: allowed for the Box-Box page only, nothing else
  base_host = QUrl(BASE_URL).host()

  def ours(qurl) -> bool:
    return qurl.host() in (base_host, "127.0.0.1", "localhost")

  def grant(*a):
    if len(a) == 1:                          # Qt >= 6.8: QWebEnginePermission
      perm = a[0]
      from PyQt6.QtWebEngineCore import QWebEnginePermission
      mic = perm.permissionType() == QWebEnginePermission.PermissionType.MediaAudioCapture
      perm.grant() if (mic and ours(perm.origin())) else perm.deny()
    else:                                    # older Qt: (url, feature)
      url, feature = a
      mic = feature == QWebEnginePage.Feature.MediaAudioCapture
      policy = (QWebEnginePage.PermissionPolicy.PermissionGrantedByUser if (mic and ours(url))
                else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
      page.setFeaturePermission(url, feature, policy)
  if hasattr(page, "permissionRequested"):
    page.permissionRequested.connect(grant)
  else:
    page.featurePermissionRequested.connect(grant)

  flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
           | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
           | Qt.WindowType.WindowDoesNotAcceptFocus)
  view.setWindowFlags(flags)
  view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
  view.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
  view.setStyleSheet("background: transparent;")

  screens = app.screens()
  screen = screens[SCREEN_INDEX] if 0 <= SCREEN_INDEX < len(screens) else app.primaryScreen()
  view.setGeometry(screen.geometry())

  url = args.url or overlay_url(args.demo)

  def load():
    view.load(QUrl(url))

  def on_loaded(ok):
    if not ok:
      print(f"[overlay] Can't reach {BASE_URL} — start the Box-Box server (start.bat). Retrying…")
      QTimer.singleShot(3000, load)
  view.loadFinished.connect(on_loaded)
  load()
  view.show()

  # Windows: make sure clicks go to the game and the overlay never takes focus,
  # and keep it above borderless games that push themselves to the front.
  if IS_WINDOWS:
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = int(view.winId())
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x80
    get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_style = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    set_style(hwnd, GWL_EXSTYLE, get_style(hwnd, GWL_EXSTYLE)
              | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def keep_on_top():
      HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x2, 0x1, 0x10
      user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    top_timer = QTimer()
    top_timer.timeout.connect(keep_on_top)
    top_timer.start(2000)

  # Push-to-talk → page
  bridge.down.connect(lambda: page.runJavaScript("window.boxbox && window.boxbox.pttDown()"))
  bridge.up.connect(lambda: page.runJavaScript("window.boxbox && window.boxbox.pttUp()"))
  if PTT_KEY and PTT_KEY.lower() != "off":
    threading.Thread(target=run_hotkey, args=(PTT_KEY, bridge.down.emit, bridge.up.emit), daemon=True).start()
  if PTT_BUTTON:
    try:
      btn = int(PTT_BUTTON)
      threading.Thread(target=run_wheel_button, args=(btn, PTT_DEVICE, bridge.down.emit, bridge.up.emit),
                       daemon=True).start()
      print(f"[overlay] Push-to-talk button: {btn}" + (f" on '{PTT_DEVICE}'" if PTT_DEVICE else ""))
    except ValueError:
      print(f"[overlay] OVERLAY_PTT_BUTTON must be a number, got {PTT_BUTTON!r}")

  # Tray icon: the overlay itself can't be clicked
  pix = QPixmap(64, 64)
  pix.fill(QColor(0, 0, 0, 0))
  p = QPainter(pix)
  p.setRenderHint(QPainter.RenderHint.Antialiasing)
  p.setBrush(QColor("#0A101C"))
  p.setPen(Qt.PenStyle.NoPen)
  p.drawRoundedRect(2, 2, 60, 60, 12, 12)
  p.setBrush(QColor("#F2B33D"))
  for i, h in enumerate((14, 28, 40, 28, 14)):
    p.drawRect(10 + i * 9, 32 - h // 2, 6, h)
  p.end()

  tray = QSystemTrayIcon(QIcon(pix), app)
  tray.setToolTip("Box-Box radio overlay")
  menu = QMenu()
  act_test = QAction("Send a test message", menu)
  act_test.triggered.connect(lambda: page.runJavaScript("window.boxbox && window.boxbox.test()"))
  act_talk = QAction("Talk to the engineer (start/send)", menu)
  act_talk.triggered.connect(lambda: page.runJavaScript("window.boxbox && window.boxbox.pttToggle()"))
  act_hide = QAction("Hide overlay", menu)

  def toggle_visible():
    if view.isVisible():
      view.hide()
      act_hide.setText("Show overlay")
    else:
      view.show()
      act_hide.setText("Hide overlay")
  act_hide.triggered.connect(toggle_visible)
  act_reload = QAction("Reload", menu)
  act_reload.triggered.connect(load)
  act_dash = QAction("Open dashboard", menu)
  act_dash.triggered.connect(lambda: webbrowser.open(BASE_URL))
  act_quit = QAction("Quit overlay", menu)
  act_quit.triggered.connect(app.quit)
  for a in (act_test, act_talk, act_hide, act_reload, act_dash):
    menu.addAction(a)
  menu.addSeparator()
  menu.addAction(act_quit)
  tray.setContextMenu(menu)
  tray.show()
  hint = f"Hold {PTT_KEY} to talk" if PTT_KEY.lower() != "off" else "Overlay running"
  if PTT_BUTTON:
    hint += f" (or wheel button {PTT_BUTTON})"
  tray.showMessage("Box-Box radio overlay", hint, QSystemTrayIcon.MessageIcon.Information, 4000)
  print(f"[overlay] Showing {url} on screen {screens.index(screen) if screen in screens else 0}")

  sys.exit(app.exec())


if __name__ == "__main__":
  main()
