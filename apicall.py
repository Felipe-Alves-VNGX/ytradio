"""Política de repetição das chamadas à API do YouTube Music (sem dependências de rede ou UI)."""
import logging
import time
from typing import Callable

log = logging.getLogger("ytradio")

AUTH_ERROR_HINTS = ("401", "403", "unauthorized", "forbidden", "logged out", "sign in", "authentication", "login")
NETWORK_DELAYS = (0.5, 1.5)  # pausas entre as novas tentativas após falha de rede


def classify(e: Exception) -> str:
    """'auth' (sessão/cookies), 'network' (rede, timeout) ou 'data' (resposta inesperada, bug, argumento)."""
    if getattr(e, "status_code", None) in (401, 403) or any(h in str(e).lower() for h in AUTH_ERROR_HINTS):
        return "auth"
    if isinstance(e, OSError):  # inclui requests.RequestException, socket e timeouts
        return "network"
    return "data"


def run_with_retries(label: str, attempt: Callable, reconnect: Callable, auth_error: Callable[[Exception], Exception],
                     passthrough: tuple[type[Exception], ...] = (), sleep: Callable[[float], None] = time.sleep,
                     delays: tuple[float, ...] = NETWORK_DELAYS):
    """Chama `attempt()`. Auth: relê os cookies (`reconnect`) e tenta uma vez; rede: repete com pausa; dados: não repete."""
    reconnected = False
    pauses = iter(delays)
    while True:
        try:
            return attempt()
        except passthrough:
            raise
        except Exception as e:
            kind = classify(e)
            log.warning("%s: falha de %s (%s: %.200s)", label, kind, type(e).__name__, e)
            if kind == "auth":
                if reconnected:
                    raise auth_error(e) from e
                reconnected = True
                reconnect()  # cookies podem ter rotacionado
            elif kind == "network":
                pause = next(pauses, None)
                if pause is None:
                    raise
                sleep(pause)
            else:
                raise
