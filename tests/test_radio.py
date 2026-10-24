import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio import RadioQueue


def track(i):
    return {"id": f"t{i}", "title": f"Faixa {i}"}


class FakeRadio:
    """API falsa: cada chamada fica pendente até `release()`."""

    def __init__(self):
        self.calls: list[str] = []
        self.pending: list[tuple[asyncio.Future, list[dict]]] = []

    async def __call__(self, seed):
        self.calls.append(seed)
        fut = asyncio.get_running_loop().create_future()
        self.pending.append((fut, self.answers.pop(0)))
        return await fut

    answers: list = []

    def release(self, n=0):
        fut, _ = self.pending[n]
        fut.set_result(self.pending[n][1])


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class RadioQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_stale_response_is_discarded_after_switching_radio(self):
        q, api = RadioQueue(), FakeRadio()
        api.answers = [[track(10), track(11)], [track(20)]]
        q.reset([track(1)], 0)
        old = asyncio.ensure_future(q.refill(api))
        await settle()
        q.reset([track(2)], 0)  # troca de rádio com a busca pendente
        new = asyncio.ensure_future(q.refill(api))
        await settle()
        self.assertEqual(api.calls, ["t1", "t2"])
        api.release(0)  # a resposta antiga chega primeiro
        self.assertEqual(await old, 0)
        self.assertEqual([t["id"] for t in q.tracks], ["t2"])
        api.release(1)
        self.assertEqual(await new, 1)
        self.assertEqual([t["id"] for t in q.tracks], ["t2", "t20"])

    async def test_concurrent_refills_share_one_fetch(self):
        q, api = RadioQueue(), FakeRadio()
        api.answers = [[track(2), track(3)]]
        q.reset([track(1)], 0)
        waiters = [asyncio.ensure_future(q.refill(api)) for _ in range(5)]
        await settle()
        self.assertEqual(len(api.calls), 1)
        self.assertTrue(q.refilling)
        api.release()
        self.assertEqual(await asyncio.gather(*waiters), [2] * 5)
        self.assertEqual([t["id"] for t in q.tracks], ["t1", "t2", "t3"])
        self.assertFalse(q.refilling)

    async def test_no_duplicates(self):
        q, api = RadioQueue(), FakeRadio()
        api.answers = [[track(1), track(2), track(2)]]
        q.reset([track(1)], 0)
        task = asyncio.ensure_future(q.refill(api))
        await settle()
        api.release()
        await task
        self.assertEqual([t["id"] for t in q.tracks], ["t1", "t2"])

    async def test_end_of_queue_waits_for_radio(self):
        q, api = RadioQueue(), FakeRadio()
        api.answers = [[track(2)]]
        q.reset([track(1)], 0)
        self.assertEqual(q.remaining, 0)
        task = asyncio.ensure_future(q.refill(api))
        await settle()
        self.assertFalse(task.done())
        api.release()
        await task
        self.assertEqual(q.remaining, 1)

    async def test_failure_propagates_and_allows_retry(self):
        q = RadioQueue()
        q.reset([track(1)], 0)

        async def boom(seed):
            raise RuntimeError("rede")

        with self.assertRaises(RuntimeError):
            await q.refill(boom)
        self.assertFalse(q.refilling)
        api = FakeRadio()
        api.answers = [[track(2)]]
        task = asyncio.ensure_future(q.refill(api))
        await settle()
        api.release()
        self.assertEqual(await task, 1)

    async def test_cancelling_a_waiter_keeps_the_fetch_running(self):
        q, api = RadioQueue(), FakeRadio()
        api.answers = [[track(2)]]
        q.reset([track(1)], 0)
        waiter = asyncio.ensure_future(q.refill(api))
        await settle()
        waiter.cancel()
        await settle()
        self.assertTrue(q.refilling)
        api.release()
        await settle()
        self.assertEqual([t["id"] for t in q.tracks], ["t1", "t2"])

    async def test_long_session_bounds_history(self):
        q = RadioQueue(max_history=10)
        q.reset([track(0)], 0)
        n = 1
        for _ in range(200):
            batch = [track(n + k) for k in range(5)]
            n += 5

            async def fetch(seed, batch=batch):
                return batch

            await q.refill(fetch)
            q.qi = len(q.tracks) - 1  # ouviu tudo
        self.assertLessEqual(len(q.tracks), 10 + 1 + 5)  # histórico + atual + um lote
        self.assertEqual(q.tracks[q.qi]["id"], f"t{n - 1}")
        self.assertEqual(q.ids, {t["id"] for t in q.tracks})


if __name__ == "__main__":
    unittest.main()
