"""Картинки для README: docs/hero.png и docs/demo.webp.

Рисует настоящий виджет из taskbar_player.pyw, только вместо Яндекс Музыки и звука
подставляет выдуманные треки, нарисованные обложки и синтетический спектр —
на картинках нет чужих обложек и ничего с экрана.

    pip install pillow
    python tools/make_screenshots.py
"""
import importlib.util, math, random, sys
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QRadialGradient, QRegion)
from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"

_spec = importlib.util.spec_from_file_location("taskbar_player", ROOT / "taskbar_player.pyw")
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)

SCALE = 2          # всё в 2x — чётко на ретине
FPS = 30
CYCLE = 8.0        # длина зацикленной анимации, с


# ═════════════════ обложки ═════════════════
def canvas(size=400):
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.black)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 400, size / 400)
    return img, p


def cover_sunset():
    """Синтвейв: солнце в полоску над неоновой сеткой."""
    img, p = canvas()
    sky = QLinearGradient(0, 0, 0, 290)
    sky.setColorAt(0, QColor("#0f0726"))
    sky.setColorAt(0.6, QColor("#3a0d5e"))
    sky.setColorAt(1, QColor("#8a1d6e"))
    p.fillRect(QRectF(0, 0, 400, 290), sky)
    rnd = random.Random(7)
    for _ in range(70):
        p.setPen(QColor(255, 255, 255, rnd.randint(60, 200)))
        p.drawPoint(QPointF(rnd.uniform(0, 400), rnd.uniform(0, 190)))
    glow = QRadialGradient(200, 220, 200)
    glow.setColorAt(0, QColor(255, 110, 150, 150))
    glow.setColorAt(1, QColor(255, 110, 150, 0))
    p.fillRect(QRectF(0, 0, 400, 290), glow)

    sun, cuts, sky_part = QPainterPath(), QPainterPath(), QPainterPath()
    sun.addEllipse(QPointF(200, 205), 112, 112)
    for y, h in [(200, 4), (218, 6), (236, 8), (255, 10), (275, 12)]:
        cuts.addRect(QRectF(0, y, 400, h))
    sky_part.addRect(QRectF(0, 0, 400, 290))
    g = QLinearGradient(0, 93, 0, 290)
    g.setColorAt(0, QColor("#ffe27a"))
    g.setColorAt(0.5, QColor("#ff8a5c"))
    g.setColorAt(1, QColor("#ff2e88"))
    p.fillPath(sun.subtracted(cuts).intersected(sky_part), g)

    ground = QLinearGradient(0, 290, 0, 400)
    ground.setColorAt(0, QColor("#1a0630"))
    ground.setColorAt(1, QColor("#07020f"))
    p.fillRect(QRectF(0, 290, 400, 110), ground)
    p.setPen(QPen(QColor(255, 60, 200, 170), 1.6))
    for k in range(1, 12):
        y = 290 + k ** 1.8 * 1.6
        p.drawLine(QPointF(0, y), QPointF(400, y))
    for k in range(-10, 11):
        p.drawLine(QPointF(200 + k * 6, 290), QPointF(200 + k * 60, 400))
    p.setPen(QPen(QColor(255, 120, 230), 2.2))
    p.drawLine(QPointF(0, 290), QPointF(400, 290))
    p.end()
    return img


def cover_ocean():
    """Луна и слои волн."""
    img, p = canvas()
    bg = QLinearGradient(0, 0, 0, 400)
    bg.setColorAt(0, QColor("#020b14"))
    bg.setColorAt(0.55, QColor("#06283d"))
    bg.setColorAt(1, QColor("#0a4560"))
    p.fillRect(QRectF(0, 0, 400, 400), bg)
    glow = QRadialGradient(285, 105, 150)
    glow.setColorAt(0, QColor(120, 230, 255, 120))
    glow.setColorAt(1, QColor(120, 230, 255, 0))
    p.fillRect(QRectF(0, 0, 400, 400), glow)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e8fbff"))
    p.drawEllipse(QPointF(285, 105), 44, 44)
    for j in range(6):
        base, amp = 185 + j * 38, 16 - j
        crest = QPainterPath()
        for x in range(0, 404, 4):
            y = base + amp * math.sin(x / (38 + j * 6) + j * 1.7)
            crest.lineTo(x, y) if x else crest.moveTo(x, y)
        body = QPainterPath(crest)
        body.lineTo(400, 400)
        body.lineTo(0, 400)
        body.closeSubpath()
        c = tp.mix(QColor("#38e1f5"), QColor("#062a40"), j / 5)
        g = QLinearGradient(0, base - amp, 0, base + 120)
        g.setColorAt(0, c)
        g.setColorAt(1, c.darker(170))
        p.fillPath(body, g)
        p.strokePath(crest, QPen(QColor(220, 250, 255, 70), 1.2))
    p.end()
    return img


def cover_garden():
    """Боке из светящихся кругов."""
    img, p = canvas()
    bg = QRadialGradient(200, 230, 300)
    bg.setColorAt(0, QColor("#1f3d12"))
    bg.setColorAt(1, QColor("#050d06"))
    p.fillRect(QRectF(0, 0, 400, 400), bg)
    p.setCompositionMode(QPainter.CompositionMode_Screen)
    p.setPen(Qt.NoPen)
    rnd = random.Random(11)
    colors = ["#a3e635", "#facc15", "#4ade80", "#d9f99d", "#fde047", "#86efac"]
    for _ in range(28):
        x, y, r = rnd.uniform(0, 400), rnd.uniform(0, 400), rnd.uniform(16, 88)
        c = QColor(rnd.choice(colors))
        g = QRadialGradient(x, y, r)
        for stop, a in [(0, 150), (0.7, 90), (0.95, 140), (1, 0)]:
            c.setAlpha(a)
            g.setColorAt(stop, c)
        p.setBrush(g)
        p.drawEllipse(QPointF(x, y), r, r)
    p.end()
    return img


#          название            исполнитель               обложка       длит.  старт
TRACKS = [("Неоновый закат",   "Midnight Arcade",        cover_sunset, 214,   61),
          ("Ocean Drift",      "Lumen Coast feat. Mira", cover_ocean,  187,   95),
          ("Сад после дождя",  "Velvet Garden",          cover_garden, 241,  120)]


def png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


# ═════════════════ «музыка» для эквалайзера ═════════════════
def spectrum(t, seed):
    """Бочка, малый и хэты на 120 BPM; все периоды делят CYCLE — анимация зацикливается без шва."""
    i = np.linspace(0, 1, tp.BARS)

    def hump(center, width):
        return np.exp(-((i - center) / width) ** 2)

    kick = math.exp(-(t % 0.5) / 0.5 * 7)
    snare = math.exp(-((t + 0.5) % 1.0) * 9)
    hat = math.exp(-(t % 0.25) / 0.25 * 10)
    noise = np.random.default_rng(round(t * 60) % round(CYCLE * 60) * 7 + seed).random(tp.BARS)
    level = (0.28 + 0.30 * hump(0.08, 0.12) + 0.18 * hump(0.45, 0.15) + 0.12 * hump(0.78, 0.10)
             + 0.12 * np.sin(np.pi * t + i * 9 + seed)
             + 0.35 * kick * np.exp(-i * 6)
             + 0.25 * snare * hump(0.5, 0.2)
             + 0.18 * hat * i ** 2
             + 0.22 * (noise - 0.5))
    return np.clip(level, 0, 1)


# ═════════════════ плеер без настоящих источников ═════════════════
class FakeMedia:
    def __init__(self):
        self.state = tp.track()

    def current(self):
        return dict(self.state)

    def source(self):
        return None

    def command(self, cmd):
        pass


class FakeSpectrum:
    ok = True

    def __init__(self):
        self.target = np.zeros(tp.BARS)

    def current(self):
        return self.target


def make_player():
    tp.Media, tp.Spectrum = FakeMedia, FakeSpectrum
    tp.Player._make_tray = lambda self: None
    tp.Player.update_visibility = lambda self: None
    w = tp.Player()
    for timer in w.findChildren(QTimer):
        timer.stop()          # кадры двигаем сами, по 60 «тиков» в секунду
    w.setAttribute(Qt.WA_DontShowOnScreen)
    w.show()
    return w


def render(w):
    img = QImage(w.size() * SCALE, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(SCALE)
    img.fill(Qt.transparent)
    w.render(img, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    img.setDevicePixelRatio(1)
    return img


def to_pil(img):
    img = img.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()),
                            "raw", "RGBA", img.bytesPerLine(), 1).copy()


def simulate(w, covers, seconds, timeline, t0=0.0, record=False):
    """Крутит настоящий tick() плеера; record — собирать кадры с частотой FPS."""
    frames = []
    for k in range(round(seconds * 60)):
        t = t0 + k / 60
        idx, playing, pos = timeline(t)
        title, artist, _, dur, _ = TRACKS[idx]
        w.media.state = tp.track(title, artist, playing, covers[idx], pos, dur)
        w.spec.target = spectrum(t, idx) if playing else np.zeros(tp.BARS)
        w.tick()
        if record and k % (60 // FPS) == 0:
            frames.append(to_pil(render(w)))
    return frames


# ═════════════════ сценарий анимации ═════════════════
SEGMENTS = [(0, 0.0, 2.6), (1, 2.6, 5.4), (2, 5.4, CYCLE)]   # трек, начало, конец
PAUSE = (3.6, 4.6)                                           # пауза во втором треке


def demo_timeline(t):
    tc = t % CYCLE
    for idx, a, b in SEGMENTS:
        if a <= tc < b:
            played, playing = tc - a, True
            if idx == 1 and tc >= PAUSE[0]:
                playing = tc >= PAUSE[1]
                played -= min(tc, PAUSE[1]) - PAUSE[0]
            return idx, playing, TRACKS[idx][4] + played
    return 0, True, TRACKS[0][4]


def make_demo(w, covers):
    simulate(w, covers, CYCLE, demo_timeline)                       # разгон: цвет и столбики «устаканиваются»
    return simulate(w, covers, CYCLE, demo_timeline, t0=CYCLE, record=True)


def make_hero(w, covers, cover_imgs):
    shots = []
    for idx in range(len(TRACKS)):
        simulate(w, covers, 2.5, lambda t, i=idx: (i, True, TRACKS[i][4] + t), t0=1.3 * idx)
        shots.append(render(w))

    W, H = 1760, 990
    out = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    frame = QPainterPath()
    frame.addRoundedRect(QRectF(0, 0, W, H), 40, 40)
    p.setClipPath(frame)
    p.fillRect(out.rect(), QColor("#09090e"))

    positions = [QPoint(150, 80), QPoint(444, 347), QPoint(738, 614)]
    for shot, pos, cover in zip(shots, positions, cover_imgs):
        center = QPointF(pos.x() + shot.width() / 2, pos.y() + shot.height() / 2)
        g = QRadialGradient(center, 620)
        c = tp.accent_from(cover)
        c.setAlpha(70)
        g.setColorAt(0, c)
        c.setAlpha(0)
        g.setColorAt(1, c)
        p.fillRect(out.rect(), g)
    vignette = QRadialGradient(W / 2, H / 2, W * 0.65)
    vignette.setColorAt(0.5, QColor(0, 0, 0, 0))
    vignette.setColorAt(1, QColor(0, 0, 0, 150))
    p.fillRect(out.rect(), vignette)
    for shot, pos in zip(shots, positions):
        p.drawImage(pos, shot)
    p.end()
    return out


def main():
    app = QApplication(sys.argv)
    OUT.mkdir(exist_ok=True)
    cover_imgs = [make() for _, _, make, _, _ in TRACKS]
    covers = [png_bytes(img) for img in cover_imgs]
    w = make_player()

    frames = make_demo(w, covers)
    frames[0].save(OUT / "demo.webp", save_all=True, append_images=frames[1:],
                   duration=round(1000 / FPS), loop=0, quality=82, method=4)
    to_pil(make_hero(w, covers, cover_imgs)).save(OUT / "hero.png", optimize=True)

    for name in ("hero.png", "demo.webp"):
        print(f"{name}: {(OUT / name).stat().st_size / 1024:.0f} KB")
    print(f"demo frames: {len(frames)}, {frames[0].size}")


if __name__ == "__main__":
    main()
