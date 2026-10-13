"""Testa as interações de mouse do ytradio (Textual Pilot, headless, dados e mpv falsos).
uso: python -I mouse_test.py"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ytradio
import shot_data as D

ytradio.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
CALLS = []

class FakeMPV:
    def __init__(self, **kw): self.pause = False; self.time_pos = 20.0; self.duration = 170.0; self.volume = kw.get("volume", 80); self.http_header_fields = []
    def event_callback(self, *_): return lambda f: f
    def play(self, *_): pass
    def seek(self, *a): CALLS.append(("mpv.seek",) + a)
    def terminate(self): pass

ytradio.mpv.MPV = FakeMPV
async def _noop(self): pass
ytradio.Mpris.start = _noop
ytradio.Api.playlists = lambda self: D.PLAYLISTS
ytradio.Api.home = lambda self: D.TRACKS
ytradio.Api.search = lambda self, q: D.TRACKS

RESULTS = []
def check(name, ok, extra=""):
    RESULTS.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))

async def main():
    app = ytradio.YTRadio()
    async with app.run_test(size=(150, 42)) as pilot:
        await pilot.pause(1.5)
        app.fill(D.TRACKS, "Busca: matanza", "search")
        app.queue = list(D.TRACKS); app.qids = {t["id"] for t in D.TRACKS}; app.qi = 1; app.current = D.TRACKS[1]
        app.on_track_changed(); app.render_all()
        await pilot.pause(0.6)
        for name in ("next", "prev", "toggle_pause", "like", "shuffle", "toggle_loop", "toggle_autoplay", "toggle_mute",
                     "pick_theme", "cycle_cover", "go_home", "focus_search", "radio_here"):
            setattr(app, f"action_{name}", (lambda n: (lambda *a: CALLS.append(n)))(name))
        app.prefetch_next = lambda: None
        app.start_from_view = lambda i: CALLS.append(("play", i))
        app.radio_from = lambda r: CALLS.append(("radio", r["title"]))

        def last(): return CALLS[-1] if CALLS else None
        async def click(sel, x, y=0, **k):
            CALLS.clear(); await pilot.click(sel, offset=(x, y), **k); await pilot.pause(0.2); return list(CALLS)

        # --- botões do cartão
        for lab, x, exp in (("anterior", 1, "prev"), ("play/pausa", 5, "toggle_pause"), ("próxima", 9, "next"),
                            ("curtir", 14, "like"), ("embaralhar", 18, "shuffle")):
            c = await click("#buttons", x); check(f"botão {lab}", exp in c, str(c))
        c = await click("#chips", 2); check("chip loop", "toggle_loop" in c, str(c))
        c = await click("#chips", 12); check("chip rádio", "toggle_autoplay" in c, str(c))
        c = await click("#vollabel", 1); check("rótulo 'vol' = mudo", "toggle_mute" in c, str(c))

        # --- atalhos clicáveis
        segs = app.query_one("#hints").segs
        for label_action in ("next", "pick_theme", "cycle_cover", "focus_search"):
            x0, x1, _ = next(sg for sg in segs if sg[2] == label_action)
            c = await click("#hints", x0 + 1); check(f"atalho clicável '{label_action}'", label_action in c, str(c))

        # --- capa e logo
        c = await click("#coverbox", 5, 5); check("clicar na capa = pausa", "toggle_pause" in c, str(c))
        c = await click("#logobox", 3, 1); check("clicar no logo = início", "go_home" in c, str(c))

        # --- barra de busca: clique e arrasto
        seek = app.query_one("#seek"); w = seek.size.width
        CALLS.clear(); await pilot.click("#seek", offset=(w // 2, 0)); await pilot.pause(0.2)
        s = [c for c in CALLS if c[0] == "mpv.seek"]
        check("clicar no meio da barra busca ~metade", bool(s) and abs(s[-1][1] - 170 * (w // 2) / (w - 1)) < 2, str(s))
        CALLS.clear()
        await pilot.mouse_down("#seek", offset=(5, 0)); await pilot.pause(0.1)
        await pilot.hover("#seek", offset=(w - 10, 0)); await pilot.pause(0.2)
        prev = seek.preview
        check("arrastar mostra prévia sem buscar ainda", prev is not None and not [c for c in CALLS if c[0] == "mpv.seek"], f"preview={prev}")
        await pilot.mouse_up("#seek", offset=(w - 10, 0)); await pilot.pause(0.2)
        s = [c for c in CALLS if c[0] == "mpv.seek"]
        check("soltar aplica a busca", len(s) == 1 and abs(s[0][1] - 170 * (w - 10) / (w - 1)) < 2 and seek.preview is None, str(s))

        # --- volume: clique e arrasto
        vol = app.query_one("#vol"); vw = vol.size.width
        await pilot.click("#vol", offset=(vw // 2, 0)); await pilot.pause(0.2)
        check("clicar no volume ajusta", abs(app.volume - 100 * (vw // 2) / (vw - 1)) < 3, f"vol={app.volume:.0f}")
        await pilot.mouse_down("#vol", offset=(2, 0)); await pilot.hover("#vol", offset=(vw - 2, 0)); await pilot.pause(0.2)
        mid = app.volume
        await pilot.mouse_up("#vol", offset=(vw - 2, 0)); await pilot.pause(0.2)
        check("arrastar volume (ao vivo)", mid > 80 and app.volume >= mid, f"vol={app.volume:.0f}")
        CALLS.clear(); app.slider_wheel(seek, 1); check("roda na busca = +5s", ("mpv.seek", 5, "relative") in CALLS, str(CALLS))
        v0 = app.volume; app.slider_wheel(vol, -1); check("roda no volume = -5", abs(app.volume - (v0 - 5)) < 0.01, f"{v0}->{app.volume}")

        # --- tabela: duplo clique, direito, ordenação
        t = app.query_one("#tbl"); y = 1
        CALLS.clear(); await pilot.click("#tbl", offset=(20, y + 2)); await pilot.pause(0.2)
        n1 = [c for c in CALLS if c[0] == "play"]
        await pilot.click("#tbl", offset=(20, y + 2)); await pilot.pause(0.2)
        n2 = [c for c in CALLS if c[0] == "play"]
        check("1º clique só seleciona; 2º toca", not n1 and n2 == [("play", 2)], f"{n1} -> {n2}")
        c = await click("#tbl", 20, y + 4, button=3); check("botão direito = rádio da faixa", c and c[0] == ("radio", D.TRACKS[4]["title"]), str(c))
        found = None
        for x in range(2, t.size.width, 3):
            app.sort = None; app.refill(); await pilot.pause(0.1)
            await pilot.click("#tbl", offset=(x, 0)); await pilot.pause(0.15)
            if app.sort and app.sort[0] == "a":
                found = x; break
        arts = [r["artist"].lower() for r in app.rows]
        check("clicar no cabeçalho ARTISTA ordena (asc)", found is not None and arts == sorted(arts), f"x={found}")
        await pilot.click("#tbl", offset=(found, 0)); await pilot.pause(0.15)
        arts = [r["artist"].lower() for r in app.rows]
        check("2º clique inverte (desc)", app.sort == ("a", True) and arts == sorted(arts, reverse=True))
        await pilot.click("#tbl", offset=(found, 0)); await pilot.pause(0.15)
        check("3º clique remove a ordenação", app.sort is None and [r["id"] for r in app.rows] == [r["id"] for r in D.TRACKS])

        # --- arrastar para reordenar a fila
        app.fill(list(app.queue), "Fila / rádio", "queue"); await pilot.pause(0.2)
        ids0 = [r["id"] for r in app.queue]; cur0 = app.current["id"]
        await pilot.mouse_down("#tbl", offset=(20, y + 4)); await pilot.hover("#tbl", offset=(20, y + 7)); await pilot.mouse_up("#tbl", offset=(20, y + 7)); await pilot.pause(0.3)
        ids1 = [r["id"] for r in app.queue]
        exp = list(ids0); exp.insert(7, exp.pop(4))
        check("arrastar linha 5→8 reordena a fila", ids1 == exp, f"{ids1[:9]}")
        check("faixa tocando continua a mesma (índice ajustado)", app.queue[app.qi]["id"] == cur0, f"qi={app.qi}")
        check("tabela reflete a nova ordem", [r["id"] for r in app.rows] == ids1)

        # --- divisória (offsets do Pilot são relativos ao widget, que se move: usar x absoluto)
        async def drag_splitter_to(abs_x, release=True):
            sp = app.query_one("#splitter")
            await pilot.hover("#splitter", offset=(abs_x - sp.region.x, 0)); await pilot.pause(0.1)
            if release:
                sp = app.query_one("#splitter")
                await pilot.mouse_up("#splitter", offset=(abs_x - sp.region.x, 0)); await pilot.pause(0.3)
        await pilot.mouse_down("#splitter"); await pilot.pause(0.1)
        await drag_splitter_to(44, release=False)
        mid_w = app.query_one("#side").size.width
        await drag_splitter_to(44)
        check("arrastar a divisória alarga a barra lateral", 43 <= app.side_w <= 45 and mid_w > 32, f"side_w={app.side_w} (durante o arrasto: {mid_w})")
        await pilot.mouse_down("#splitter"); await pilot.pause(0.1)
        await drag_splitter_to(10)
        check("largura mínima respeitada (32)", app.side_w == 32, f"side_w={app.side_w}")
        await pilot.mouse_down("#splitter"); await pilot.pause(0.1)
        await drag_splitter_to(149)
        check("largura máxima respeitada (56)", app.side_w == 56, f"side_w={app.side_w}")
        import json
        check("largura salva no state.json", json.load(open(ytradio.STATE_FILE)).get("side_w") == 56)
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} testes passaram")
    return all(RESULTS)

sys.exit(0 if asyncio.run(main()) else 1)
