"""Мини-плеер поверх всех окон: Яндекс Музыка (и любой другой плеер Windows) + эквалайзер.
Тянуть мышкой — переместить (за край экрана не уходит), ПКМ или значок в трее — меню.
Сам появляется, когда открыта Яндекс Музыка, и прячется, когда её закрыли.

Откуда берётся трек:
  1. системные медиа-сессии Windows (приложение Яндекс Музыки, Spotify, Chrome…);
  2. расширение browser-extension/ — видит music.yandex.ru даже в фоновой вкладке
     (Яндекс Браузер не передаёт трек в Windows);
  3. без расширения — читаем плеер со страницы через UI Automation,
     но так видна только активная вкладка."""
import asyncio, ctypes, json, math, queue, re, sys, threading, time, urllib.parse, urllib.request, winreg
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QPoint, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPixmap, QRadialGradient, QAction, QGuiApplication, QIcon)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as Manager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
)
from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

HERE = Path(__file__).resolve().parent
CFG = HERE / "taskbar_player.json"
PREFER = "yandex"
BARS = 26
FALLBACK_ACCENT = QColor("#ffcc00")
EDGE = 8                    # отступ карточки от края экрана
HIDE_AFTER = 4              # через сколько секунд без музыки прятаться
AUDIO_APPS = {"browser.exe"}


def load_cfg():
    try:
        c = json.loads(CFG.read_text())
    except Exception:
        return {}
    return {"pos": c} if isinstance(c, list) else c  # старый формат — просто [x, y]


def save_cfg(**kw):
    c = load_cfg()
    c.update(kw)
    CFG.write_text(json.dumps(c))


def browser_audio_active():
    """Играет ли звук из браузера (вкладка с музыкой может быть не активной)."""
    try:
        from pycaw.pycaw import AudioUtilities
        return any(s.Process and s.Process.name().lower() in AUDIO_APPS and s.State == 1
                   for s in AudioUtilities.GetAllSessions())
    except Exception:
        return False

PREV, TOGGLE, NEXT = "prev", "toggle", "next"
SMTC_CMD = {PREV: "try_skip_previous_async", TOGGLE: "try_toggle_play_pause_async", NEXT: "try_skip_next_async"}
MEDIA_KEY = {PREV: 0xB1, TOGGLE: 0xB3, NEXT: 0xB0}
WEB_BUTTON = {PREV: ("Предыдущая песня",), TOGGLE: ("Пауза", "Воспроизведение"), NEXT: ("Следующая песня",)}


def track(title="", artist="", playing=False, cover=None, pos=None, dur=None):
    return {"title": title, "artist": artist, "playing": playing, "cover": cover,
            "pos": pos, "dur": dur, "at": time.time()}


# ═════════════════ источник 1: медиа-сессии Windows ═════════════════
class SmtcSource:
    def __init__(self):
        self.data = None            # track(...) или None, если Windows плеер не видит
        self._mgr = None
        self._key = None
        self._cover = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._run(), self.loop)

    async def _session(self):
        if self._mgr is None:
            self._mgr = await Manager.request_async()
        sessions = list(self._mgr.get_sessions())
        for s in sessions:
            if PREFER in s.source_app_user_model_id.lower():
                return s
        return self._mgr.get_current_session() or (sessions[0] if sessions else None)

    async def _run(self):
        while True:
            try:
                await self._poll()
            except Exception:
                self._mgr, self.data = None, None
            await asyncio.sleep(0.4)

    async def _poll(self):
        s = await self._session()
        if not s:
            self.data = None
            return
        p = await s.try_get_media_properties_async()
        title, artist = p.title or "", p.artist or p.album_artist or ""
        if not artist:
            for sep in (" — ", " - "):
                if sep in title:
                    artist, title = title.split(sep, 1)
                    break
        if not title:
            self.data = None
            return
        if (title, artist) != self._key:
            self._key = (title, artist)
            self._cover = await self._read_cover(p.thumbnail)
        self.data = track(title, artist, s.get_playback_info().playback_status == Status.PLAYING, self._cover)

    @staticmethod
    async def _read_cover(ref):
        if ref is None:
            return None
        try:
            stream = await ref.open_read_async()
            buf = await stream.read_async(Buffer(stream.size), stream.size, InputStreamOptions.READ_AHEAD)
            data = bytearray(buf.length)
            DataReader.from_buffer(buf).read_bytes(data)
            return bytes(data)
        except Exception:
            return None

    def command(self, cmd):
        async def go():
            s = await self._session()
            if s:
                await getattr(s, SMTC_CMD[cmd])()
        asyncio.run_coroutine_threadsafe(go(), self.loop)


# ═════════════════ Яндекс Музыка: открытый API и обложки ═════════════════
def http(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TaskbarPlayer"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def spoken_seconds(text):
    """« 1 минута, 50 секунд.» → 110"""
    units = {"час": 3600, "минут": 60, "секунд": 1}
    return sum(int(n) * units[u] for n, u in re.findall(r"(\d+)\s*(час|минут|секунд)", text or ""))


_META, _COVERS = {}, {}         # ответы API и обложки — последние 64 штуки
_CACHE_LOCK = threading.Lock()


def remember(cache, key, value, limit=64):
    with _CACHE_LOCK:
        cache[key] = value
        while len(cache) > limit:
            cache.pop(next(iter(cache)))
    return value


def yandex_meta(kind, id_):
    """Исполнители, длительность и обложка из открытого API Яндекс Музыки (авторизация не нужна)."""
    key = (kind, id_)
    if key in _META:
        return _META[key]
    meta = None
    try:
        url = f"https://api.music.yandex.net/tracks/{id_}" if kind == "track" else \
              f"https://api.music.yandex.net/albums/{id_}/with-tracks"
        res = json.loads(http(url))["result"]
        res = res[0] if isinstance(res, list) else res

        def info(t):
            return {"artists": ", ".join(a["name"] for a in t.get("artists", [])),
                    "dur": t.get("durationMs", 0) / 1000 or None}
        meta = info(res)
        if kind == "album":
            meta["dur"] = None
            meta["tracks"] = {t["title"].casefold(): info(t) for vol in res.get("volumes", []) for t in vol}
        if uri := res.get("coverUri") or (res.get("albums") or [{}])[0].get("coverUri"):
            meta["cover"] = http("https://" + uri.replace("%%", "400x400"))
    except Exception:
        pass
    return remember(_META, key, meta)


def cover_from(url):
    """Обложка по адресу со страницы (avatars.yandex.net/…) — только с серверов Яндекса."""
    if not isinstance(url, str) or not url.startswith("https://") or \
            not (urllib.parse.urlsplit(url).hostname or "").endswith(".yandex.net"):
        return None
    if url not in _COVERS:
        try:
            data = http(url)
        except Exception:
            data = None
        remember(_COVERS, url, data)
    return _COVERS.get(url)


def yandex_track(title, artists, playing, pos, dur, track_id=None, album_id=None, artwork=None):
    """track() для Яндекс Музыки: чего не хватает на странице, добираем из API."""
    cover = cover_from(artwork)
    if not (cover and artists and dur):
        meta = (yandex_meta("track", track_id) if track_id else
                yandex_meta("album", album_id) if album_id else None) or {}
        # в «Моей волне» ссылки на трек нет — ищем его по названию в треках альбома
        song = meta.get("tracks", {}).get(title.casefold(), meta)
        artists, dur = artists or song.get("artists", ""), dur or song.get("dur")
        cover = cover or meta.get("cover")
    return track(title or "—", artists, playing, cover, pos, dur)


# ═════════════════ источник 2: страница Яндекс Музыки через UI Automation ═════════════════
class YandexWebSource(threading.Thread):
    """Читает блок «Плеер» со страницы. Браузер показывает Windows только активную вкладку,
    поэтому из фоновой вкладки трек так не прочитать — для этого есть расширение."""

    def __init__(self, skip=lambda: False):
        super().__init__(daemon=True)
        self.data = None
        self.skip = skip            # True — трек уже передаёт расширение, страницу не трогаем
        self.commands = queue.Queue()
        self._player = None
        self._found_at = 0.0
        self._buttons = {}
        self.start()

    def command(self, cmd):
        self.commands.put(cmd)

    def run(self):
        import uiautomation as auto
        from uiautomation.uiautomation import _AutomationClient
        self.auto = auto
        with auto.UIAutomationInitializerInThread():
            iu = _AutomationClient.instance().IUIAutomation
            self._cond = iu.CreateAndCondition(
                iu.CreatePropertyCondition(auto.PropertyId.NameProperty, "Плеер"),
                iu.CreatePropertyCondition(auto.PropertyId.ControlTypeProperty, auto.ControlType.GroupControl))
            while True:
                try:
                    cmd = self.commands.get(timeout=0.5)
                    self._press(cmd)
                except queue.Empty:
                    pass
                except Exception:
                    pass
                try:
                    self._poll()
                except Exception:
                    self._player, self.data = None, None

    def _find_player(self):
        auto = self.auto
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName.startswith("Chrome") and "Музык" in w.Name:
                el = w.Element.FindFirst(4, self._cond)  # 4 = TreeScope_Descendants
                if el:
                    return auto.Control.CreateControlFromElement(el)
        return None

    def _poll(self):
        if self.skip():
            self._player, self.data = None, None
            return
        auto = self.auto
        # плеер на странице может смениться (Моя волна ↔ нижняя панель) — периодически ищем заново
        if self._player is None or time.time() - self._found_at > 5:
            self._player = self._find_player()
            self._found_at = time.time()
        if self._player is None:
            self.data = None
            return

        text_title = link_title = track_id = album_id = tc = None
        artists, buttons = [], {}
        for c, _ in auto.WalkControl(self._player, maxDepth=10):
            ct, name = c.ControlType, c.Name or ""
            if ct == auto.ControlType.ButtonControl:
                buttons[name] = c
            elif ct == auto.ControlType.HyperlinkControl:
                url = c.GetValuePattern().Value or ""
                if m := re.search(r"/track/(\d+)", url):
                    track_id, link_title = m.group(1), name.removeprefix("Трек ").strip()
                if m := re.search(r"/album/(\d+)", url):
                    album_id = m.group(1)
                if "/artist/" in url:
                    artists.append(name.removeprefix("Артист ").strip())
            elif ct == auto.ControlType.SliderControl and "таймкод" in name:
                tc = c.GetValuePattern().Value
            elif ct == auto.ControlType.TextControl and name and name != "Плеер" and not text_title:
                text_title = name

        if not buttons:
            self._player, self.data = None, None
            return
        self._buttons = buttons

        pos_txt, _, dur_txt = (tc or "").partition("/")
        self.data = yandex_track(link_title or text_title or "—", ", ".join(artists), "Пауза" in buttons,
                                 spoken_seconds(pos_txt) if tc else None, spoken_seconds(dur_txt) or None,
                                 track_id, album_id)

    def _press(self, cmd):
        for name in WEB_BUTTON[cmd]:
            if b := self._buttons.get(name):
                b.GetInvokePattern().Invoke()
                return


# ═════════════════ источник 3: расширение для браузера — видит и фоновые вкладки ═════════════════
BRIDGE_PORT = 47821             # тот же порт прописан в browser-extension/background.js


def _digits(v):
    return v if isinstance(v, str) and v.isdigit() else None


def _number(v):
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 else None


class BridgeSource:
    """Состояние плеера от расширения browser-extension/ по WebSocket на 127.0.0.1.
    Windows видит только активную вкладку браузера, а расширение — страницу Яндекс Музыки всегда."""

    def __init__(self):
        self.data = None
        self._tabs = {}             # (соединение, вкладка) → {"ws", "tab", "data", "seen"}
        self._active = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._serve(), self.loop)

    async def _serve(self):
        from websockets.asyncio.server import serve
        try:
            self._server = await serve(self._client, "127.0.0.1", BRIDGE_PORT)
        except OSError:
            pass                    # порт занят — скорее всего, плеер уже запущен

    async def _client(self, ws):
        # пускаем только расширения браузера — обычным сайтам здесь делать нечего
        if not ws.request.headers.get("Origin", "").startswith("chrome-extension://"):
            return
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    key = (id(ws), msg.get("tab"))
                    if msg.get("type") == "state":
                        prev = self._tabs.get(key)
                        data = await self.loop.run_in_executor(None, self._track, msg, prev and prev["data"])
                        self._tabs[key] = {"ws": ws, "tab": msg.get("tab"), "data": data, "seen": time.time()}
                    elif msg.get("type") == "gone":
                        self._tabs.pop(key, None)
                    self._pick()
                except Exception:
                    pass
        except Exception:
            pass                    # соединение оборвалось
        finally:
            for key in [k for k in self._tabs if k[0] == id(ws)]:
                del self._tabs[key]
            self._pick()

    @staticmethod
    def _track(msg, prev):
        pos, dur = _number(msg.get("pos")), _number(msg.get("dur"))
        if dur and dur > 36000:     # ползунок может считать в миллисекундах
            pos, dur = (pos or 0) / 1000, dur / 1000
        timecode = msg.get("timecode")
        if pos is None and isinstance(timecode, str):
            pos_txt, _, dur_txt = timecode.partition("/")
            pos, dur = spoken_seconds(pos_txt), dur or spoken_seconds(dur_txt) or None
        title, artist = msg.get("title"), msg.get("artist")
        t = yandex_track(title if isinstance(title, str) else "", artist if isinstance(artist, str) else "",
                         msg.get("playing") is True, pos, dur,
                         _digits(msg.get("trackId")), _digits(msg.get("albumId")), msg.get("artwork"))
        # фоновая вкладка может не обновлять время — тогда отсчитываем его сами от прошлого изменения
        if prev and prev["playing"] and t["playing"] and (prev["title"], prev["pos"]) == (t["title"], t["pos"]):
            t["at"] = prev["at"]
        return t

    def _pick(self):
        tabs = sorted(self._tabs.values(), key=lambda v: (v["data"]["playing"], v["seen"]), reverse=True)
        self._active = tabs[0] if tabs else None
        self.data = self._active["data"] if self._active else None

    def command(self, cmd):
        if active := self._active:
            msg = json.dumps({"type": "command", "tab": active["tab"], "command": cmd})
            asyncio.run_coroutine_threadsafe(active["ws"].send(msg), self.loop)


# ═════════════════ что показывать ═════════════════
class Media:
    def __init__(self):
        self.smtc, self.bridge = SmtcSource(), BridgeSource()
        self.web = YandexWebSource(skip=lambda: self.bridge.data is not None)
        self.last = track("Ничего не играет", "включи музыку")
        self._override = None       # (playing, until) — мгновенный отклик кнопки play

    def source(self):
        """Сначала то, что сейчас играет; если ничего — по надёжности источника."""
        live = [s for s in (self.smtc, self.bridge, self.web) if s.data]
        return next((s for s in live if (s.data or {}).get("playing")), live[0] if live else None)

    def current(self):
        src = self.source()
        data = src.data if src else None
        if data:
            self.last = data
        t = dict(self.last)
        if not data:
            t["playing"] = False
        if self._override and time.time() < self._override[1]:
            t["playing"] = self._override[0]
        return t

    def command(self, cmd):
        if cmd == TOGGLE:
            self._override = (not self.current()["playing"], time.time() + 1.2)
        src = self.source()
        if src:
            src.command(cmd)
        else:  # плеер не найден — жмём системные медиаклавиши
            vk = MEDIA_KEY[cmd]
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


# ═════════════════ звук: спектр того, что играет в системе ═════════════════
class Spectrum:
    def __init__(self):
        self.levels = np.zeros(BARS)
        self.last = 0.0
        self.ok = False
        try:
            import pyaudiowpatch as pa
            self._pa = pa.PyAudio()
            dev = self._pa.get_default_wasapi_loopback()
            self.rate = int(dev["defaultSampleRate"])
            self.ch = dev["maxInputChannels"]
            self.buf = np.zeros(2048, dtype=np.float32)
            self.win = np.hanning(2048).astype(np.float32)
            freqs = np.fft.rfftfreq(2048, 1 / self.rate)
            edges = np.geomspace(45, 15000, BARS + 1)
            self.bands = [np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges, edges[1:])]
            self.bands = [b if len(b) else np.array([np.searchsorted(freqs, lo)]) for b, lo in zip(self.bands, edges)]
            # колбэк, а не read(): в тишине WASAPI не шлёт данные, read() бы завис
            self._stream = self._pa.open(format=pa.paFloat32, channels=self.ch, rate=self.rate, input=True,
                                         input_device_index=dev["index"], frames_per_buffer=512,
                                         stream_callback=self._cb)
            self.ok = True
        except Exception:
            pass

    def _cb(self, data, frames, info, status):
        x = np.frombuffer(data, dtype=np.float32).reshape(-1, self.ch).mean(axis=1)
        n = len(x)
        self.buf = np.roll(self.buf, -n)
        self.buf[-n:] = x[-2048:]
        mag = np.abs(np.fft.rfft(self.buf * self.win)) / 512
        db = 20 * np.log10(np.array([mag[b].max() for b in self.bands]) + 1e-9)
        tilt = np.linspace(0, 10, BARS)  # высокие частоты тише — подтягиваем
        self.levels = np.clip((db + tilt + 62) / 52, 0, 1)
        self.last = time.time()
        return None, 0  # paContinue

    def current(self):
        if time.time() - self.last > 0.15:
            return np.zeros(BARS)
        return self.levels


# ═════════════════ автозапуск ═════════════════
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "TaskbarPlayer"


def autostart_cmd():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}" "{Path(__file__).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0] == autostart_cmd()
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ═════════════════ окно ═════════════════
def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return QColor(int(lerp(c1.red(), c2.red(), t)), int(lerp(c1.green(), c2.green(), t)),
                  int(lerp(c1.blue(), c2.blue(), t)))


def accent_from(img: QImage):
    small = img.scaled(24, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    best, score = None, -1
    for y in range(24):
        for x in range(24):
            c = QColor(small.pixel(x, y))
            h, s, v, _ = c.getHsvF()
            sc = s * 1.4 + v - abs(v - 0.8)
            if v > 0.35 and sc > score:
                best, score = c, sc
    if best is None:
        return FALLBACK_ACCENT
    h, s, v, _ = best.getHsvF()
    return QColor.fromHsvF(max(h, 0), min(1, max(s, 0.45)), max(v, 0.9))


class Player(QWidget):
    M = 18                      # поля под тень
    W, H = 400, 112             # сама карточка
    COVER = 84

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.resize(self.W + 2 * self.M, self.H + 2 * self.M)

        self.media, self.spec = Media(), Spectrum()
        self.t = self.media.current()
        self.key = None
        self.cover = None
        self.accent = QColor(FALLBACK_ACCENT)
        self.target_accent = QColor(FALLBACK_ACCENT)
        self.bars = np.zeros(BARS)
        self.peaks = np.zeros(BARS)
        self.idle_t = 0.0
        self.scroll = 0.0
        self.progress = 0.0
        self.hover = None
        self.press = {}
        self.drag = None
        self.play_anim = 0.0

        self.f_title = QFont("Segoe UI Variable Display", 11, QFont.DemiBold)
        self.f_artist = QFont("Segoe UI Variable Text", 9)

        x0, y0 = self.M + self.COVER + 30, self.M
        cy = y0 + 70
        bx = self.M + self.W - 120
        self.buttons = {
            PREV: (QPointF(bx + 14, cy), 16),
            TOGGLE: (QPointF(bx + 56, cy), 22),
            NEXT: (QPointF(bx + 98, cy), 16),
        }
        self.text_x = x0
        self.text_w = self.M + self.W - 22 - x0
        self.viz = QRectF(x0, y0 + 56, bx - x0 - 14, 30)

        self.autohide = load_cfg().get("autohide", True)
        self.shown = False
        self.last_active = 0.0
        self.peek_until = 0.0
        self.slide = QPropertyAnimation(self, b"pos", self, duration=420, easingCurve=QEasingCurve.OutBack)
        self.slide.finished.connect(lambda: save_cfg(pos=[self.x(), self.y()]))
        self.fade = QPropertyAnimation(self, b"windowOpacity", self, duration=260)
        self.fade.finished.connect(lambda: None if self.shown else self.hide())
        self.tray = self._make_tray()

        self._restore_pos()
        QTimer(self, interval=16, timeout=self.tick).start()
        QTimer(self, interval=1000, timeout=self.force_top).start()
        QTimer(self, interval=700, timeout=self.update_visibility).start()
        self.update_visibility()

    # ---------- позиция ----------
    def _restore_pos(self):
        if pos := load_cfg().get("pos"):
            self.move(self._clamp(QPoint(*pos)))
        else:
            g = QApplication.primaryScreen().availableGeometry()
            self.move(self._clamp(QPoint(g.left(), g.bottom() - self.height() + self.M - 10)))

    def _clamp(self, pt):
        """Ближайшая позиция окна, при которой карточка целиком на экране."""
        center = pt + QPoint(self.width() // 2, self.height() // 2)
        scr = QGuiApplication.screenAt(center) or min(
            QGuiApplication.screens(), key=lambda s: (s.geometry().center() - center).manhattanLength())
        g = scr.geometry()
        x = min(max(pt.x(), g.left() + EDGE - self.M), g.left() + g.width() - EDGE - self.M - self.W)
        y = min(max(pt.y(), g.top() + EDGE - self.M), g.top() + g.height() - EDGE - self.M - self.H)
        return QPoint(x, y)

    def _rubber(self, want):
        """За краем экрана окно тянется с сопротивлением, как резинка (не дальше ~50 px)."""
        c = self._clamp(want)

        def damp(over):
            return int(math.copysign(50 * (1 - math.exp(-abs(over) / 90)), over))
        return QPoint(c.x() + damp(want.x() - c.x()), c.y() + damp(want.y() - c.y()))

    def force_top(self):
        # панель задач и разворачиваемые окна иногда перекрывают — возвращаемся наверх
        if self.shown:
            ctypes.windll.user32.SetWindowPos(int(self.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    # ---------- появление / скрытие ----------
    def update_visibility(self):
        now = time.time()
        if self.media.source() or browser_audio_active():
            self.last_active = now
        want = not self.autohide or now < self.peek_until or now - self.last_active < HIDE_AFTER
        if want and not self.shown:
            self._set_shown(True)
        elif not want and self.shown and self.drag is None and not self.underMouse():
            self._set_shown(False)

    def _set_shown(self, on):
        self.shown = on
        self.fade.stop()
        if on:
            self.setWindowOpacity(0.0)
            self.show()
            self.force_top()
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(1.0 if on else 0.0)
        self.fade.start()

    def set_autohide(self, on):
        self.autohide = on
        save_cfg(autohide=on)
        self.update_visibility()

    def peek(self):
        """Показать на пару секунд (клик по значку в трее)."""
        self.peek_until = time.time() + 6
        self.update_visibility()

    # ---------- меню и трей ----------
    def build_menu(self, menu=None):
        menu = menu or QMenu(self)
        menu.clear()
        menu.setStyleSheet("""
            QMenu { background:#1c1c22; color:#eee; border:1px solid #333; border-radius:10px; padding:6px; }
            QMenu::item { padding:7px 22px 7px 14px; border-radius:6px; }
            QMenu::item:selected { background:#2e2e38; }
            QMenu::indicator { width:14px; height:14px; margin-left:6px; }""")
        auto = QAction("Запускать вместе с Windows", menu, checkable=True, checked=autostart_enabled())
        auto.toggled.connect(set_autostart)
        hide = QAction("Прятать, когда Яндекс Музыка закрыта", menu, checkable=True, checked=self.autohide)
        hide.toggled.connect(self.set_autohide)
        menu.addAction(auto)
        menu.addAction(hide)
        menu.addSeparator()
        menu.addAction("Закрыть плеер", QApplication.quit)
        return menu

    def _make_tray(self):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(FALLBACK_ACCENT)
        p.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)
        p.setPen(QColor(20, 20, 24))
        p.setFont(QFont("Segoe UI Symbol", 30, QFont.Bold))
        p.drawText(QRectF(0, 0, 64, 64), Qt.AlignCenter, "♫")
        p.end()
        tray = QSystemTrayIcon(QIcon(pm), self)
        tray.setToolTip("Taskbar Player")
        menu = QMenu()
        menu.aboutToShow.connect(lambda: self.build_menu(menu))
        self.build_menu(menu)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda r: self.peek() if r == QSystemTrayIcon.Trigger else None)
        tray.show()
        return tray

    # ---------- анимация ----------
    def tick(self):
        if not self.isVisible():
            return
        t = self.t = self.media.current()
        key = (t["title"], t["artist"], t["cover"] is not None)
        if key != self.key:
            if key[:2] != (self.key or ("", ""))[:2]:
                self.scroll = -60.0
            self.key = key
            self.cover = None
            if t["cover"]:
                img = QImage.fromData(t["cover"])
                if not img.isNull():
                    self.cover = QPixmap.fromImage(img)
                    self.target_accent = accent_from(img)
            if self.cover is None:
                self.target_accent = QColor(FALLBACK_ACCENT)
        self.accent = mix(self.accent, self.target_accent, 0.06)

        # прогресс: позиция со страницы + сколько прошло с последнего опроса
        if t["pos"] is not None and t["dur"]:
            pos = t["pos"] + (time.time() - t["at"] if t["playing"] else 0)
            target = min(1.0, pos / t["dur"])
            jump = self.progress is None or abs(target - self.progress) > 0.05
            self.progress = target if jump else lerp(self.progress, target, 0.3)
        else:
            self.progress = None

        target = self.spec.current() if self.spec.ok else np.zeros(BARS)
        if t["playing"] and not target.any() and not self.spec.ok:
            # нет захвата звука — мягкая «живая» анимация
            self.idle_t += 0.016
            i = np.arange(BARS)
            target = 0.35 + 0.25 * np.sin(self.idle_t * 3 + i * 0.5) * np.sin(self.idle_t * 1.7 + i * 0.23)
        up = target > self.bars
        self.bars = np.where(up, lerp(self.bars, target, 0.55), lerp(self.bars, target, 0.12))
        self.peaks = np.maximum(self.peaks - 0.012, self.bars)
        self.play_anim = lerp(self.play_anim, 1.0 if t["playing"] else 0.0, 0.2)
        self.scroll += 0.5
        self.update()

    # ---------- рисование ----------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        card = QRectF(self.M, self.M, self.W, self.H)
        R = 24

        # тень
        for i in range(self.M, 0, -2):
            a = int(38 * (1 - i / self.M) ** 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(card.adjusted(-i, -i + 4, i, i + 4), R + i, R + i)

        # фон: тёмное стекло с подсветкой цветом обложки
        path = QPainterPath()
        path.addRoundedRect(card, R, R)
        p.setClipPath(path)
        p.fillPath(path, QColor(16, 16, 20, 242))
        glow = QRadialGradient(card.left() + 60, card.top() + 20, 260)
        a = QColor(self.accent)
        a.setAlpha(80)
        glow.setColorAt(0, a)
        a.setAlpha(0)
        glow.setColorAt(1, a)
        p.fillRect(card, glow)

        # полоска прогресса по нижнему краю
        if self.progress is not None:
            bar = QRectF(card.left(), card.bottom() - 3, card.width(), 3)
            p.fillRect(bar, QColor(255, 255, 255, 22))
            p.fillRect(QRectF(bar.left(), bar.top(), bar.width() * self.progress, bar.height()), self.accent)
        p.setClipping(False)

        p.setPen(QColor(255, 255, 255, 26))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), R, R)

        self._paint_cover(p, QRectF(self.M + 14, self.M + 14, self.COVER, self.COVER))
        self._paint_text(p)
        self._paint_viz(p)
        self._paint_buttons(p)

    def _paint_cover(self, p, r):
        path = QPainterPath()
        path.addRoundedRect(r, 16, 16)
        p.save()
        p.setClipPath(path)
        if self.cover:
            pm = self.cover.scaled(r.size().toSize() * 2, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            p.drawPixmap(r, pm, QRectF(0, 0, pm.width(), pm.height()))
        else:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, self.accent.darker(150))
            g.setColorAt(1, QColor(40, 30, 60))
            p.fillPath(path, g)
            p.setPen(QColor(255, 255, 255, 200))
            p.setFont(QFont("Segoe UI Symbol", 26))
            p.drawText(r, Qt.AlignCenter, "♫")
        if self.play_anim < 0.99:  # на паузе обложка чуть гаснет
            p.fillPath(path, QColor(0, 0, 0, int(110 * (1 - self.play_anim))))
        p.restore()
        p.setPen(QColor(255, 255, 255, 30))
        p.drawRoundedRect(r, 16, 16)

    def _marquee(self, p, text, font, y, color):
        fm = QFontMetricsF(font)
        p.setFont(font)
        p.setPen(color)
        w = fm.horizontalAdvance(text)
        box = QRectF(self.text_x, y, self.text_w, fm.height() + 2)
        p.save()
        p.setClipRect(box)
        if w <= self.text_w:
            p.drawText(box, Qt.AlignLeft | Qt.AlignVCenter, text)
        else:
            gap = 48
            off = max(0.0, self.scroll) % (w + gap)
            for dx in (-off, w + gap - off):
                p.drawText(QRectF(self.text_x + dx, y, w + 4, box.height()), Qt.AlignLeft | Qt.AlignVCenter, text)
        p.restore()

    def _paint_text(self, p):
        self._marquee(p, self.t["title"], self.f_title, self.M + 14, QColor(255, 255, 255, 245))
        self._marquee(p, self.t["artist"], self.f_artist, self.M + 36, QColor(255, 255, 255, 150))

    def _paint_viz(self, p):
        r = self.viz
        gap = 2.5
        bw = (r.width() - gap * (BARS - 1)) / BARS
        top, low = self.accent.lighter(130), self.accent.darker(140)
        for i, v in enumerate(self.bars):
            h = max(3.0, v * r.height())
            x = r.left() + i * (bw + gap)
            g = QLinearGradient(0, r.bottom() - h, 0, r.bottom())
            g.setColorAt(0, top)
            g.setColorAt(1, low)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(x, r.bottom() - h, bw, h), bw / 2, bw / 2)
            pk = r.bottom() - max(3.0, self.peaks[i] * r.height()) - 3
            p.setBrush(QColor(255, 255, 255, int(170 * min(1, self.peaks[i] * 3))))
            p.drawRoundedRect(QRectF(x, pk, bw, 2), 1, 1)

    def _paint_buttons(self, p):
        for name, (c, rad) in self.buttons.items():
            hov, pressed = self.hover == name, self.press.get(name, 0) > time.time()
            scale = 0.9 if pressed else (1.08 if hov else 1.0)
            p.save()
            p.translate(c)
            p.scale(scale, scale)
            if name == TOGGLE:
                p.setPen(Qt.NoPen)
                p.setBrush(self.accent if hov else QColor(255, 255, 255))
                p.drawEllipse(QPointF(0, 0), rad, rad)
                p.setBrush(QColor(18, 18, 22))
                if self.t["playing"]:
                    p.drawRoundedRect(QRectF(-6.5, -7.5, 4.6, 15), 1.5, 1.5)
                    p.drawRoundedRect(QRectF(1.9, -7.5, 4.6, 15), 1.5, 1.5)
                else:
                    tri = QPainterPath()
                    tri.moveTo(-4.5, -8)
                    tri.lineTo(8.5, 0)
                    tri.lineTo(-4.5, 8)
                    tri.closeSubpath()
                    p.drawPath(tri)
            else:
                if hov:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 255, 255, 28))
                    p.drawEllipse(QPointF(0, 0), rad, rad)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 235))
                d = -1 if name == PREV else 1
                for off in (0, 7):
                    tri = QPainterPath()
                    tx = d * (off - 3.5)
                    tri.moveTo(tx - d * 6, -6.5)
                    tri.lineTo(tx + d * 6, 0)
                    tri.lineTo(tx - d * 6, 6.5)
                    tri.closeSubpath()
                    p.drawPath(tri)
            p.restore()

    # ---------- мышь ----------
    def _hit(self, pos):
        for name, (c, rad) in self.buttons.items():
            if math.hypot(pos.x() - c.x(), pos.y() - c.y()) <= rad + 4:
                return name
        return None

    def mouseMoveEvent(self, e):
        if self.drag is not None:
            self.move(self._rubber((e.globalPosition() - self.drag).toPoint()))
            return
        self.hover = self._hit(e.position())
        self.setCursor(Qt.PointingHandCursor if self.hover else Qt.ArrowCursor)

    def leaveEvent(self, _):
        self.hover = None

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        name = self._hit(e.position())
        if name:
            self.press[name] = time.time() + 0.12
            self.media.command(name)
        else:
            self.slide.stop()
            self.drag = e.globalPosition() - QPointF(self.pos())
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, e):
        if self.drag is not None:
            self.drag = None
            self.setCursor(Qt.ArrowCursor)
            target = self._clamp(self.pos())
            if target != self.pos():  # вытянули за край — отпружиниваем обратно
                self.slide.setStartValue(self.pos())
                self.slide.setEndValue(target)
                self.slide.start()
            else:
                save_cfg(pos=[self.x(), self.y()])

    def contextMenuEvent(self, e):
        self.build_menu().exec(e.globalPos())


if __name__ == "__main__":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TaskbarPlayer")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # окно прячется, а плеер продолжает ждать музыку
    w = Player()
    sys.exit(app.exec())
