import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import big_art, norm, parse_len
from state import load_state, save_state


class NormTest(unittest.TestCase):
    def test_full_track(self):
        t = norm({
            "videoId": "abc", "title": "Música", "artists": [{"name": "A"}, {"name": "B"}, {"name": None}],
            "album": {"name": "Álbum"}, "duration": "3:25",
            "thumbnails": [{"url": "https://x/small", "width": 60}, {"url": "https://x/big", "width": 500}],
        })
        self.assertEqual(t, {"kind": "track", "id": "abc", "title": "Música", "artist": "A, B", "album": "Álbum",
                             "dur": 205, "art": "https://x/big"})

    def test_fallbacks(self):
        t = norm({"videoId": "v", "author": "Canal", "album": "Single"})
        self.assertEqual((t["title"], t["artist"], t["album"], t["dur"]), ("?", "Canal", "Single", 0))
        self.assertEqual(t["art"], "https://i.ytimg.com/vi/v/hqdefault.jpg")

    def test_unavailable_or_without_id(self):
        self.assertIsNone(norm({"title": "sem id"}))
        self.assertIsNone(norm({"videoId": "v", "isAvailable": False}))

    def test_parse_len_and_art(self):
        self.assertEqual((parse_len("1:02:03"), parse_len("x"), parse_len(None)), (3723, 0, 0))
        self.assertEqual(big_art("https://lh3.googleusercontent.com/a=w60-h60-l90-rj"),
                         "https://lh3.googleusercontent.com/a=w544-h544-l90-rj")


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "state.json")

    def test_roundtrip_and_no_temp_left(self):
        save_state(self.path, {"volume": 70})
        self.assertEqual(load_state(self.path), {"volume": 70})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["state.json"])

    def test_failed_save_keeps_previous_state(self):
        save_state(self.path, {"volume": 70})
        with self.assertLogs("ytradio", "ERROR"):
            save_state(self.path, {"x": object()})
        self.assertEqual(load_state(self.path), {"volume": 70})
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["state.json"])

    def test_missing_and_corrupt(self):
        self.assertEqual(load_state(self.path), {})
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{truncado")
        with self.assertLogs("ytradio", "ERROR"):
            self.assertEqual(load_state(self.path), {})


if __name__ == "__main__":
    unittest.main()
