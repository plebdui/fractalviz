# fractalviz

A terminal music visualizer that zooms endlessly into a fractal, in full color.

The zoom runs at a steady pace. Bass hits slow it down, highs speed it up, and it
always drifts back to steady once the music calms. In the default **Glow** look,
the fractal's edges light up from black toward purple on bass and orange on highs.

## Fractals

Mandelbrot · Julia · Burning Ship · Tricorn · Multibrot z³ · Newton z³−1 · Phoenix ·
Golden Dragon · Heighway Dragon · Lévy C Curve · Apollonian Gasket ·
Sierpinski Triangle · Sierpinski Carpet · Sierpinski Pentagon

## Run it

Needs Python 3, numpy, and `parec` (PulseAudio or PipeWire) to hear what your
speakers are playing. Use a terminal with truecolor support.

```sh
python3 fractalviz.py            # pick a fractal from the menu
python3 fractalviz.py -f 8       # go straight to the Golden Dragon
python3 fractalviz.py --mic      # listen to the microphone instead
python3 fractalviz.py --detail 1 --fps 144
```

## Keys

| Key | Does |
| --- | --- |
| `1`–`9`, Tab, ←/→ | switch fractal |
| `p` | next color palette |
| `d` | detail 1x / 2x / 3x (smoother vs. faster) |
| `+` / `-` | base zoom speed |
| `[` / `]` | audio sensitivity |
| `h` | pin the info bar (it hides itself 5s after your last keypress) |
| `r` | restart the zoom |
| `q` / Esc | quit |
