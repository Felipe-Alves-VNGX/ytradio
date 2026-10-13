"""Dados de exemplo (fictícios) e capas geradas por código, para testes e capturas de tela."""
import colorsys
import json
import os
import random
import tempfile

from PIL import Image, ImageDraw


def gen_cover(seed: str, size: int = 480) -> Image.Image:
    """Capa abstrata (céu em degradê, sol e montanhas) — sem nenhuma arte de terceiros."""
    rnd = random.Random(seed)
    h = rnd.random()
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    rgb = lambda hh, l, s: tuple(int(c * 255) for c in colorsys.hls_to_rgb(hh % 1, l, s))  # noqa: E731
    for y in range(size):
        t = y / size
        d.line([(0, y), (size, y)], fill=rgb(h + 0.08 * t, 0.18 + 0.24 * t, 0.65))
    cx, cy, rad = rnd.randint(size // 3, 2 * size // 3), rnd.randint(size // 4, size // 2), rnd.randint(size // 9, size // 5)
    d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=rgb(h + 0.5, 0.72, 0.85))
    for k in range(3):
        base, pts, x = int(size * (0.62 + 0.12 * k)), [(0, size)], 0
        while x <= size:
            pts.append((x, base - rnd.randint(0, int(size * 0.14))))
            x += size // 8
        pts.append((size, size))
        d.polygon(pts, fill=rgb(h + 0.02 * k, 0.12 + 0.05 * k, 0.5))
    return img


def T(i, title, artist, album, dur):
    return {"kind": "track", "id": f"id{i}", "title": title, "artist": artist, "album": album, "dur": dur, "art": f"gen:{album}"}


TRACKS = [
    T(1, "Horizonte de Vidro", "Maré Alta", "Cartas ao Mar", 212),
    T(2, "Noite sem Fim", "Neon Delta", "Circuito Aberto", 187),
    T(3, "Vento Sul", "Vale do Eco", "Terra Firme", 243),
    T(4, "Ondas de Neon", "Neon Delta", "Circuito Aberto", 201),
    T(5, "Casa de Sal", "Maré Alta", "Cartas ao Mar", 176),
    T(6, "Estrela Cadente", "Luz do Norte", "Aurora", 229),
    T(7, "Mapa Antigo", "Vale do Eco", "Terra Firme", 198),
    T(8, "Farol", "Maré Alta", "Cartas ao Mar", 254),
    T(9, "Rádio Fantasma", "Neon Delta", "Sinal", 167),
    T(10, "Último Trem", "Luz do Norte", "Aurora", 215),
]
PLAYLISTS = [{"playlistId": k, "title": t} for k, t in
             (("a", "Músicas curtidas"), ("b", "Foco profundo"), ("c", "Estrada à noite"),
              ("d", "Clássicos"), ("e", "Descobertas da semana"), ("f", "Treino"))]


class FakeMPV:
    """Substitui o libmpv: sem áudio, com posição fixa."""

    def __init__(self, **kw):
        self.pause, self.time_pos, self.duration = False, 83.0, 201.0
        self.volume, self.http_header_fields = kw.get("volume", 80), []

    def event_callback(self, *_):
        return lambda f: f

    def play(self, *_):
        pass

    def seek(self, *_):
        pass

    def terminate(self):
        pass


def install(ytradio, theme: str | None = None, cover: str | None = None):
    """Troca rede, áudio, MPRIS e estado por dados falsos (nunca toca no state.json real)."""
    ytradio.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
    json.dump({k: v for k, v in (("theme", theme), ("cover", cover)) if v}, open(ytradio.STATE_FILE, "w"))
    ytradio.mpv.MPV = FakeMPV

    async def _noop(self):
        pass

    ytradio.Mpris.start = _noop
    ytradio.Api.playlists = lambda self: PLAYLISTS
    ytradio.Api.home = lambda self: TRACKS
    ytradio.Api.search = lambda self, q: TRACKS
    ytradio.fetch_cover = lambda url: ytradio.cover_from_image(gen_cover(url))
