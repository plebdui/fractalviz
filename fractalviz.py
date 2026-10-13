#!/usr/bin/env python3
"""fractalviz - a terminal audio visualizer that endlessly zooms into a fractal.

The zoom runs at a steady pace. Bass hits pull the pace down, highs push it up,
and it always eases back to steady when the music calms down.

Keys (while running):
  1-9 / Tab / arrows  switch fractal   p     next color palette
  + / -       base zoom speed          [ ]   audio sensitivity
  d           detail (1x/2x/3x)        r     restart the zoom
  h           pin the info bar (it hides itself after 5s without a keypress)
  q / Esc     quit
"""

import argparse
import colorsys
import math
import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty

import numpy as np

# ---------------------------------------------------------------- audio ----

RATE = 44100
FFT_SIZE = 2048


class Audio:
    """Reads what your speakers are playing (or the mic) through parec in a thread."""

    def __init__(self, use_mic=False):
        self.buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self.lock = threading.Lock()
        self.ok = False
        self.proc = None
        device = "@DEFAULT_SOURCE@" if use_mic else "@DEFAULT_MONITOR@"
        try:
            self.proc = subprocess.Popen(
                ["parec", f"--device={device}", "--format=float32le",
                 f"--rate={RATE}", "--channels=1", "--latency-msec=20"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.ok = True
            threading.Thread(target=self._reader, daemon=True).start()
        except FileNotFoundError:
            pass

    def _reader(self):
        chunk = 1024 * 4  # bytes -> 1024 float samples
        while True:
            data = self.proc.stdout.read(chunk)
            if not data:
                self.ok = False
                return
            samples = np.frombuffer(data, dtype=np.float32)
            with self.lock:
                self.buf = np.roll(self.buf, -len(samples))
                self.buf[-len(samples):] = samples

    def bands(self):
        """Return (bass, mid, treble) energy for the latest audio."""
        with self.lock:
            x = self.buf.copy()
        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        freqs = np.fft.rfftfreq(len(x), 1 / RATE)

        def band(lo, hi):
            m = (freqs >= lo) & (freqs < hi)
            return float(np.sqrt(np.mean(spec[m] ** 2)))

        return band(20, 250), band(250, 4000), band(4000, 16000)

    def close(self):
        if self.proc:
            self.proc.terminate()


class Pace:
    """Turns raw band energy into a zoom-speed multiplier around 1.0 (steady).

    Each band is compared to its own slow running average, so only *changes*
    matter: a bass hit louder than the recent bass slows things down, a burst of
    highs speeds them up, and constant sound settles back to steady pace.
    """

    def __init__(self):
        self.avg = np.array([1e-3, 1e-3, 1e-3])
        self.fast = np.zeros(3)
        self.mult = 1.0
        self.sensitivity = 1.0
        self.bass = self.treble = 0.0  # for the meters
        self.tint = 0.5  # 0 = all bass (purple) ... 1 = all treble (orange)

    def update(self, bands, dt):
        b = np.array(bands)
        self.fast += (b - self.fast) * min(1.0, dt * 12)       # quick follower
        self.avg += (self.fast - self.avg) * min(1.0, dt * 0.4)  # ~2.5s memory
        loud = self.avg.sum() > 0.02  # ignore near-silence
        dev = np.clip((self.fast - self.avg) / (self.avg + 1e-4), -1, 2.5) if loud else np.zeros(3)
        self.bass, self.treble = float(dev[0]), float(dev[2])
        tint = np.clip(0.5 + 0.35 * (dev[2] - dev[0]), 0, 1)
        self.tint += (tint - self.tint) * min(1.0, dt * 4)

        s = self.sensitivity
        target = 1.0 + 2.4 * s * max(dev[2], 0) - 1.3 * s * max(dev[0], 0)
        target = float(np.clip(target, 0.04, 7.0))
        # react fast, return to steady a bit slower so changes feel smooth
        rate = 12 if abs(target - 1) > abs(self.mult - 1) else 2.5
        self.mult += (target - self.mult) * min(1.0, dt * rate)
        return self.mult, float(dev[1])


# ------------------------------------------------------------- fractals ----

def _escape(z, c, step, maxit, power=2.0, skip=None):
    """Generic escape-time loop. Returns a smooth iteration count per pixel
    (maxit for pixels that never escape). Pixels in `skip` are left at maxit."""
    out = np.full(z.shape, float(maxit))
    idx = np.arange(z.size)
    z = z.ravel().copy()
    c = np.broadcast_to(c, out.shape).ravel().copy()
    if skip is not None:
        keep = ~skip.ravel()
        z, c, idx = z[keep], c[keep], idx[keep]
    flat = out.ravel()
    lp = math.log(power)
    for i in range(maxit):
        z = step(z, c)
        mag = z.real * z.real + z.imag * z.imag
        esc = mag > 256.0
        if esc.any():
            m = np.sqrt(mag[esc])
            flat[idx[esc]] = i + 1 - np.log(np.log(m)) / lp
            keep = ~esc
            z, c, idx = z[keep], c[keep], idx[keep]
            if idx.size == 0:
                break
    return out


def mandelbrot(px, maxit, _t):
    # the big heart shape and the circle left of it are known to be inside the set,
    # so skip them instead of grinding through every iteration (big speedup)
    x, y = px.real, px.imag
    q = (x - 0.25) ** 2 + y * y
    known = (q * (q + x - 0.25) <= 0.25 * y * y) | ((x + 1) ** 2 + y * y <= 0.0625)
    return _escape(np.zeros_like(px), px, lambda z, c: z * z + c, maxit, skip=known)


def julia(px, maxit, t):
    # c drifts very slowly around a lovely spot so the shape breathes
    c = complex(-0.7269 + 0.012 * math.cos(t * 0.05), 0.1889 + 0.012 * math.sin(t * 0.05))
    return _escape(px, np.full(px.shape, c), lambda z, c: z * z + c, maxit)


def burning_ship(px, maxit, _t):
    def step(z, c):
        z = np.abs(z.real) + 1j * np.abs(z.imag)
        return z * z + c
    return _escape(np.zeros_like(px), np.conj(px), step, maxit)  # flip so the ship sits upright


def tricorn(px, maxit, _t):
    return _escape(np.zeros_like(px), px, lambda z, c: np.conj(z) ** 2 + c, maxit)


def multibrot(px, maxit, _t):
    return _escape(np.zeros_like(px), px, lambda z, c: z * z * z + c, maxit, power=3.0)


ROOTS = np.exp(2j * np.pi * np.arange(3) / 3)


def newton(px, maxit, _t):
    """Newton's method on z^3 - 1. Color = which root a point falls into,
    shaded by how long it took to get there."""
    out = np.full(px.shape, float(maxit))
    flat = out.ravel()
    z = px.ravel().copy()
    z[z == 0] = 1e-12
    idx = np.arange(z.size)
    for i in range(min(maxit, 150)):
        z = z - (z ** 3 - 1) / (3 * z * z + 1e-300)
        d = np.abs(z[:, None] - ROOTS)
        near = d.min(1)
        hit = near < 1e-4
        if hit.any():
            root = d[hit].argmin(1)
            # fractional part from how close it landed = smooth shading
            frac = np.log(near[hit] + 1e-30) / np.log(1e-4)
            flat[idx[hit]] = ((i + 2 - frac) * 2.5 + root * maxit / 3.0) % maxit
            keep = ~hit
            z, idx = z[keep], idx[keep]
            if idx.size == 0:
                break
    return out


def phoenix(px, maxit, _t):
    """Like Julia, but each step also remembers the step before it:
    z = z² + 0.5667 - 0.5·(previous z). Drawn on its side so it stands up."""
    out = np.full(px.shape, float(maxit))
    flat = out.ravel()
    z = (px.imag - 1j * px.real).ravel().copy()
    prev = np.zeros_like(z)
    idx = np.arange(z.size)
    for i in range(maxit):
        z, prev = z * z + 0.5667 - 0.5 * prev, z
        mag = z.real * z.real + z.imag * z.imag
        esc = mag > 256.0
        if esc.any():
            flat[idx[esc]] = i + 1 - np.log(np.log(np.sqrt(mag[esc]))) / math.log(2)
            keep = ~esc
            z, prev, idx = z[keep], prev[keep], idx[keep]
            if idx.size == 0:
                break
    return out


# ---- Apollonian gasket: circles packed inside circles, forever.
# Start with a big circle holding three equal circles that all touch. Every gap
# between touching circles gets filled with the biggest circle that fits, and so on.
# Trick for drawing it per pixel: there are 4 "mirror" circles, each running through
# the three touching points of one gap. Reflecting a point in the mirror (circle
# inversion) maps that gap's circles onto bigger ones. Keep reflecting until the point
# lands in one of the 4 starting circles. Points in tiny deep circles need lots of
# reflections, so they glow brightest, right where the packing gets endless.
_R = 2 * math.sqrt(3) - 3                                      # inner circle size
_C = [(1 - _R) * np.exp(1j * (math.pi / 2 + 2 * math.pi * k / 3)) for k in range(3)]


def _circle_through(a, b, c):
    d = 2 * (a.real * (b.imag - c.imag) + b.real * (c.imag - a.imag) + c.real * (a.imag - b.imag))
    ux = (abs(a) ** 2 * (b.imag - c.imag) + abs(b) ** 2 * (c.imag - a.imag) + abs(c) ** 2 * (a.imag - b.imag)) / d
    uy = (abs(a) ** 2 * (c.real - b.real) + abs(b) ** 2 * (a.real - c.real) + abs(c) ** 2 * (b.real - a.real)) / d
    o = complex(ux, uy)
    return o, abs(a - o)


_MIRRORS = [(0j, (1 - _R) / 2)] + [
    _circle_through(_C[i] / abs(_C[i]), _C[j] / abs(_C[j]), (_C[i] + _C[j]) / 2)
    for i, j in ((0, 1), (1, 2), (2, 0))]


def apollonian(px, maxit, _t):
    out = np.full(px.shape, float(maxit))
    flat = out.ravel()
    z = px.ravel().copy()
    idx = np.arange(z.size)
    for i in range(maxit):
        # landed in one of the 4 starting circles (the outer one counts from outside)?
        az = np.abs(z)
        rim = np.where(az >= 1, 1 / np.maximum(az, 1e-12), 0.0)
        for c in _C:
            dc = np.abs(z - c)
            rim = np.maximum(rim, np.where(dc < _R, dc / _R, 0.0))
        done = rim > 0
        if done.any():
            # rim = how close it sits to its circle's edge -> every circle gets a bright edge
            flat[idx[done]] = i + 0.8 * rim[done] ** 6
            z, idx = z[~done], idx[~done]
            if idx.size == 0:
                break
        # still in a gap: reflect it in the mirror that covers that gap
        new = z.copy()
        for o, r in _MIRRORS:
            d = z - o
            m = d.real * d.real + d.imag * d.imag < r * r
            new[m] = o + r * r / np.conj(d[m])
        z = new
    return out


def sierpinski_triangle(px, maxit, _t):
    """Every step: find which corner third the point is in and blow that corner
    up 2x to fill the whole triangle. If it's in the middle hole instead, stop.
    The step it stopped on says how small (deep) that hole is."""
    out = np.full(px.shape, float(maxit))
    flat = out.ravel()
    # barycentric coords: how much of each corner a point is made of (they add to 1)
    v = [np.exp(1j * (math.pi / 2 + 2 * math.pi * k / 3)) for k in range(3)]
    p = px.ravel()
    det = (v[1].imag - v[2].imag) * (v[0].real - v[2].real) + (v[2].real - v[1].real) * (v[0].imag - v[2].imag)
    a = ((v[1].imag - v[2].imag) * (p.real - v[2].real) + (v[2].real - v[1].real) * (p.imag - v[2].imag)) / det
    b = ((v[2].imag - v[0].imag) * (p.real - v[2].real) + (v[0].real - v[2].real) * (p.imag - v[2].imag)) / det
    w = np.stack([a, b, 1 - a - b])
    outside = (w < 0).any(0)
    flat[outside] = 0
    idx = np.flatnonzero(~outside)
    w = w[:, idx]
    for i in range(maxit):
        top = w.max(0)
        hole = top < 0.5
        if hole.any():
            flat[idx[hole]] = i + 1 + 0.8 * ((top[hole] - 1 / 3) * 6) ** 6
            idx, w = idx[~hole], w[:, ~hole]
            if idx.size == 0:
                break
        w = 2 * w - (w >= 0.5)  # zoom the corner it's in up to full size
    return out


def sierpinski_carpet(px, maxit, _t):
    """Cut the square into a 3x3 grid; the middle square is a hole, the other 8
    get the same treatment, forever."""
    out = np.full(px.shape, float(maxit))
    flat = out.ravel()
    u, v = (px.real.ravel() + 1) / 2, (px.imag.ravel() + 1) / 2
    outside = (u < 0) | (u >= 1) | (v < 0) | (v >= 1)
    flat[outside] = 0
    idx = np.flatnonzero(~outside)
    u, v = u[idx], v[idx]
    for i in range(maxit):
        u, v = u * 3, v * 3
        du, dv = np.floor(u), np.floor(v)
        u, v = u - du, v - dv
        hole = (du == 1) & (dv == 1)
        if hole.any():
            edge = np.maximum(np.abs(u[hole] - 0.5), np.abs(v[hole] - 0.5)) * 2
            flat[idx[hole]] = i + 1 + 0.8 * edge ** 6
            keep = ~hole
            idx, u, v = idx[keep], u[keep], v[keep]
            if idx.size == 0:
                break
    return out


def make_ifs(maps):
    """Shapes built from shrunken copies of themselves (dragons, Sierpinski pentagon...).

    maps = list of (a, b) meaning "z -> a*z + b": shrink/rotate by a, then move by b.
    The shape is exactly those copies glued together. To draw any zoomed-in view we
    walk the tree of copies-of-copies, skipping every branch that's off screen, and
    stop splitting once a copy is smaller than a pixel. That's why it stays sharp
    no matter how deep the zoom goes.
    """
    A = np.array([m[0] for m in maps], complex)
    B = np.array([m[1] for m in maps], complex)
    n = len(maps)
    # find a circle the whole shape fits in by throwing random points at it
    rng = np.random.default_rng(1)
    z = np.zeros(4000, complex)
    pts = []
    for it in range(80):
        k = rng.integers(n, size=z.size)
        z = A[k] * z + B[k]
        if it > 30:
            pts.append(z)
    pts = np.concatenate(pts)
    c0 = complex(pts.mean())
    R0 = float(np.abs(pts - c0).max()) * 1.1

    def render(px, maxit, _t):
        H, W = px.shape
        x0, x1 = px[0, 0].real, px[0, -1].real
        ytop, ybot = px[0, 0].imag, px[-1, 0].imag
        pxw, pxh = (x1 - x0) / (W - 1), (ytop - ybot) / (H - 1)
        pix = max(pxw, pxh)
        view = max(x1 - x0, ytop - ybot)
        na, nb = np.ones(1, complex), np.zeros(1, complex)
        val = np.zeros(1)
        cov = np.zeros(H * W)
        tot = np.zeros(H * W)
        digits = np.arange(n) / max(n - 1, 1)
        for _ in range(600):
            cen = na * c0 + nb
            r = np.abs(na) * R0
            vis = ((cen.real + r > x0 - pix) & (cen.real - r < x1 + pix)
                   & (cen.imag + r > ybot - pix) & (cen.imag - r < ytop + pix))
            na, nb, val, cen, r = na[vis], nb[vis], val[vis], cen[vis], r[vis]
            leaf = r < pix * 0.5
            if (~leaf).sum() * n > 3_000_000:  # safety valve: never blow up memory
                leaf[:] = True
            if leaf.any():
                col = np.rint((cen[leaf].real - x0) / pxw).astype(int)
                row = np.rint((ytop - cen[leaf].imag) / pxh).astype(int)
                ok = (col >= 0) & (col < W) & (row >= 0) & (row < H)
                lin = row[ok] * W + col[ok]
                cov += np.bincount(lin, minlength=H * W)
                tot += np.bincount(lin, val[leaf][ok], minlength=H * W)
                na, nb, val, r = na[~leaf], nb[~leaf], val[~leaf], r[~leaf]
            if na.size == 0:
                break
            # shade by which copy-of-copy a point is in, weighted toward copies about
            # the size of the screen, so colors change smoothly as we zoom
            x = r / view
            wgt = np.minimum(x, 1 / (x * x))
            val = (val[:, None] + wgt[:, None] * digits).ravel()
            nb = (na[:, None] * B + nb[:, None]).ravel()
            na = (na[:, None] * A).ravel()
        covered = (cov > 0).reshape(H, W)
        shade = (tot / np.maximum(cov, 1)).reshape(H, W)
        c = covered.astype(float)
        nbrs = (np.roll(c, 1, 0) + np.roll(c, -1, 0) + np.roll(c, 1, 1) + np.roll(c, -1, 1)) / 4
        edge = 1 - nbrs  # glowing outline where the shape meets empty space
        return np.where(covered, 20 + 5 * shade + 12 * edge, 0.0)

    fixed = B[0] / (1 - A[0]) if abs(1 - A[0]) > 1e-9 else 0j  # a point on the shape
    t = A[0] * (B[1] / (1 - A[1])) + B[0]
    return render, (c0.real, c0.imag), R0 * 0.75, [(t.real, t.imag), (fixed.real, fixed.imag)]


PHI = (1 + 5 ** 0.5) / 2
_GR = (1 / PHI) ** (1 / PHI)                     # golden dragon's shrink factor
_GA = math.acos((1 + _GR ** 2 - _GR ** 4) / (2 * _GR))
_GB = math.acos((1 + _GR ** 4 - _GR ** 2) / (2 * _GR ** 2))
_PS = (3 - 5 ** 0.5) / 2                         # pentagon copies are this big
_PV = [np.exp(1j * (math.pi / 2 + 2 * math.pi * k / 5)) for k in range(5)]

heighway, *_HW = make_ifs([((1 + 1j) / 2, 0), (-(1 - 1j) / 2, 1)])
golden, *_GD = make_ifs([(_GR * np.exp(1j * _GA), 0), (_GR ** 2 * np.exp(1j * (math.pi - _GB)), 1)])
levy, *_LV = make_ifs([((1 - 1j) / 2, 0), ((1 + 1j) / 2, (1 - 1j) / 2)])
pentagon, *_PG = make_ifs([(_PS, (1 - _PS) * v) for v in _PV])

# name, function, where to start, how wide the opening shot is, deep points worth zooming into
FRACTALS = [
    ("Mandelbrot", mandelbrot, (-0.5, 0.0), 1.6,
     [(-0.7436438870371587, 0.1318259042053119), (-0.7453, 0.1127),
      (-1.2506653, 0.0201272), (-0.1607013, 1.0375665)]),
    ("Julia", julia, (0.0, 0.0), 1.6, [(0.0, 0.0), (0.4, 0.2), (-0.3, 0.3)]),
    ("Burning Ship", burning_ship, (-0.45, -0.5), 1.8, [(-1.7625, 0.0285), (-1.7495, 0.0)]),
    ("Tricorn", tricorn, (-0.3, 0.0), 1.8, [(-1.0, 0.25), (0.25, 0.75)]),
    ("Multibrot z³", multibrot, (0.0, 0.0), 1.5, [(-0.5, 0.6), (0.35, 0.6)]),
    ("Newton z³-1", newton, (0.0, 0.0), 1.8, [(0.0, 0.0), (-0.5, 0.0)]),
    ("Phoenix", phoenix, (0.0, 0.0), 1.5, [(0.0, 0.62), (0.53, 0.0)]),
    ("Golden Dragon", golden, *_GD),
    ("Heighway Dragon", heighway, *_HW),
    ("Lévy C Curve", levy, *_LV),
    ("Apollonian Gasket", apollonian, (0.0, 0.0), 1.05, [(0.0, 1.0), (0.0, (1 - _R) / 2)]),
    ("Sierpinski Triangle", sierpinski_triangle, (0.0, -0.1), 1.05, [(0.0, 1.0), (0.0, -0.5)]),
    ("Sierpinski Carpet", sierpinski_carpet, (0.0, 0.0), 1.05, [(1 / 3, 1 / 3), (-1 / 3, 1 / 9)]),
    ("Sierpinski Pentagon", pentagon, *_PG),
]

# --------------------------------------------------------------- colors ----


def _grad(stops, n=256):
    """Build a looping n-color palette from a list of RGB stops."""
    stops = np.array(stops + [stops[0]], dtype=float)
    pos = np.linspace(0, len(stops) - 1, n, endpoint=False)
    i = pos.astype(int)
    f = (pos - i)[:, None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(int)


PALETTES = [
    ("Glow", None),  # black -> audio color, built on the fly by glow_cells()
    ("Electric", _grad([(0, 7, 100), (32, 107, 203), (237, 255, 255), (255, 170, 0), (60, 2, 60)])),
    ("Fire", _grad([(10, 0, 0), (180, 20, 0), (255, 140, 0), (255, 240, 120), (90, 0, 40)])),
    ("Neon", _grad([(10, 0, 30), (255, 0, 160), (0, 255, 220), (120, 0, 255), (255, 255, 80)])),
    ("Ocean", _grad([(0, 10, 30), (0, 80, 120), (40, 200, 200), (220, 255, 250), (10, 40, 90)])),
    ("Mono", _grad([(0, 0, 0), (255, 255, 255)])),
]


def build_cells(pal):
    """Every possible (top color, bottom color) pair as a ready-to-print string.
    The '▀' glyph paints the top half with the foreground color and the bottom
    half with the background, so one character cell = two pixels."""
    fg = [f"\x1b[38;2;{r};{g};{b}m" for r, g, b in pal]
    bg = [f"\x1b[48;2;{r};{g};{b}m" for r, g, b in pal]
    fg.append("\x1b[38;2;0;0;0m")  # index 256 = "inside the set" = black
    bg.append("\x1b[48;2;0;0;0m")
    return [f + b + "▀" for f in fg for b in bg]


GLOW_LEVELS = 64
PURPLE_HUE, ORANGE_HUE = 275, 390  # 390 = 30 (orange), going the long way through pink/red
_glow_cache = {}


def glow_cells(tint):
    """Cells for the Glow look: black fading up to the audio color, with a
    white-hot core at the very brightest. tint 0 = purple (bass), 1 = orange (highs).
    Cached in 32 steps so we're not rebuilding thousands of strings every frame."""
    q = int(round(tint * 31))
    if q not in _glow_cache:
        hue = (PURPLE_HUE + (ORANGE_HUE - PURPLE_HUE) * q / 31) % 360 / 360
        color = np.array(colorsys.hsv_to_rgb(hue, 1.0, 1.0))
        b = np.linspace(0, 1, GLOW_LEVELS)[:, None]
        rgb = color * b ** 1.4 + 0.75 * b ** 8  # color rises, then burns toward white
        _glow_cache[q] = build_cells((np.clip(rgb, 0, 1) * 255).astype(int))
    return _glow_cache[q]


def shrink(a, ss):
    """Average each ss x ss block of pixels into one (supersampling)."""
    if ss == 1:
        return a
    h, w = a.shape
    return a.reshape(h // ss, ss, w // ss, ss).mean((1, 3))


def blur(a):
    """Cheap soft blur (a few shifted copies averaged) used for the bloom halo."""
    for _ in range(2):
        a = (a + np.roll(a, 1, 0) + np.roll(a, -1, 0) + np.roll(a, 1, 1) + np.roll(a, -1, 1)) / 5
    return a


# ------------------------------------------------------------- terminal ----

class Term:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)

    def __enter__(self):
        tty.setcbreak(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        return self

    def __exit__(self, *a):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def key(self, timeout=0.0):
        if select.select([self.fd], [], [], timeout)[0]:
            k = os.read(self.fd, 8).decode(errors="ignore")
            if k.startswith("\x1b["):
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(k[2:3], "")
            return k
        return None


def menu(term):
    """Startup picker: arrow keys / number, Enter to start."""
    sel = 0
    while True:
        out = ["\x1b[H\x1b[2J\x1b[0m\n  \x1b[1mfractalviz\x1b[0m  - pick a fractal\n"]
        for i, f in enumerate(FRACTALS):
            mark = "\x1b[7m" if i == sel else ""
            out.append(f"   {mark} {i + 1:>2}  {f[0]:<20}\x1b[0m\n")
        out.append("\n  ↑/↓ or number, Enter to start, q to quit\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        k = term.key(None)
        if k in ("q", "\x1b"):
            return None
        if k == "up":
            sel = (sel - 1) % len(FRACTALS)
        elif k == "down":
            sel = (sel + 1) % len(FRACTALS)
        elif k in ("\n", "\r", " "):
            return sel
        elif k and k.isdigit() and 1 <= int(k) <= min(9, len(FRACTALS)):
            return int(k) - 1


# ----------------------------------------------------------------- main ----

class Zoom:
    """Where the camera is and where it's heading."""

    MIN_SCALE = 2e-13  # past this, 64-bit floats turn the image to mush

    def __init__(self, fractal_index):
        self.set_fractal(fractal_index)

    def set_fractal(self, i):
        self.fi = i
        _, self.fn, start, self.start_scale, self.targets = FRACTALS[i]
        self.cx, self.cy = start
        self.scale = self.start_scale
        self.ti = 0
        self.rewinding = False
        self.from_center = start

    @property
    def target(self):
        return self.targets[self.ti % len(self.targets)]

    def step(self, dt, speed):
        if self.rewinding:
            # zoom back out quickly, then pick the next target
            self.scale *= math.exp(4.0 * dt)
            p = min(1.0, math.log(self.scale / self.MIN_SCALE) / math.log(self.start_scale / self.MIN_SCALE))
            tx, ty = self.target
            self.cx = self.from_center[0] + (tx - self.from_center[0]) * p
            self.cy = self.from_center[1] + (ty - self.from_center[1]) * p
            if self.scale >= self.start_scale:
                self.scale = self.start_scale
                self.rewinding = False
            return
        self.scale *= math.exp(-speed * dt)
        # glide toward the target point; ease harder as we get deeper
        tx, ty = self.target
        k = min(1.0, dt * 1.5)
        self.cx += (tx - self.cx) * k
        self.cy += (ty - self.cy) * k
        if self.scale < self.MIN_SCALE:
            self.ti += 1
            self.from_center = (self.cx, self.cy)
            self.rewinding = True

    def steer(self, img, inside):
        """Keep the camera on the interesting edge of the fractal.

        Points that take the longest to escape (without never escaping) sit
        right on the fractal's border, where the endless detail is. Each frame
        we look for the highest such point near the middle of the screen and
        make it the new target, so the zoom can't wander into an empty void.
        """
        if self.rewinding:
            return
        h, w = img.shape
        depth = math.log10(self.start_scale / self.scale)
        if depth < 1.0:  # let the opening shot head for the hand-picked spot first
            return
        yy, xx = np.ogrid[0:h, 0:w]
        dist = ((xx - w / 2) / w) ** 2 + ((yy - h / 2) / h) ** 2
        score = np.where(inside, 0.0, img) * np.exp(-dist / 0.05)
        if score.max() <= 0:
            return
        py, px = np.unravel_index(score.argmax(), score.shape)
        aspect = w / h
        nx = self.cx + (px / (w - 1) - 0.5) * 2 * self.scale * aspect
        ny = self.cy - (py / (h - 1) - 0.5) * 2 * self.scale
        self.targets = list(self.targets)
        self.targets[self.ti % len(self.targets)] = (nx, ny)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--fractal", type=int, choices=range(1, len(FRACTALS) + 1),
                    help="start with this fractal: " + ", ".join(f"{i + 1}={f[0]}" for i, f in enumerate(FRACTALS)))
    ap.add_argument("--mic", action="store_true", help="listen to the microphone instead of speakers")
    ap.add_argument("--fps", type=int, default=144, help="frame rate cap (default 144)")
    ap.add_argument("--detail", type=int, choices=(1, 2, 3), default=2,
                    help="pixels computed per screen pixel, per side (default 2 = 4x smoother)")
    args = ap.parse_args()

    if not sys.stdout.isatty():
        sys.exit("fractalviz needs to run in a terminal")

    audio = Audio(args.mic)
    pace = Pace()
    resized = [True]
    signal.signal(signal.SIGWINCH, lambda *a: resized.__setitem__(0, True))

    try:
        with Term() as term:
            fi = args.fractal - 1 if args.fractal else menu(term)
            if fi is None:
                return
            zoom = Zoom(fi)
            pal_i = 0
            cells = None
            glow_range = None  # smoothed brightness range so the glow doesn't flicker
            base_speed = 0.35  # how fast "steady" is (zoom rate per second)
            hud_pinned = False
            hud_until = time.time() + 5  # bar hides itself 5s after the last keypress
            show_hud = True
            ss = args.detail
            color_shift = 0.0
            last = time.time()
            t0 = last
            fps_avg = args.fps

            while True:
                # ---- input
                k = term.key(0)
                if k is not None:
                    hud_until = time.time() + 5
                while k is not None:
                    if k in ("q", "\x1b"):
                        return
                    if k.isdigit() and 1 <= int(k) <= min(9, len(FRACTALS)):
                        zoom.set_fractal(int(k) - 1)
                    elif k in ("\t", "right"):
                        zoom.set_fractal((zoom.fi + 1) % len(FRACTALS))
                    elif k == "left":
                        zoom.set_fractal((zoom.fi - 1) % len(FRACTALS))
                    elif k == "p":
                        pal_i = (pal_i + 1) % len(PALETTES)
                        cells = build_cells(PALETTES[pal_i][1]) if PALETTES[pal_i][1] is not None else None
                    elif k in ("+", "="):
                        base_speed = min(2.0, base_speed * 1.2)
                    elif k in ("-", "_"):
                        base_speed = max(0.05, base_speed / 1.2)
                    elif k == "]":
                        pace.sensitivity = min(4.0, pace.sensitivity * 1.25)
                    elif k == "[":
                        pace.sensitivity = max(0.1, pace.sensitivity / 1.25)
                    elif k == "h":
                        hud_pinned = not hud_pinned
                        if not hud_pinned:
                            hud_until = 0  # unpinning hides it right away
                    elif k == "d":
                        ss = ss % 3 + 1
                    elif k == "r":
                        zoom.set_fractal(zoom.fi)
                    k = term.key(0)

                want_hud = hud_pinned or time.time() < hud_until
                if want_hud != show_hud:
                    show_hud = want_hud
                    resized[0] = True
                if resized[0]:
                    cols, rows = shutil.get_terminal_size()
                    resized[0] = False
                    sys.stdout.write("\x1b[0m\x1b[2J")
                img_rows = rows - (1 if show_hud else 0)
                W, H = cols, img_rows * 2  # two pixels per character row

                # ---- timing + audio
                now = time.time()
                dt = min(0.1, now - last)
                last = now
                fps_avg += (1 / max(dt, 1e-3) - fps_avg) * 0.05
                mult, mid = pace.update(audio.bands() if audio.ok else (0, 0, 0), dt)
                zoom.step(dt, base_speed * mult)
                color_shift += dt * (6 + 30 * max(mid, 0))  # mids nudge the colors

                # ---- compute the fractal
                # detail: compute ss x ss sample points per screen pixel, then
                # average them, which smooths jagged edges and shows finer filaments
                aspect = W / H
                xs = np.linspace(zoom.cx - zoom.scale * aspect, zoom.cx + zoom.scale * aspect, W * ss)
                ys = np.linspace(zoom.cy + zoom.scale, zoom.cy - zoom.scale, H * ss)
                px = xs[None, :] + 1j * ys[:, None]
                depth = max(0.0, math.log10(zoom.start_scale / zoom.scale))
                maxit = int(min(1500, 150 + 75 * depth))  # deeper = more steps = finer detail
                img_hi = zoom.fn(px, maxit, now - t0)
                inside_hi = img_hi >= maxit
                inside_frac = shrink(inside_hi.astype(float), ss)
                inside = inside_frac > 0.5
                zoom.steer(shrink(np.where(inside_hi, 0.0, img_hi), ss), inside)

                if PALETTES[pal_i][1] is None:
                    # ---- Glow: the slower a point escapes, the closer it is to the
                    # fractal's edge, so it gets brighter. Everything else sinks to black.
                    esc = img_hi[~inside_hi]
                    if esc.size:
                        lo, hi = np.percentile(esc, 2), np.percentile(esc, 99.7)
                        if glow_range is None:
                            glow_range = [lo, hi]
                        glow_range[0] += (lo - glow_range[0]) * min(1.0, dt * 3)
                        glow_range[1] += (hi - glow_range[1]) * min(1.0, dt * 3)
                    lo, hi = glow_range or (0, 1)
                    # log curve: escape counts bunch up at the low end, this spreads them out
                    v = np.log1p(np.clip(img_hi - lo, 0, None)) / np.log1p(max(hi - lo, 1e-9))
                    v = np.clip(v, 0, 1) ** 2.2     # push the background down to black
                    v[inside_hi] = 0
                    v = shrink(v, ss)
                    v = v + 0.9 * blur(blur(v))     # bloom: light spills onto neighbours
                    v *= 1 + 0.35 * max(pace.bass, 0)  # bass hits pulse the brightness
                    n = GLOW_LEVELS
                    idx = (np.clip(v, 0, 1) * (n - 1)).astype(int)
                    idx[inside & (v < 0.02)] = n  # deep inside the set = pure black
                    cells = glow_cells(pace.tint)
                else:
                    # ---- classic palettes: log-ish stretch keeps bands nice at any depth
                    n = 256
                    s = shrink(np.where(inside_hi, 0.0, np.sqrt(np.maximum(img_hi, 0))), ss)
                    s /= np.maximum(1 - inside_frac, 1e-9)  # average of the escaping samples only
                    idx = (s * 24 + color_shift).astype(int) % n
                    idx[inside] = n
                pair = idx[0::2] * (n + 1) + idx[1::2]
                # a cell the same colors as the one before it only needs the bare "▀",
                # not the color codes again: much less for the terminal to chew on
                same = np.zeros(pair.shape, bool)
                same[:, 1:] = pair[:, 1:] == pair[:, :-1]
                out = ["\x1b[H"]
                for r, (row, srow) in enumerate(zip(pair.tolist(), same.tolist())):
                    out.append("".join(["▀" if s else cells[v] for v, s in zip(row, srow)]))
                    out.append("\x1b[0m\n" if r < len(pair) - 1 else "\x1b[0m")
                if show_hud:
                    def bar(v, n=8):
                        v = max(0.0, min(1.0, v))
                        return "█" * int(v * n) + "·" * (n - int(v * n))
                    def tint_bar(t):
                        return "purple " + "─" * int(t * 6) + "●" + "─" * (6 - int(t * 6)) + " orange"
                    status = "" if audio.ok else "  [no audio: steady pace]"
                    hud = (f" {FRACTALS[zoom.fi][0]} │ {fps_avg:3.0f}/{args.fps}fps │ detail {ss}x │ speed {mult:4.2f}x │ bass {bar(pace.bass / 2)}"
                           f" treble {bar(pace.treble / 2)} │ zoom 1e{depth:4.1f} │ {PALETTES[pal_i][0]}{' ' + tint_bar(pace.tint) if PALETTES[pal_i][1] is None else ''}"
                           f" │ 1-9 Tab p d +- [] h{'(pinned)' if hud_pinned else ''} q{status}")
                    out.append(f"\n\x1b[0m\x1b[7m{hud[:cols].ljust(cols)}\x1b[0m")
                sys.stdout.write("".join(out))
                sys.stdout.flush()

                # ---- frame cap
                spare = 1 / args.fps - (time.time() - now)
                if spare > 0:
                    time.sleep(spare)
    except KeyboardInterrupt:
        pass
    finally:
        audio.close()


if __name__ == "__main__":
    main()
