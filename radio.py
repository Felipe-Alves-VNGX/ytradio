"""Fila de reprodução e abastecimento do rádio infinito (sem dependências de UI ou rede)."""
import asyncio
from typing import Awaitable, Callable

from models import Track

MAX_HISTORY = 100  # faixas já tocadas mantidas antes da atual; as mais antigas saem (e podem voltar a tocar)

Fetch = Callable[[str], Awaitable[list[Track]]]


class RadioQueue:
    """Faixas, posição atual e uma única busca de rádio em andamento por vez.

    Cada `reset()` abre uma nova geração: respostas de buscas iniciadas antes dele são descartadas.
    """

    def __init__(self, max_history: int = MAX_HISTORY):
        self.tracks: list[Track] = []
        self.qi = -1
        self.ids: set[str] = set()
        self.generation = 0
        self.max_history = max_history
        self._task: asyncio.Task | None = None

    def reset(self, tracks: list[Track], qi: int = -1) -> None:
        self.generation += 1
        self._task = None  # a busca antiga segue até o fim, mas seu resultado será ignorado
        self.tracks = list(tracks)
        self.ids = {t["id"] for t in self.tracks}
        self.qi = qi

    @property
    def remaining(self) -> int:
        return len(self.tracks) - self.qi - 1

    @property
    def refilling(self) -> bool:
        return self._task is not None and not self._task.done()

    async def refill(self, fetch: Fetch) -> int:
        """Busca mais faixas a partir da última da fila. Chamadas concorrentes compartilham a mesma busca."""
        if not self.tracks:
            return 0
        if not self.refilling:
            self._task = asyncio.ensure_future(self._fetch(fetch, self.generation, self.tracks[-1]["id"]))
        return await asyncio.shield(self._task)  # cancelar quem espera não cancela a busca

    async def _fetch(self, fetch: Fetch, gen: int, seed: str) -> int:
        new = await fetch(seed)
        if gen != self.generation:
            return 0
        added = []
        for t in new:
            if t["id"] not in self.ids:
                self.ids.add(t["id"])
                added.append(t)
        self.tracks.extend(added)
        self._trim()
        return len(added)

    def _trim(self) -> None:
        drop = self.qi - self.max_history
        if drop <= 0:
            return
        self.ids.difference_update(t["id"] for t in self.tracks[:drop])
        del self.tracks[:drop]
        self.qi -= drop
