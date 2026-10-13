"""Gera um PNG do ytradio (Textual headless, dados fictícios; não mostra imagem real de capa).
uso: python shot.py saida.png [--idle] [--w 150 --h 42] [--theme album|ansi:blue|preset:nord] [--cover auto|blocks|image|off]
requer: pip install cairosvg"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cairosvg  # noqa: E402
import shot_data as D  # noqa: E402
import ytradio  # noqa: E402


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


D.install(ytradio, arg("--theme"), arg("--cover"))


async def main(out, idle, w, h):
    app = ytradio.YTRadio()
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause(1.5)
        app.fill(D.TRACKS, "Busca: mar", "search")
        if not idle:
            app.queue, app.qids, app.qi, app.current = list(D.TRACKS), {t["id"] for t in D.TRACKS}, 1, D.TRACKS[1]
            app.on_track_changed()
        app.render_all()
        await pilot.pause(4 if not idle else 0.5)
        app.render_all()
        app.query_one("#tbl").move_cursor(row=3)
        await pilot.pause(0.5)
        svg = app.export_screenshot()
    svg = re.sub(r"font-family:[^;]+;", "font-family: monospace;", svg)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=out, background_color="#14161f")
    print("ok", out)


asyncio.run(main(sys.argv[1], "--idle" in sys.argv, int(arg("--w", 150)), int(arg("--h", 42))))
