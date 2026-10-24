"""Estado persistente do app (volume, tema etc.) em JSON, com gravação atômica."""
import json
import logging
import os
import tempfile

log = logging.getLogger("ytradio")


def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("estado ilegível em %s; começando do zero", path)
        return {}


def save_state(path: str, d: dict) -> None:
    """Grava em arquivo temporário na mesma pasta e troca de uma vez, para não corromper o estado."""
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="state.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)
        tmp = None
    except Exception:
        log.exception("não foi possível salvar %s", path)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
