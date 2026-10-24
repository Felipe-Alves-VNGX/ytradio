import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import ytradio
except Exception:  # dependências do app (textual, mpv...) não instaladas
    ytradio = None


def T(i):
    return {"kind": "track", "id": f"t{i}", "title": f"F{i}", "artist": "", "album": "", "dur": 1, "art": ""}


@unittest.skipIf(ytradio is None, "dependências do app não instaladas")
class AppQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_queue_view_follows_trim_without_new_tracks(self):
        ytradio.STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
        app = ytradio.YTRadio()
        app.api.radio = lambda seed: [T(3)]  # só uma faixa que já está na fila
        app.api.playlists = lambda: []
        app.api.home = lambda: []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.player.mute = True
            app.rq.max_history = 1
            app.rq.reset([T(0), T(1), T(2), T(3)], 3)
            app.fill(list(app.queue), "Fila / rádio", "queue")
            app.extend_radio()
            for _ in range(20):
                await pilot.pause(0.1)
            self.assertEqual([t["id"] for t in app.queue], ["t2", "t3"])
            self.assertEqual([r["id"] for r in app.rows], ["t2", "t3"])


if __name__ == "__main__":
    unittest.main()
