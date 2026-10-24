import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apicall import classify, run_with_retries


class Auth(Exception):
    pass


class Flaky:
    def __init__(self, *errors, result="ok"):
        self.errors, self.result, self.calls = list(errors), result, 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


def run(fn, **kw):
    reconnects, sleeps = [], []
    kw.setdefault("passthrough", ())
    try:
        out = run_with_retries("t", fn, lambda: reconnects.append(1), Auth, sleep=sleeps.append, **kw)
    except Exception as e:
        out = e
    return out, len(reconnects), sleeps


class ApiCallTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify(ConnectionError("x")), "network")
        self.assertEqual(classify(TimeoutError()), "network")
        self.assertEqual(classify(KeyError("tracks")), "data")
        self.assertEqual(classify(RuntimeError("HTTP 403")), "auth")
        e = RuntimeError("boom")
        e.status_code = 401
        self.assertEqual(classify(e), "auth")

    def test_data_error_not_retried(self):
        f = Flaky(KeyError("tracks"))
        out, rec, sl = run(f)
        self.assertIsInstance(out, KeyError)
        self.assertEqual((f.calls, rec, sl), (1, 0, []))

    def test_network_retries_with_backoff_then_succeeds(self):
        f = Flaky(ConnectionError(), TimeoutError())
        out, rec, sl = run(f)
        self.assertEqual((out, f.calls, rec, sl), ("ok", 3, 0, [0.5, 1.5]))

    def test_network_gives_up(self):
        f = Flaky(*[ConnectionError()] * 5)
        out, rec, _ = run(f)
        self.assertIsInstance(out, ConnectionError)
        self.assertEqual((f.calls, rec), (3, 0))

    def test_auth_reconnects_once(self):
        f = Flaky(RuntimeError("401 Unauthorized"))
        out, rec, _ = run(f)
        self.assertEqual((out, f.calls, rec), ("ok", 2, 1))

    def test_auth_twice_raises_auth_error(self):
        f = Flaky(RuntimeError("403"), RuntimeError("403"))
        out, rec, _ = run(f)
        self.assertIsInstance(out, Auth)
        self.assertEqual((f.calls, rec), (2, 1))

    def test_passthrough_is_not_retried(self):
        f = Flaky(PermissionError("sem login"))
        out, rec, _ = run(f, passthrough=(PermissionError,))
        self.assertIsInstance(out, PermissionError)
        self.assertEqual((f.calls, rec), (1, 0))


if __name__ == "__main__":
    unittest.main()
