"""Roda o ytradio de verdade (terminal real) com dados e áudio falsos, para capturas via preview.sh.
env: YTR_THEME, YTR_COVER, YTR_PICKER=1 (abre o seletor de tema)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shot_data as D  # noqa: E402
import ytradio  # noqa: E402

D.install(ytradio, os.environ.get("YTR_THEME") or None, os.environ.get("YTR_COVER") or None)


class Preview(ytradio.YTRadio):
    def on_mount(self):  # o Textual chama também o on_mount da classe base
        self.set_timer(1.5, self.setup)

    def setup(self):
        self.fill(D.TRACKS, "Busca: mar", "search")
        self.queue, self.qids, self.qi, self.current = list(D.TRACKS), {t["id"] for t in D.TRACKS}, 1, D.TRACKS[1]
        self.on_track_changed()
        self.query_one("#tbl").move_cursor(row=3)
        if os.environ.get("YTR_PICKER"):
            self.action_pick_theme()


Preview().run()
