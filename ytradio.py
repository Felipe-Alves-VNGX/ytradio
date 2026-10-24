#!/usr/bin/env python3
"""ytradio — YouTube Music no terminal: biblioteca, busca e rádio infinito.

Os cookies de login são lidos AO VIVO do navegador (Firefox/Zen; Chrome no Linux; ou um cookies.txt),
o áudio vem por yt-dlp + libmpv (stream, sem baixar) e o player aparece no sistema via
MPRIS (Linux) ou SMTC (Windows, experimental).
"""
import asyncio
import colorsys
import glob
import io
import json
import locale
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

locale.setlocale(locale.LC_NUMERIC, "C")  # exigido pelo libmpv

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")


def config_dir() -> str:
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ytradio")
    return os.path.expanduser("~/.config/ytradio")


if IS_WIN:  # libmpv (mpv-2.dll): procura na pasta do app, em %APPDATA%\ytradio\mpv ou em YTRADIO_MPV_DIR
    for _d in (os.environ.get("YTRADIO_MPV_DIR"), os.path.join(config_dir(), "mpv"), os.path.dirname(os.path.abspath(__file__))):
        if _d and os.path.isdir(_d):
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(_d)
            except Exception:
                pass
try:
    import mpv  # noqa: E402
except OSError as _e:
    sys.exit("ytradio: não encontrei o libmpv (mpv-2.dll / libmpv-2.dll). Baixe o libmpv para Windows e coloque a DLL na pasta "
             f"do ytradio ou em {os.path.join(config_dir(), 'mpv')} (ou defina YTRADIO_MPV_DIR). Detalhe: {_e}")
import requests  # noqa: E402
import yt_dlp  # noqa: E402
if IS_WIN:  # o dbus_fast calcula CMSG_LEN ao importar; o socket do Windows não tem (o D-Bus só é usado no Linux)
    import socket as _socket

    if not hasattr(_socket, "CMSG_LEN"):
        _socket.CMSG_LEN = lambda n: n
    if not hasattr(_socket, "SCM_RIGHTS"):
        _socket.SCM_RIGHTS = 1
from dbus_fast import BusType, Variant  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method  # noqa: E402
from PIL import Image  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.text import Text  # noqa: E402
from textual import events, on  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.widgets import DataTable, Input, OptionList, Static  # noqa: E402
from textual.widgets.option_list import Option  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widget import Widget  # noqa: E402

try:  # imagem real no terminal (kitty/sixel); importar ANTES de o Textual iniciar (consulta o terminal)
    from textual_image.renderable import Image as _AutoRenderable  # noqa: E402
    from textual_image.renderable.sixel import Image as _SixelRenderable  # noqa: E402
    from textual_image.renderable.tgp import Image as _TGPRenderable  # noqa: E402
    from textual_image.widget import Image as GfxImage  # noqa: E402

    GRAPHICS = _AutoRenderable in (_SixelRenderable, _TGPRenderable)
except Exception:  # biblioteca ausente: só blocos de texto
    GfxImage, GRAPHICS = None, False
from yt_dlp.cookies import extract_cookies_from_browser  # noqa: E402
from ytmusicapi import YTMusic  # noqa: E402
from apicall import run_with_retries  # noqa: E402
from models import Row, Track, norm  # noqa: E402
from radio import RadioQueue  # noqa: E402
from state import load_state, save_state  # noqa: E402

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"
STATE_FILE = os.path.join(config_dir(), "state.json")
log = logging.getLogger("ytradio")
CONFIG_FILE = os.path.join(config_dir(), "config.json")


def load_config() -> dict:
    """Opções fixas (opcional): {"browser": "firefox", "profile": "...", "cookies_file": "...", "icons": "plain"}."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CFG = load_config()


def opt(name: str, env: str):
    return os.environ.get(env) or CFG.get(name) or None


FIREFOX_LIKE = {"firefox", "zen", "librewolf", "waterfox"}  # mesmo formato de cookies (sqlite sem criptografia)
BROWSER = (opt("browser", "YTRADIO_BROWSER") or "firefox").lower()
ENGINE = "firefox" if BROWSER in FIREFOX_LIKE else BROWSER
COOKIES_FILE = opt("cookies_file", "YTRADIO_COOKIES")


def firefox_bases() -> list[str]:
    h = os.path.expanduser("~")
    if IS_WIN:
        ad = os.environ.get("APPDATA") or os.path.join(h, "AppData", "Roaming")
        return [os.path.join(ad, "zen", "Profiles"), os.path.join(ad, "Mozilla", "Firefox", "Profiles")]
    if sys.platform == "darwin":
        return [h + "/Library/Application Support/zen/Profiles", h + "/Library/Application Support/Firefox/Profiles"]
    return [h + "/.var/app/app.zen_browser.zen/.zen", h + "/.zen", h + "/.mozilla/firefox",
            h + "/.var/app/org.mozilla.firefox/.mozilla/firefox", h + "/snap/firefox/common/.mozilla/firefox"]


def profile_has_login(d: str) -> bool:
    """O perfil tem cookie de login do YouTube? (trabalha numa cópia: o navegador pode estar aberto)"""
    if not os.path.exists(os.path.join(d, "cookies.sqlite")):
        return False
    tmp = tempfile.mkdtemp()
    try:
        for f in ("cookies.sqlite", "cookies.sqlite-wal"):
            if os.path.exists(os.path.join(d, f)):
                shutil.copy(os.path.join(d, f), tmp)
        db = sqlite3.connect(os.path.join(tmp, "cookies.sqlite"))
        try:
            return db.execute("select 1 from moz_cookies where name='SAPISID' and host like '%youtube.com' limit 1").fetchone() is not None
        finally:
            db.close()
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_profile() -> str | None:
    """Perfil Firefox/Zen com login do YouTube (o mais recente); None = deixa o yt-dlp escolher o padrão."""
    env = opt("profile", "YTRADIO_PROFILE")
    if env:
        return os.path.expanduser(env)
    cands: list[str] = []
    for base in firefox_bases():
        cands += [os.path.dirname(c) for c in glob.glob(os.path.join(base, "*", "cookies.sqlite"))]
    cands.sort(key=lambda d: os.path.getmtime(os.path.join(d, "cookies.sqlite")), reverse=True)
    for d in cands:
        if profile_has_login(d):
            return d
    return cands[0] if cands else None


PROFILE = find_profile() if ENGINE == "firefox" else None


class CookieError(Exception):
    """Sem cookies de login do YouTube: a mensagem explica como resolver."""


def cookie_help(detail: str) -> str:
    msg = f"Não consegui ler o login do YouTube ({ENGINE}): {detail}.\n\n"
    if ENGINE in ("chrome", "chromium", "edge", "brave", "opera", "vivaldi"):
        msg += ("O Chrome (v127+) no Windows criptografa os cookies de um jeito que o yt-dlp não consegue ler.\n\n"
                "Soluções:\n• Instale o Firefox (ou o Zen), entre no music.youtube.com nele e use "
                "YTRADIO_BROWSER=firefox (ou \"browser\" no config.json);\n• ou exporte um cookies.txt do Chrome e "
                "aponte para ele com YTRADIO_COOKIES.")
    else:
        msg += ("Entre no music.youtube.com neste navegador, ou aponte o perfil com YTRADIO_PROFILE, ou use um cookies.txt "
                "com YTRADIO_COOKIES.")
    return msg + f"\n\nConfig: {CONFIG_FILE}"


LOGIN_URL = "https://music.youtube.com/"
BROWSER_EXES = {  # nome -> (comandos no PATH, caminhos relativos nas pastas de programas do Windows)
    "firefox": (["firefox"], [r"Mozilla Firefox\firefox.exe"]),
    "zen": (["zen", "zen-browser"], [r"Zen Browser\zen.exe"]),
    "librewolf": (["librewolf"], [r"LibreWolf\librewolf.exe"]),
    "waterfox": (["waterfox"], [r"Waterfox\waterfox.exe"]),
    "chrome": (["google-chrome", "chrome"], [r"Google\Chrome\Application\chrome.exe"]),
    "chromium": (["chromium", "chromium-browser"], []),
    "edge": (["microsoft-edge", "msedge"], [r"Microsoft\Edge\Application\msedge.exe"]),
    "brave": (["brave", "brave-browser"], [r"BraveSoftware\Brave-Browser\Application\brave.exe"]),
}


def open_login_browser() -> str:
    """Abre o music.youtube.com no navegador configurado (o do login) para o usuário entrar de novo.
    Devolve o nome do navegador aberto (ou "padrão" se caiu no navegador padrão do sistema)."""
    import subprocess
    import webbrowser

    cmds, rels = BROWSER_EXES.get(BROWSER, ([BROWSER], []))
    exe = next((w for w in map(shutil.which, cmds) if w), None)
    if not exe and IS_WIN:
        roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        roots += [os.path.join(os.environ["LOCALAPPDATA"], "Programs")] if os.environ.get("LOCALAPPDATA") else []
        exe = next((p for r in roots if r for rel in rels if os.path.isfile(p := os.path.join(r, rel))), None)
    if exe:
        try:
            subprocess.Popen([exe, LOGIN_URL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            return BROWSER
        except OSError:
            pass
    webbrowser.open(LOGIN_URL)
    return "padrão"


def load_cookie_jar():
    """Cookies de login: arquivo cookies.txt (YTRADIO_COOKIES) ou lidos AO VIVO do navegador."""
    if COOKIES_FILE:
        from yt_dlp.cookies import YoutubeDLCookieJar

        try:
            jar = YoutubeDLCookieJar(os.path.expanduser(COOKIES_FILE))
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            raise CookieError(cookie_help(f"não consegui abrir {COOKIES_FILE} ({e})")) from e
    else:
        try:
            jar = extract_cookies_from_browser(ENGINE, PROFILE)
        except Exception as e:
            raise CookieError(cookie_help(str(e)[:120])) from e
    if not any(c.name == "SAPISID" and "youtube" in c.domain for c in jar):
        raise CookieError(cookie_help("nenhum cookie de login (SAPISID) encontrado"))
    return jar


def find_js_runtime() -> tuple[str, str | None]:
    """Runtime JS que o yt-dlp exige p/ resolver os desafios do YouTube. Apps abertos pelo Hyprland/launcher
    NÃO herdam o PATH do shell (node vem do brew), então procura também nos diretórios comuns."""
    extra = {
        "node": ["/home/linuxbrew/.linuxbrew/bin", "~/.local/bin", "/usr/local/bin", r"C:\Program Files\nodejs"],
        "bun": ["~/.bun/bin", "/home/linuxbrew/.linuxbrew/bin"],
        "deno": ["~/.deno/bin", "/home/linuxbrew/.linuxbrew/bin"],
    }
    exe = ".exe" if IS_WIN else ""
    for name, dirs in extra.items():
        path = shutil.which(name)
        if not path:
            for d in dirs:
                cand = os.path.join(os.path.expanduser(d), name + exe)
                if os.access(cand, os.X_OK):
                    path = cand
                    break
        if path:
            return name, path
    return "node", None


# ----------------------------------------------------------------------------- helpers
def fmt_time(s) -> str:
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


# ----------------------------------------------------------------------------- YouTube Music API
class Api:
    """ytmusicapi autenticado com os cookies atuais do Zen."""

    def __init__(self):
        self.yt: YTMusic | None = None
        self.lock = threading.Lock()

    def connect(self):
        jar = load_cookie_jar()
        cookie = jar.get_cookie_header("https://music.youtube.com/")
        auth = {
            "cookie": cookie,
            "user-agent": UA,
            "x-goog-authuser": "0",
            "origin": "https://music.youtube.com",
            "authorization": "SAPISIDHASH 0_0",  # recalculado a cada requisição
            "accept": "*/*",
            "content-type": "application/json",
        }
        self.yt = YTMusic(json.dumps(auth))

    def call(self, name: str, *a, **k):
        def attempt():
            with self.lock:
                if self.yt is None:
                    self.connect()
                return getattr(self.yt, name)(*a, **k)

        def reconnect():
            with self.lock:
                self.connect()

        return run_with_retries(
            name, attempt, reconnect, lambda e: CookieError(cookie_help("a sessão do YouTube expirou")),
            passthrough=(CookieError,),
        )

    def logged_in(self) -> bool:
        """Relê os cookies do navegador e confirma que o YouTube Music aceita o login."""
        try:
            with self.lock:
                self.connect()
                self.yt.get_account_info()
            return True
        except Exception:
            log.exception("verificação de login falhou")
            return False

    def tracks(self, raw) -> list[Track]:
        return [t for t in (norm(x) for x in raw or []) if t]

    def search(self, q: str) -> list[Track]:
        rows = self.tracks(self.call("search", q, filter="songs"))
        return rows or self.tracks(self.call("search", q, filter="videos"))

    def home(self) -> list[Row]:
        rows: list[Row] = []
        for shelf in self.call("get_home", limit=6):
            for it in shelf.get("contents", []):
                if it.get("videoId"):
                    t = norm(it)
                    if t:
                        rows.append(t)
                elif it.get("playlistId") or it.get("browseId"):
                    rows.append({
                        "kind": "playlist" if it.get("playlistId") else "album",
                        "id": it.get("playlistId") or it.get("browseId"),
                        "title": it.get("title", "?"),
                        "artist": shelf.get("title", ""),
                    })
        return rows

    def liked(self) -> list[Track]:
        return self.tracks(self.call("get_liked_songs", limit=500)["tracks"])

    def history(self) -> list[Track]:
        return self.tracks(self.call("get_history"))

    def playlists(self) -> list[dict]:
        return [p for p in self.call("get_library_playlists", limit=100) if p.get("playlistId")]

    def playlist(self, pid: str) -> list[Track]:
        return self.tracks(self.call("get_playlist", pid, limit=500)["tracks"])

    def album(self, bid: str) -> list[Track]:
        data = self.call("get_album", bid)
        rows = self.tracks(data["tracks"])
        for r in rows:
            r["album"] = r["album"] or data.get("title", "")
            if not r["art"].startswith("http"):
                continue
        return rows

    def radio(self, vid: str) -> list[Track]:
        return self.tracks(self.call("get_watch_playlist", videoId=vid, radio=True, limit=50)["tracks"])

    def like(self, vid: str):
        return self.call("rate_song", vid, "LIKE")


# ----------------------------------------------------------------------------- resolver de stream
class Resolver:
    """Resolve vid -> URL direta de áudio (yt-dlp), com cache para pré-carregar a próxima faixa."""

    def __init__(self):
        _js = find_js_runtime()
        self.ydl = yt_dlp.YoutubeDL({
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            **({"cookiefile": os.path.expanduser(COOKIES_FILE)} if COOKIES_FILE else {"cookiesfrombrowser": (ENGINE, PROFILE, None, None)}),
            "js_runtimes": {_js[0]: ({"path": _js[1]} if _js[1] else {})},
            "remote_components": ["ejs:github"],
        })
        self.cache: dict[str, tuple[str, list[str], float]] = {}
        self.lock = threading.Lock()

    def resolve(self, vid: str):
        hit = self.cache.get(vid)
        if hit and hit[2] > time.time():
            return hit
        with self.lock:
            hit = self.cache.get(vid)
            if hit and hit[2] > time.time():
                return hit
            info = self.ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        headers = [f"{k}: {v}" for k, v in (info.get("http_headers") or {}).items() if k.lower() != "accept-encoding"]
        entry = (info["url"], headers, time.time() + 2 * 3600)
        self.cache[vid] = entry
        if len(self.cache) > 40:
            for k in sorted(self.cache, key=lambda x: self.cache[x][2])[:10]:
                self.cache.pop(k, None)
        return entry


# ----------------------------------------------------------------------------- MPRIS
class RootIface(ServiceInterface):
    def __init__(self, app):
        super().__init__("org.mpris.MediaPlayer2")
        self.app = app

    @method()
    def Raise(self):
        pass

    @method()
    def Quit(self):
        self.app.exit()

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "YTRadio"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class PlayerIface(ServiceInterface):
    def __init__(self, app):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.app = app

    @method()
    def Next(self):
        self.app.action_next()

    @method()
    def Previous(self):
        self.app.action_prev()

    @method()
    def Pause(self):
        self.app.set_pause(True)

    @method()
    def PlayPause(self):
        self.app.action_toggle_pause()

    @method()
    def Stop(self):
        self.app.set_pause(True)

    @method()
    def Play(self):
        self.app.set_pause(False)

    @method()
    def Seek(self, offset: "x"):
        self.app.seek_rel(offset / 1_000_000)

    @method()
    def SetPosition(self, track_id: "o", position: "x"):
        self.app.seek_abs(position / 1_000_000)

    @method()
    def OpenUri(self, uri: "s"):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self.app.mpris_status()

    @dbus_property()
    def LoopStatus(self) -> "s":
        return "Track" if self.app.loop else "None"

    @LoopStatus.setter
    def LoopStatus(self, v: "s"):
        self.app.set_loop(v == "Track")

    @dbus_property()
    def Rate(self) -> "d":
        return 1.0

    @Rate.setter
    def Rate(self, v: "d"):
        pass

    @dbus_property()
    def Shuffle(self) -> "b":
        return False

    @Shuffle.setter
    def Shuffle(self, v: "b"):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self.app.mpris_metadata()

    @dbus_property()
    def Volume(self) -> "d":
        return self.app.volume / 100

    @Volume.setter
    def Volume(self, v: "d"):
        self.app.set_volume(v * 100)

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return int(self.app.position() * 1_000_000)

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True


class Mpris:
    def __init__(self, app):
        self.app = app
        self.bus: MessageBus | None = None
        self.player = PlayerIface(app)
        self.root = RootIface(app)

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self.bus.export("/org/mpris/MediaPlayer2", self.root)
        self.bus.export("/org/mpris/MediaPlayer2", self.player)
        await self.bus.request_name("org.mpris.MediaPlayer2.ytradio")

    def changed(self, **props):
        if self.bus:
            try:
                self.player.emit_properties_changed(props)
            except Exception:
                log.debug("MPRIS: falha ao emitir propriedades", exc_info=True)


class NullMedia:
    """Sem integração de mídia (sistemas sem MPRIS/SMTC)."""

    def __init__(self, app):
        self.app = app

    async def start(self):
        pass

    def changed(self, **props):
        pass


class WinMedia:
    """Controles de mídia do Windows (SMTC): teclas de mídia e overlay de reprodução. EXPERIMENTAL (sem teste no Windows)."""

    def __init__(self, app):
        self.app = app
        self.smtc = None
        self.B = self.S = None

    async def start(self):
        if os.environ.get("YTRADIO_SMTC") == "0":
            return
        try:
            from winrt.windows.media import MediaPlaybackStatus, SystemMediaTransportControlsButton
            from winrt.windows.media.playback import MediaPlayer
        except Exception as e:  # pacotes winrt ausentes
            raise RuntimeError(f"controles de mídia do Windows indisponíveis ({e.__class__.__name__}): pip install winrt-Windows.Media.Playback") from e
        self.B, self.S = SystemMediaTransportControlsButton, MediaPlaybackStatus
        self.player = MediaPlayer()
        self.player.command_manager.is_enabled = False  # os botões chegam a nós, o MediaPlayer não toca nada
        s = self.smtc = self.player.system_media_transport_controls
        s.is_enabled = True
        s.is_play_enabled = s.is_pause_enabled = s.is_next_enabled = s.is_previous_enabled = True
        s.add_button_pressed(self._pressed)
        self.changed()

    def _pressed(self, sender, args):  # chamado numa thread do Windows
        B, app = self.B, self.app
        action = {B.PLAY: lambda: app.set_pause(False), B.PAUSE: lambda: app.set_pause(True),
                  B.NEXT: app.action_next, B.PREVIOUS: app.action_prev}.get(args.button)
        if action and app.ui_loop:
            app.ui_loop.call_soon_threadsafe(action)

    def changed(self, **props):
        if self.smtc is None or ("PlaybackStatus" not in props and "Metadata" not in props):
            return
        try:
            c, S = self.app.current, self.S
            st = {"Playing": S.PLAYING, "Paused": S.PAUSED}.get(self.app.mpris_status(), S.STOPPED)
            self.smtc.playback_status = st
            if c:
                from winrt.windows.foundation import Uri
                from winrt.windows.media import MediaPlaybackType
                from winrt.windows.storage.streams import RandomAccessStreamReference

                du = self.smtc.display_updater
                du.type = MediaPlaybackType.MUSIC
                du.music_properties.title = c["title"]
                du.music_properties.artist = c["artist"]
                du.music_properties.album_title = c["album"] or ""
                du.thumbnail = RandomAccessStreamReference.create_from_uri(Uri(c["art"]))
                du.update()
        except Exception:
            log.debug("SMTC: falha ao atualizar a mídia", exc_info=True)


# ----------------------------------------------------------------------------- visual
DEFAULT_RGB = (182, 156, 255)
COVER_COLS, COVER_ROWS = 28, 14  # tamanho da capa em células (modo blocos); o modo imagem usa a mesma caixa
def default_icons() -> str:
    return "plain" if IS_WIN else "nerd"  # o Windows Terminal padrão não traz Nerd Font


NERD = (opt("icons", "YTRADIO_ICONS") or default_icons()) != "plain"  # "plain" p/ fontes sem Nerd Font
IC = {
    "search": "" if NERD else "◎",
    "queue": "" if NERD else "≡",
    "home": "" if NERD else "⌂",
    "heart": "" if NERD else "♥",
    "history": "" if NERD else "◷",
    "music": "" if NERD else "♪",
    "loop": "" if NERD else "⟳",
    "prev": "" if NERD else "|<",
    "next": "" if NERD else ">|",
    "play": "" if NERD else "▶",
    "pause": "" if NERD else "‖",
    "shuffle": "" if NERD else "~",
    "like": "" if NERD else "♥",
}
EQ_FRAMES = ["▂▅▃", "▃▇▅", "▅▃▇", "▇▅▂", "▅▂▃", "▃▅▇"]
INK = "#0e0f16"  # texto escuro sobre fundo de destaque


def hexc(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, c))) for c in rgb)


def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lerp(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def hls_rgb(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return (r * 255, g * 255, b * 255)


# ---- temas
def make_palette(rgb) -> dict:
    """Deriva a paleta (destaque, claro, apagado, fundo) a partir de uma cor."""
    h, l, s = colorsys.rgb_to_hls(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    s = max(0.45, min(0.9, s))
    return {
        "ansi": False,
        "art": hexc(hls_rgb(h, 0.64, s)),
        "art-hi": hexc(hls_rgb(h, 0.80, s * 0.9)),
        "art-dim": hexc(hls_rgb(h, 0.38, s * 0.65)),
        "art-bg": hexc(hls_rgb(h, 0.22, s * 0.6)),
    }


SCHEME_FILE = os.path.expanduser(os.environ.get("YTRADIO_SCHEME") or "~/.local/state/caelestia/scheme.json")


def read_scheme() -> dict | None:
    """Esquema de cores atual do Caelestia (gerado pelo matugen a partir do wallpaper)."""
    try:
        with open(SCHEME_FILE) as f:
            d = json.load(f)
        return d if d.get("colours", {}).get("primary") else None
    except Exception:
        return None


def caelestia_palette() -> dict | None:
    """Mesmas cores do shell: primary = destaque, containers = bordas/seleção, onPrimary = texto sobre o destaque."""
    d = read_scheme()
    if not d:
        return None
    c, light = d["colours"], d.get("mode") == "light"
    g = lambda k, dflt=None: hex_to_rgb(c[k]) if k in c else dflt  # noqa: E731
    primary, surface = g("primary"), g("surface", (22, 19, 17))
    container = g("primaryContainer", primary)
    hi = g("primaryFixed") or lerp(primary, (0, 0, 0) if light else (255, 255, 255), 0.3 if light else 0.35)
    return {
        "ansi": False,
        "art": hexc(primary),
        "art-hi": hexc(hi),
        "art-dim": hexc(lerp(container, surface, 0.45)),
        "art-bg": hexc(lerp(surface, container, 0.38)),
        "ink": hexc(g("onPrimary", (14, 15, 22))),
    }


def ansi_palette(color: str) -> dict:
    """Usa as cores ANSI do terminal: acompanha a paleta do kitty/Caelestia automaticamente."""
    return {"ansi": True, "art": f"bright_{color}", "art-hi": f"bright_{color}", "art-dim": color, "art-bg": "bright_black"}


PRESETS = {
    "violeta": ("Violeta", (182, 156, 255)),
    "sakura": ("Sakura", (245, 169, 198)),
    "oceano": ("Oceano", (102, 204, 255)),
    "esmeralda": ("Esmeralda", (110, 231, 160)),
    "ambar": ("Âmbar", (255, 196, 92)),
    "brasa": ("Brasa", (255, 122, 89)),
    "nord": ("Nord", (136, 192, 208)),
    "dracula": ("Drácula", (189, 147, 249)),
    "gruvbox": ("Gruvbox", (250, 189, 47)),
    "catppuccin": ("Catppuccin", (203, 166, 247)),
    "tokyo": ("Tokyo Night", (122, 162, 247)),
    "mono": ("Monocromático", (200, 200, 200)),
}
ANSI_NAMES = {"magenta": "magenta", "blue": "azul", "cyan": "ciano", "green": "verde", "yellow": "amarelo", "red": "vermelho"}
ANSI_SWATCH = {"magenta": "bright_magenta", "blue": "bright_blue", "cyan": "bright_cyan",
               "green": "bright_green", "yellow": "bright_yellow", "red": "bright_red"}


def theme_items(album_rgb=None) -> list[tuple[str, str, str]]:
    """(id, nome, cor da amostra) de todos os temas, na ordem do seletor."""
    items = [("album", "Álbum — segue a música tocando", hexc(album_rgb or DEFAULT_RGB))]
    cp = caelestia_palette()
    if cp:
        items.append(("caelestia", "Caelestia — espelha o esquema do shell (matugen)", cp["art"]))
    items += [(f"ansi:{c}", f"Terminal · {n} (paleta do terminal)", ANSI_SWATCH[c]) for c, n in ANSI_NAMES.items()]
    items += [(f"preset:{k}", name, hexc(rgb)) for k, (name, rgb) in PRESETS.items()]
    return items


def theme_palette(theme_id: str, album_rgb=None) -> dict:
    if theme_id == "caelestia":
        cp = caelestia_palette()
        if cp:
            return cp
    if theme_id.startswith("ansi:") and theme_id[5:] in ANSI_NAMES:
        return ansi_palette(theme_id[5:])
    if theme_id.startswith("preset:") and theme_id[7:] in PRESETS:
        return make_palette(PRESETS[theme_id[7:]][1])
    return make_palette(album_rgb or DEFAULT_RGB)  # "album" (e padrão enquanto não há capa)


def css_vars(pal: dict) -> dict:
    pre = "ansi_" if pal.get("ansi") else ""
    return {k: pre + pal[k] for k in ("art", "art-hi", "art-dim", "art-bg")}


def ink(pal: dict) -> str:
    return "black" if pal.get("ansi") else pal.get("ink", INK)


def gradient_text(txt: str, c1, c2, pal: dict, bold=True) -> Text:
    out = Text()
    if pal.get("ansi"):
        out.append(txt, style=Style(color=pal["art-hi"], bold=bold))
        return out
    n = max(1, len(txt) - 1)
    for i, ch in enumerate(txt):
        out.append(ch, style=Style(color=hexc(lerp(c1, c2, i / n)), bold=bold))
    return out


# ---- capa
def dominant_color(img):
    """Cor mais 'viva' da capa: agrupa por matiz e pondera por saturação."""
    small = img.resize((40, 40)).convert("RGB")
    buckets: dict[int, list] = {}
    for r, g, b in small.getdata():
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if l < 0.14 or l > 0.92 or s < 0.22:
            continue
        w = s * (1 - abs(l - 0.5))
        acc = buckets.setdefault(int(h * 18) % 18, [0.0, 0.0, 0.0, 0.0])
        acc[0] += r * w
        acc[1] += g * w
        acc[2] += b * w
        acc[3] += w
    if not buckets:
        return DEFAULT_RGB
    acc = max(buckets.values(), key=lambda a: a[3])
    return (acc[0] / acc[3], acc[1] / acc[3], acc[2] / acc[3])


QUAD = {1: "▘", 2: "▝", 3: "▀", 4: "▖", 5: "▌", 6: "▞", 7: "▛"}  # bits: 1=TL 2=TR 4=BL (BR fica no fundo)


def _mean(cs):
    n = len(cs)
    return tuple(sum(c[i] for c in cs) / n for i in range(3))


def _err(cs, m):
    return sum((c[0] - m[0]) ** 2 + (c[1] - m[1]) ** 2 + (c[2] - m[2]) ** 2 for c in cs)


def blockimg(img, cols: int, rows: int) -> Text:
    """Imagem em blocos de quadrante: 2x2 pixels por célula, com as 2 cores que melhor aproximam cada bloco."""
    px = img.convert("RGB").resize((cols * 2, rows * 2), Image.LANCZOS).load()
    out = Text(no_wrap=True)
    for y in range(rows):
        for x in range(cols):
            p = [px[2 * x, 2 * y], px[2 * x + 1, 2 * y], px[2 * x, 2 * y + 1], px[2 * x + 1, 2 * y + 1]]
            best = None
            for mask in range(8):
                fg = [p[i] for i in range(4) if mask >> i & 1]
                bg = [p[i] for i in range(4) if not mask >> i & 1]
                fm, bm = (_mean(fg) if fg else None), _mean(bg)
                e = (_err(fg, fm) if fg else 0) + _err(bg, bm)
                if best is None or e < best[0]:
                    best = (e, mask, fm, bm)
            _, mask, fm, bm = best
            out.append(QUAD.get(mask, " "), style=Style(color=hexc(fm) if fm else None, bgcolor=hexc(bm)))
        if y < rows - 1:
            out.append("\n")
    return out


def cover_from_image(img) -> dict:
    """Imagem (modo gráfico), blocos (fallback) e cor dominante (tema 'álbum') a partir de uma capa."""
    img = img.convert("RGB")
    w, h = img.size
    if w != h:  # miniaturas de vídeo (16:9 / 4:3): recorta o quadrado central
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    return {
        "img": img.resize((480, 480), Image.LANCZOS),
        "text": blockimg(img, COVER_COLS, COVER_ROWS),
        "rgb": dominant_color(img),
    }


def fetch_cover(url: str) -> dict:
    """Baixa a capa e prepara os formatos de exibição."""
    r = requests.get(url, timeout=10, headers={"User-Agent": UA})
    r.raise_for_status()
    return cover_from_image(Image.open(io.BytesIO(r.content)))


def placeholder_cover(pal: dict) -> Text:
    """Quadrado com uma nota musical, usado enquanto a capa não existe/carrega."""
    out = Text(no_wrap=True)
    if pal.get("ansi"):
        for y in range(COVER_ROWS):
            line = "░" * COVER_COLS
            if y == COVER_ROWS // 2 - 1:
                line = line[: COVER_COLS // 2 - 1] + "♫" + line[COVER_COLS // 2:]
            out.append(line, style=Style(color=pal["art-dim"]))
            if y < COVER_ROWS - 1:
                out.append("\n")
        return out
    top, bot = hex_to_rgb(pal["art-bg"]), hex_to_rgb(pal["art-dim"])
    for y in range(COVER_ROWS):
        for x in range(COVER_COLS):
            a = lerp(top, bot, (y * 2) / (COVER_ROWS * 2) + x / (COVER_COLS * 6))
            b = lerp(top, bot, (y * 2 + 1) / (COVER_ROWS * 2) + x / (COVER_COLS * 6))
            if (y, x) == (COVER_ROWS // 2 - 1, COVER_COLS // 2 - 1):
                out.append("♫", style=Style(color=pal["art-hi"], bgcolor=hexc(lerp(a, b, 0.5)), bold=True))
            else:
                out.append("▀", style=Style(color=hexc(a), bgcolor=hexc(b)))
        if y < COVER_ROWS - 1:
            out.append("\n")
    return out


CSS = """
Screen { layout: vertical; background: transparent; }
#main { height: 1fr; }
#side { width: 32; padding: 0 1; }
#logobox { height: 4; }
#logo { height: 4; padding: 1 1 0 1; }
#nav { border: none; background: transparent; padding: 0; height: 1fr; scrollbar-size-vertical: 1; }
#nav > .option-list--option-highlighted { background: $art-bg; text-style: bold; }
#nav:focus > .option-list--option-highlighted { background: $art-dim; }
#nav > .option-list--option-hover { background: $art-bg; }
#coverbox { width: 30; height: 16; border: round $art-dim; padding: 0; background: transparent; }
#cover_txt { width: 100%; height: 100%; }
#cover_img { width: 100%; height: 100%; }
#splitter { width: 1; height: 100%; }
#content { width: 1fr; padding: 0 1 0 0; }
#q { border: round $art-dim; background: transparent; height: 3; }
#q:focus { border: round $art; background: transparent; }
#viewtitle { height: 1; margin: 1 0 0 0; padding: 0 1; }
#tbl { height: 1fr; border: none; background: transparent; padding: 0 1 0 0; scrollbar-size-vertical: 1; }
#tbl > .datatable--header { background: transparent; color: $art; text-style: bold; }
#tbl > .datatable--cursor { background: $art-bg; text-style: bold; }
#tbl > .datatable--hover { background: $art-bg 60%; }
#tbl:focus > .datatable--cursor { background: $art-dim; text-style: bold; }
#card { height: 8; border: round $art-dim; padding: 0 1; background: transparent; }
#meta { height: 3; padding: 0 1; }
#seekrow { height: 1; padding: 0 1; }
#tcur, #tdur { width: 6; height: 1; }
#tcur { content-align: right middle; }
#seek { width: 1fr; height: 1; margin: 0 1; }
#ctrlrow { height: 1; margin-top: 1; padding: 0 1; }
#buttons, #chips, #vollabel, #volpct { width: auto; height: 1; }
#chips { margin-left: 2; }
#vollabel { margin-left: 3; }
#vol { width: 18; height: 1; margin: 0 1; }
#hints { height: 1; content-align: center middle; }
"""

NAV = [
    Option(f"{IC['search']}  Buscar", id="search"),
    Option(f"{IC['queue']}  Fila / rádio", id="queue"),
    Option(f"{IC['home']}  Início", id="home"),
    Option(f"{IC['heart']}  Curtidas", id="liked"),
    Option(f"{IC['history']}  Histórico", id="history"),
    Option(Text("\n PLAYLISTS", style="dim bold"), id="_hdr", disabled=True),
]

HINTS = [("space", "pausa", "toggle_pause"), ("n", "próxima", "next"), ("p", "anterior", "prev"),
         ("l", "loop", "toggle_loop"), ("a", "rádio", "toggle_autoplay"), ("r", "rádio daqui", "radio_here"),
         ("y", "curtir", "like"), ("s", "embaralhar", "shuffle"), ("t", "tema", "pick_theme"),
         ("c", "capa", "cycle_cover"), ("/", "buscar", "focus_search"), ("q", "sair", "quit")]

SORT_KEYS = {
    "t": lambda r: (r.get("title") or "").lower(),
    "a": lambda r: (r.get("artist") or "").lower(),
    "b": lambda r: (r.get("album") or "").lower(),
    "d": lambda r: r.get("dur") or 0,
}

COVER_MODES = [("auto", "automático (imagem real no kitty, blocos nos demais)"),
               ("image", "imagem real (protocolo gráfico do terminal)"),
               ("blocks", "blocos de texto (funciona em qualquer terminal)"),
               ("off", "sem capa")]


class Segs:
    """Monta um Text e registra as regiões clicáveis (início, fim, ação)."""

    def __init__(self):
        self.text = Text()
        self.segs: list[tuple[int, int, str]] = []

    def add(self, s: str, style, action: str | None = None):
        x0 = self.text.cell_len
        self.text.append(s, style=style)
        if action:
            self.segs.append((x0, self.text.cell_len, action))

    def add_text(self, t: Text, action: str | None = None):
        x0 = self.text.cell_len
        self.text.append_text(t)
        if action:
            self.segs.append((x0, self.text.cell_len, action))


class ClickText(Static):
    """Static cujos trechos disparam ações do app ao clicar."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.segs: list[tuple[int, int, str]] = []

    def set(self, sg: Segs):
        self.segs = sg.segs
        self.update(sg.text)

    async def on_click(self, ev: events.Click):
        for x0, x1, action in self.segs:
            if x0 <= ev.x < x1:
                ev.stop()
                await self.app.run_action(f"app.{action}")
                return


class Slider(Widget):
    """Barra arrastável (busca da faixa / volume): clique, arraste e roda do mouse."""

    def __init__(self, kind: str, **kw):
        super().__init__(**kw)
        self.kind = kind
        self.value = 0.0
        self.preview: float | None = None
        self.dragging = False

    def render(self) -> Text:
        pal = self.app.palette
        w = max(2, self.size.width)
        v = self.preview if self.preview is not None else self.value
        head = int(round((w - 1) * max(0.0, min(1.0, v))))
        out = Text(no_wrap=True)
        if pal.get("ansi"):
            out.append("━" * head, style=Style(color=pal["art-hi"], bold=True))
        else:
            c0, c1 = hex_to_rgb(pal["art-dim"]), hex_to_rgb(pal["art-hi"])
            for i in range(head):
                out.append("━", style=Style(color=hexc(lerp(c0, c1, i / max(1, w - 1))), bold=True))
        out.append("●", style=Style(color=pal["art-hi"], bold=True))
        out.append("─" * (w - head - 1), style="dim")
        return out

    def frac(self, x: float) -> float:
        return max(0.0, min(1.0, x / max(1, self.size.width - 1)))

    def on_mouse_down(self, ev: events.MouseDown):
        if ev.button != 1:
            return
        ev.stop()
        self.capture_mouse()
        self.dragging = True
        self.app.slider_event(self, self.frac(ev.x), False)

    def on_mouse_move(self, ev: events.MouseMove):
        if self.dragging:
            ev.stop()
            self.app.slider_event(self, self.frac(ev.x), False)

    def on_mouse_up(self, ev: events.MouseUp):
        if self.dragging:
            ev.stop()
            self.dragging = False
            self.release_mouse()
            self.app.slider_event(self, self.frac(ev.x), True)

    def on_mouse_scroll_up(self, ev):
        ev.stop()
        self.app.slider_wheel(self, 1)

    def on_mouse_scroll_down(self, ev):
        ev.stop()
        self.app.slider_wheel(self, -1)


class ClickBox(Vertical):
    """Contêiner que executa uma ação ao ser clicado (capa = pausa, logo = início)."""

    def __init__(self, action: str, **kw):
        super().__init__(**kw)
        self._action = action

    async def on_click(self, ev: events.Click):
        ev.stop()
        await self.app.run_action(self._action)


class Splitter(Widget):
    """Divisória arrastável entre a barra lateral e o conteúdo."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.dragging = False
        self.hover = False

    def render(self) -> Text:
        pal = self.app.palette
        on = self.dragging or self.hover
        ch = "┃" if on else "│"
        return Text("\n".join(ch for _ in range(max(1, self.size.height))), style=pal["art"] if on else pal["art-dim"])

    def on_enter(self, ev):
        self.hover = True
        self.refresh()

    def on_leave(self, ev):
        self.hover = False
        self.refresh()

    def on_mouse_down(self, ev: events.MouseDown):
        if ev.button == 1:
            ev.stop()
            self.capture_mouse()
            self.dragging = True
            self.refresh()

    def on_mouse_move(self, ev: events.MouseMove):
        if self.dragging:
            ev.stop()
            self.app.set_side_width(ev.screen_x, final=False)

    def on_mouse_up(self, ev: events.MouseUp):
        if self.dragging:
            ev.stop()
            self.dragging = False
            self.release_mouse()
            self.refresh()
            self.app.set_side_width(ev.screen_x, final=True)


class TrackTable(DataTable):
    """Tabela de faixas: repassa botão direito (rádio) e arrastar-e-soltar (reordenar a fila) ao app."""

    def on_mouse_down(self, ev: events.MouseDown):
        self.app.table_mouse_down(ev)

    def on_mouse_up(self, ev: events.MouseUp):
        self.app.table_mouse_up(ev)


class InfoScreen(ModalScreen):
    """Aviso com texto longo (ex.: como configurar o login do YouTube)."""

    DEFAULT_CSS = """
    InfoScreen { align: center middle; background: transparent; }
    InfoScreen #ibox { width: 78; height: auto; max-height: 90%; border: round $art; background: $art-bg; padding: 1 2; }
    InfoScreen #ititle { text-style: bold; color: $art-hi; margin-bottom: 1; }
    InfoScreen #ihint { color: $art-hi 70%; margin-top: 1; }
    """
    BINDINGS = [("escape", "dismiss(None)", "Fechar"), ("enter", "dismiss(None)", "Fechar")]

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_text, self.body = title, text

    def compose(self) -> ComposeResult:
        with Vertical(id="ibox"):
            yield Static(self.title_text, id="ititle")
            yield Static(self.body, markup=False)
            yield Static("Enter ou Esc para fechar", id="ihint")


class LoginScreen(InfoScreen):
    """Sessão do YouTube ausente/expirada: o navegador já foi aberto e o app espera o login."""

    def compose(self) -> ComposeResult:
        with Vertical(id="ibox"):
            yield Static(self.title_text, id="ititle")
            yield Static(self.body, markup=False)
            yield Static("Aguardando o login… (o ytradio continua sozinho)  ·  Enter: abrir o navegador de novo  ·  Esc: fechar", id="ihint")

    BINDINGS = [("escape", "dismiss(None)", "Fechar"), ("enter", "dismiss('reopen')", "Reabrir")]


class ThemePicker(ModalScreen):
    """Seletor de tema: álbum, paleta do terminal (ANSI) ou presets."""

    DEFAULT_CSS = """
    ThemePicker { align: center middle; background: transparent; }
    ThemePicker #pbox { width: 58; height: auto; max-height: 90%; border: round $art; background: $art-bg; padding: 0 1; }
    ThemePicker #ptitle { height: 1; padding: 0 1; text-style: bold; color: $art-hi; }
    ThemePicker #phint { height: 1; padding: 0 1; color: $art-hi 70%; }
    ThemePicker OptionList { height: auto; max-height: 22; background: transparent; border: none; padding: 0; }
    ThemePicker OptionList:focus { border: none; }
    ThemePicker OptionList > .option-list--option-highlighted { background: $art-dim; text-style: bold; }
    ThemePicker OptionList:focus > .option-list--option-highlighted { background: $art-dim; text-style: bold; }
    """
    BINDINGS = [("escape", "dismiss(None)", "Fechar"), ("t", "dismiss(None)", "Fechar")]

    def __init__(self, items, current: str):
        super().__init__()
        self.items, self.current = items, current

    def compose(self) -> ComposeResult:
        opts = []
        for tid, name, sw in self.items:
            line = Text()
            line.append("██ ", style=sw)
            line.append(name)
            if tid == self.current:
                line.append("  ●", style="bold")
            opts.append(Option(line, id=tid))
        with Vertical(id="pbox"):
            yield Static("Tema da interface", id="ptitle")
            yield Static("Enter aplica · Esc fecha", id="phint")
            yield OptionList(*opts)

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = next((i for i, it in enumerate(self.items) if it[0] == self.current), 0)
        ol.focus()

    @on(OptionList.OptionSelected)
    def picked(self, ev: OptionList.OptionSelected):
        self.dismiss(ev.option.id)


class YTRadio(App):
    CSS = CSS
    TITLE = "ytradio"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("space", "toggle_pause", "Play/Pausa"),
        Binding("n", "next", "Próxima"),
        Binding("p", "prev", "Anterior"),
        Binding("l", "toggle_loop", "Loop"),
        Binding("a", "toggle_autoplay", "Rádio auto"),
        Binding("r", "radio_here", "Rádio daqui"),
        Binding("s", "shuffle", "Embaralhar"),
        Binding("y", "like", "Curtir"),
        Binding("t", "pick_theme", "Tema"),
        Binding("c", "cycle_cover", "Capa"),
        Binding("slash", "focus_search", "Buscar"),
        Binding("bracketleft", "seek_back", "-10s", show=False),
        Binding("bracketright", "seek_fwd", "+10s", show=False),
        Binding("minus", "vol_down", "Vol-", show=False),
        Binding("plus,equals_sign", "vol_up", "Vol+", show=False),
        Binding("q", "quit", "Sair"),
    ]

    def __init__(self):
        st = load_state(STATE_FILE)
        self.theme_id: str = st.get("theme", "album")
        self.cover_mode: str = st.get("cover", "auto")
        self.album_rgb = None
        self.palette = theme_palette(self.theme_id)
        super().__init__(ansi_color=True)
        self.api = Api()
        self.resolver = Resolver()
        self.mpris = Mpris(self) if IS_LINUX else WinMedia(self) if IS_WIN else NullMedia(self)
        self.volume: float = st.get("volume", 80)
        self.loop: bool = st.get("loop", False)
        self.autoplay: bool = st.get("autoplay", True)
        self.rq = RadioQueue()
        self.current: Track | None = None
        self.rows: list[Row] = []
        self.view = "search"
        self.view_title = ""
        self.errors = 0
        self.loading = False
        self.message = ""
        self.player: mpv.MPV | None = None
        self.ui_loop: asyncio.AbstractEventLoop | None = None
        self._last_status = ""
        self._last_meta_id = ""
        self._was_playing: dict[int, bool] = {}
        self.cover_data: dict | None = None
        self.covers: dict[str, dict] = {}
        self.pl_titles: dict[str, str] = {}
        self.show_album = True
        self.eq = 0
        self._scheme_mtime = None
        self.sort: tuple[str, bool] | None = None
        self._view_key = None
        self.rows_src: list[Row] = []
        self._drag_row: int | None = None
        self._premute = 0.0
        self.side_w: int = int(st.get("side_w", 32))

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.update(css_vars(getattr(self, "palette", None) or theme_palette("album")))
        return v

    # ---- layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="side"):
                with ClickBox("app.go_home", id="logobox"):
                    yield Static(id="logo")
                yield OptionList(*NAV, id="nav")
                with ClickBox("app.toggle_pause", id="coverbox"):
                    yield Static(id="cover_txt")
                    if GfxImage is not None:
                        yield GfxImage(id="cover_img")
            yield Splitter(id="splitter")
            with Vertical(id="content"):
                yield Input(placeholder=f"{IC['search']}  Buscar músicas, artistas…", id="q")
                yield Static(id="viewtitle")
                yield TrackTable(id="tbl", cursor_type="row", zebra_stripes=False)
                with Vertical(id="card"):
                    yield Static(id="meta")
                    with Horizontal(id="seekrow"):
                        yield Static(id="tcur")
                        yield Slider("seek", id="seek")
                        yield Static(id="tdur")
                    with Horizontal(id="ctrlrow"):
                        yield ClickText(id="buttons")
                        yield ClickText(id="chips")
                        yield ClickText(id="vollabel")
                        yield Slider("vol", id="vol")
                        yield Static(id="volpct")
        yield ClickText(id="hints")

    def on_mount(self):
        self.ui_loop = asyncio.get_running_loop()
        self.query_one("#tbl", DataTable).focus()
        self.player = mpv.MPV(video=False, ytdl=False, input_default_bindings=False, osc=False,
                              idle=True, loglevel="error", volume=self.volume)

        @self.player.event_callback("end-file")
        def _(ev):
            reason = ev.as_dict().get("reason", b"")
            if reason in (b"eof", b"error") and self.ui_loop:
                self.ui_loop.call_soon_threadsafe(self.on_track_end, reason == b"error")

        self.set_interval(0.5, self.tick)
        self.set_interval(0.2, self.anim)
        self.run_worker(self.start_mpris(), exclusive=False)
        self.run_worker(self.load_sidebar(), exclusive=False)
        self.run_worker(self.show_home(), exclusive=True, group="view")
        self.apply_layout()
        self.render_all()

    async def start_mpris(self):
        try:
            await self.mpris.start()
        except Exception as e:
            self.flash(f"MPRIS indisponível: {e}")

    def persist(self):
        save_state(STATE_FILE, {"volume": self.volume, "loop": self.loop, "autoplay": self.autoplay,
                    "theme": self.theme_id, "cover": self.cover_mode, "side_w": self.side_w})

    def on_unmount(self):
        self.persist()
        if self.player:
            try:
                self.player.terminate()
            except Exception:
                pass

    def on_resize(self, ev=None):
        self.apply_layout()
        album = self.size.width >= 118
        if album != self.show_album and self.rows:
            self.show_album = album
            self.refill()
        self.render_all()

    def compact(self) -> bool:
        return self.size.height < 32

    def apply_layout(self):
        compact = self.compact()
        self.query_one("#card").styles.height = 5 if compact else 8
        self.query_one("#meta").styles.height = 1 if compact else 3
        self.query_one("#ctrlrow").styles.margin = (0, 0, 0, 0) if compact else (1, 0, 0, 0)
        self.query_one("#side").styles.width = self.side_w
        self.show_album = self.size.width >= 118
        self.update_cover()

    def set_side_width(self, x: int, final: bool):
        self.side_w = max(32, min(56, int(x)))
        self.query_one("#side").styles.width = self.side_w
        if final:
            self.persist()
            self.refill()
            self.render_all()

    # ---- capa (imagem real, blocos ou nada)
    def cover_effective(self) -> str:
        if self.cover_mode == "off":
            return "off"
        if self.cover_mode == "blocks":
            return "blocks"
        if self.cover_mode == "image":
            return "image" if GfxImage is not None else "blocks"
        return "image" if (GRAPHICS and GfxImage is not None) else "blocks"

    def update_cover(self):
        mode = self.cover_effective()
        box = self.query_one("#coverbox")
        box.display = mode != "off" and not self.compact() and self.size.width >= 90
        txt = self.query_one("#cover_txt", Static)
        img = self.query_one("#cover_img") if GfxImage is not None else None
        data = self.cover_data if self.current else None
        use_img = mode == "image" and data is not None and img is not None
        if img is not None:
            img.display = use_img
            if use_img and img.image is not data["img"]:
                img.image = data["img"]
        txt.display = not use_img
        if not use_img:
            txt.update(data["text"] if data is not None and mode != "off" else placeholder_cover(self.palette))

    # ---- helpers de UI
    def report_error(self, e: Exception, prefix: str):
        if isinstance(e, CookieError):  # abre o navegador para relogar e espera o login (uma vez só)
            if not getattr(self, "_cookie_info_shown", False):
                self._cookie_info_shown = True
                opened = open_login_browser()
                self.push_screen(LoginScreen("Login do YouTube necessário", f"{e}\n\nAbri o music.youtube.com ({opened}). "
                                             "Entre na sua conta lá; assim que o login for detectado o ytradio recarrega sozinho."),
                                 lambda r: open_login_browser() if r == "reopen" else None)
                self.run_worker(self.wait_for_login(), exclusive=False)
            return
        self.flash(f"{prefix}: {str(e)[:120]}")

    async def wait_for_login(self, timeout: float = 600, every: float = 5):
        """Espera (até 10 min) o login aparecer nos cookies do navegador; então recarrega a biblioteca."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(every)
            if await self.io(self.api.logged_in):
                if isinstance(self.screen, LoginScreen):
                    self.pop_screen()
                self.flash("Login detectado!")
                self._cookie_info_shown = False
                self.run_worker(self.load_sidebar(), exclusive=False)
                return
        self._cookie_info_shown = False  # desistiu: o próximo erro de login reabre o fluxo

    def flash(self, msg: str):
        self.message = msg
        self.render_title()
        self.set_timer(5, self._clear_flash)

    def _clear_flash(self):
        self.message = ""
        self.render_title()

    async def io(self, fn, *a, **k):
        return await asyncio.to_thread(fn, *a, **k)

    async def load_sidebar(self):
        try:
            pls = await self.io(self.api.playlists)
        except Exception as e:
            self.report_error(e, "Playlists")
            return
        nav = self.query_one("#nav", OptionList)
        for p in pls:
            self.pl_titles[p["playlistId"]] = p["title"]
            nav.add_option(Option(f"{IC['music']}  {p['title']}", id=f"pl:{p['playlistId']}"))

    # ---- temas
    def apply_palette(self, pal: dict):
        self.palette = pal
        try:
            self.refresh_css()
        except Exception:
            pass
        t = self.query_one("#tbl", DataTable)
        row = t.cursor_row
        if self.rows:  # reaplica as cores explícitas das células
            self.refill()
            if row is not None and self.rows:
                t.move_cursor(row=min(row, len(self.rows) - 1), animate=False)
        self.update_cover()
        self.render_all()

    def set_theme(self, theme_id: str | None):
        if not theme_id:
            return
        self.theme_id = theme_id
        self.persist()
        self.apply_palette(theme_palette(theme_id, self.album_rgb))
        name = next((n for i, n, _ in theme_items() if i == theme_id), theme_id)
        self.flash(f"Tema: {name.split(' — ')[0].split(' (')[0]}")

    def action_pick_theme(self):
        self.push_screen(ThemePicker(theme_items(self.album_rgb), self.theme_id), self.set_theme)

    def action_cycle_cover(self):
        ids = [m for m, _ in COVER_MODES]
        self.cover_mode = ids[(ids.index(self.cover_mode) + 1) % len(ids)] if self.cover_mode in ids else "auto"
        self.persist()
        self.update_cover()
        self.render_all()
        self.flash("Capa: " + dict(COVER_MODES)[self.cover_mode])

    # ---- tabela
    def row_cells(self, r: Row, playing: bool) -> list:
        hi = self.palette["art-hi"]
        if r["kind"] != "track":
            mark = Text("☰" if r["kind"] == "playlist" else "◉", style="dim")
            return [mark, Text(r["title"], no_wrap=True, overflow="ellipsis"),
                    Text(r["artist"], style="dim", no_wrap=True, overflow="ellipsis"),
                    Text("playlist" if r["kind"] == "playlist" else "álbum", style="dim"), Text("")]
        mark = Text(self.eq_frame(), style=hi) if playing else Text("")
        title = Text(r["title"], style=f"bold {hi}" if playing else "", no_wrap=True, overflow="ellipsis")
        return [mark, title,
                Text(r["artist"], style="dim", no_wrap=True, overflow="ellipsis"),
                Text(r["album"], style="dim", no_wrap=True, overflow="ellipsis"),
                Text(fmt_time(r["dur"]) if r["dur"] else "", style="dim", justify="right")]

    def eq_frame(self) -> str:
        return " ‖ " if self.is_paused() else EQ_FRAMES[self.eq % len(EQ_FRAMES)]

    def sorted_rows(self, rows: list[Row]) -> list[Row]:
        if not self.sort or self.view == "queue":
            return list(rows)
        k, rev = self.sort
        return sorted(rows, key=SORT_KEYS[k], reverse=rev)

    def refill(self):
        """Redesenha a tabela com as linhas atuais (mantém a ordenação escolhida)."""
        self.fill(self.rows_src, self.view_title, self.view)

    def fill(self, rows: list[Row], title: str, view: str):
        vk = (view, title)
        if vk != self._view_key:  # outra lista: zera a ordenação
            self._view_key, self.sort = vk, None
        self.rows_src, self.view, self.view_title = rows, view, title
        self.rows = self.sorted_rows(rows)
        self._was_playing = {}
        t = self.query_one("#tbl", DataTable)
        t.clear(columns=True)
        avail = max(50, t.size.width - 1)
        aw = max(16, min(30, int(avail * 0.22)))
        bw = max(18, min(34, int(avail * 0.26))) if self.show_album else 0
        ncols = 5 if self.show_album else 4
        tw = max(18, avail - 3 - 5 - aw - bw - 2 * ncols - 1)

        def head(name: str, key: str, **kw) -> Text:
            arrow = ""
            if self.sort and self.sort[0] == key:
                arrow = " ▼" if self.sort[1] else " ▲"
            return Text(name + arrow, **kw)

        t.add_column(Text(" "), key="m", width=3)
        t.add_column(head("TÍTULO", "t"), key="t", width=tw)
        t.add_column(head("ARTISTA", "a"), key="a", width=aw)
        if self.show_album:
            t.add_column(head("ÁLBUM", "b"), key="b", width=bw)
        t.add_column(head("DUR", "d", justify="right"), key="d", width=5)
        cur = self.current["id"] if self.current else None
        for i, r in enumerate(self.rows):
            cells = self.row_cells(r, r["kind"] == "track" and r["id"] == cur)
            if not self.show_album:
                cells.pop(3)
            t.add_row(*cells, key=str(i))
        self.render_title()

    @on(DataTable.HeaderSelected, "#tbl")
    def header_clicked(self, ev: DataTable.HeaderSelected):
        k = ev.column_key.value
        if k not in SORT_KEYS or self.view == "queue":
            return
        if self.sort is None or self.sort[0] != k:
            self.sort = (k, False)
        elif not self.sort[1]:
            self.sort = (k, True)
        else:
            self.sort = None
        self.refill()

    def mark_playing(self):
        """Atualiza a linha que toca (negrito + equalizador) sem refazer a tabela."""
        t = self.query_one("#tbl", DataTable)
        cur = self.current["id"] if self.current else None
        keys = ["m", "t", "a"] + (["b"] if self.show_album else []) + ["d"]
        for i, r in enumerate(self.rows):
            if r["kind"] != "track":
                continue
            playing = r["id"] == cur
            if not playing and not self._was_playing.get(i):
                continue
            cells = self.row_cells(r, playing)
            if not self.show_album:
                cells.pop(3)
            for k, c in zip(keys, cells):
                try:
                    t.update_cell(str(i), k, c)
                except Exception:
                    pass
            self._was_playing[i] = playing

    def anim(self):
        """Equalizador animado na linha que está tocando."""
        if not self.current or self.is_paused() or self.player is None:
            return
        self.eq += 1
        t = self.query_one("#tbl", DataTable)
        for i, r in enumerate(self.rows):
            if r["kind"] == "track" and r["id"] == self.current["id"]:
                try:
                    t.update_cell(str(i), "m", Text(self.eq_frame(), style=self.palette["art-hi"]))
                except Exception:
                    pass

    # ---- navegação
    @on(OptionList.OptionSelected, "#nav")
    def nav_selected(self, ev: OptionList.OptionSelected):
        oid = ev.option.id or ""
        if oid == "search":
            self.action_focus_search()
        elif oid == "queue":
            self.fill(list(self.queue), "Fila / rádio", "queue")
            self.query_one("#tbl").focus()
        elif oid == "home":
            self.run_worker(self.show_home(), exclusive=True, group="view")
        elif oid == "liked":
            self.run_worker(self.show_list("Curtidas", self.api.liked, "liked"), exclusive=True, group="view")
        elif oid == "history":
            self.run_worker(self.show_list("Histórico", self.api.history, "history"), exclusive=True, group="view")
        elif oid.startswith("pl:"):
            pid = oid[3:]
            self.run_worker(self.show_list(self.pl_titles.get(pid, "Playlist"), lambda: self.api.playlist(pid), "playlist"),
                            exclusive=True, group="view")

    async def show_list(self, title: str, fn, view: str):
        self.view_title = title
        self.loading = True
        self.render_title()
        try:
            rows = await self.io(fn)
        except Exception as e:
            self.loading = False
            self.report_error(e, f"Erro ao carregar {title}")
            return
        self.loading = False
        self.fill(rows, title, view)
        self.query_one("#tbl").focus()

    async def show_home(self):
        await self.show_list("Início", self.api.home, "home")

    def action_focus_search(self):
        self.query_one("#q", Input).focus()

    @on(Input.Submitted, "#q")
    def do_search(self, ev: Input.Submitted):
        q = ev.value.strip()
        if q:
            self.run_worker(self.show_list(f"Busca: {q}", lambda: self.api.search(q), "search"), exclusive=True, group="view")

    def on_key(self, ev):
        if ev.key == "escape" and self.focused and self.focused.id == "q":
            self.query_one("#tbl").focus()

    @on(DataTable.RowSelected, "#tbl")
    def row_selected(self, ev: DataTable.RowSelected):
        i = int(ev.row_key.value)
        r = self.rows[i]
        if r["kind"] == "track":
            self.start_from_view(i)
        elif r["kind"] == "playlist":
            self.run_worker(self.show_list(r["title"], lambda: self.api.playlist(r["id"]), "playlist"), exclusive=True, group="view")
        else:
            self.run_worker(self.show_list(r["title"], lambda: self.api.album(r["id"]), "playlist"), exclusive=True, group="view")

    # ---- fila e reprodução
    def start_from_view(self, i: int):
        if self.view == "queue":
            self.run_worker(self.play_at(i), exclusive=True, group="play")
            return
        tracks = [r for r in self.rows if r["kind"] == "track"]
        if self.view in ("playlist", "liked", "history"):
            idx = tracks.index(self.rows[i])
            self.rq.reset(tracks)
            self.run_worker(self.play_at(idx), exclusive=True, group="play")
        else:  # busca / início: começa um rádio a partir da faixa escolhida
            self.rq.reset([self.rows[i]])
            self.run_worker(self.play_at(0), exclusive=True, group="play")
            self.extend_radio()

    @property
    def queue(self) -> list[Track]:
        return self.rq.tracks

    @property
    def qi(self) -> int:
        return self.rq.qi

    @qi.setter
    def qi(self, v: int):
        self.rq.qi = v

    def extend_radio(self):
        if not self.rq.refilling:
            self.run_worker(self._extend_radio(), exclusive=False, group="radio", exit_on_error=False)

    async def _extend_radio(self) -> bool:
        gen = self.rq.generation
        try:
            added = await self.rq.refill(lambda seed: self.io(self.api.radio, seed))
        except Exception as e:
            log.exception("falha ao abastecer o rádio")
            if gen == self.rq.generation:
                self.flash(f"Rádio: {e}")
            return False
        if added:
            if self.view == "queue":
                self.fill(list(self.queue), "Fila / rádio", "queue")
            self.prefetch_next()
        return True

    def maybe_refill(self):
        if self.autoplay and len(self.queue) - self.qi - 1 <= 4:
            self.extend_radio()

    def prefetch_next(self):
        if 0 <= self.qi + 1 < len(self.queue):
            vid = self.queue[self.qi + 1]["id"]
            self.run_worker(self.io(self.resolver.resolve, vid), exclusive=False, group="prefetch", exit_on_error=False)

    def on_track_changed(self):
        """Nova faixa: marca na tabela e busca a capa (que pode definir a cor da interface)."""
        self.mark_playing()
        if self.current:
            self.run_worker(self.load_cover(self.current), exclusive=True, group="cover", exit_on_error=False)
        self.render_all()

    async def load_cover(self, track: Track):
        url = track["art"]
        data = self.covers.get(url)
        if data is None:
            try:
                data = await self.io(fetch_cover, url)
            except Exception:
                log.warning("falha ao baixar a capa", exc_info=True)
                self.cover_data = None
                self.update_cover()
                return
            self.covers[url] = data
            if len(self.covers) > 30:
                self.covers.pop(next(iter(self.covers)))
        if not self.current or self.current["id"] != track["id"]:
            return
        self.cover_data = data
        self.album_rgb = data["rgb"]
        if self.theme_id == "album":
            self.apply_palette(make_palette(data["rgb"]))
        else:
            self.update_cover()
            self.render_all()

    async def play_at(self, i: int):
        if not (0 <= i < len(self.queue)):
            return
        self.qi, self.current = i, self.queue[i]
        self.loading = True
        self.on_track_changed()
        try:
            url, headers, _ = await self.io(self.resolver.resolve, self.current["id"])
        except Exception as e:
            self.errors += 1
            self.loading = False
            self.flash(f"Falha ao carregar “{self.current['title']}”: {str(e)[:80]}")
            if self.errors < 5:
                self.run_worker(self.go_next(), exclusive=True, group="play")
            return
        self.errors = 0
        self.loading = False
        self.player.http_header_fields = headers
        self.player.play(url)
        self.player.pause = False
        self._last_meta_id = ""
        self.push_mpris()
        self.render_all()
        self.maybe_refill()
        self.prefetch_next()

    async def go_next(self):
        if self.qi + 1 >= len(self.queue) and self.autoplay:
            try:
                await asyncio.wait_for(self._extend_radio(), 15)  # aguarda a busca em andamento (ou inicia uma)
            except asyncio.TimeoutError:
                self.flash("Rádio: tempo esgotado")
        if self.qi + 1 < len(self.queue):
            await self.play_at(self.qi + 1)
        else:
            self.flash("Fim da fila")

    def on_track_end(self, error: bool):
        if error:
            self.errors += 1
            if self.errors >= 5:
                return
        if self.loop and not error:
            self.run_worker(self.play_at(self.qi), exclusive=True, group="play")
        else:
            self.run_worker(self.go_next(), exclusive=True, group="play")

    # ---- ações
    def action_next(self):
        self.run_worker(self.go_next(), exclusive=True, group="play")

    def action_prev(self):
        if self.position() > 5 or self.qi <= 0:
            self.seek_abs(0)
        else:
            self.run_worker(self.play_at(self.qi - 1), exclusive=True, group="play")

    def set_pause(self, v: bool):
        if self.player and self.current:
            self.player.pause = v
            self.push_mpris()
            self.render_all()

    def action_toggle_pause(self):
        if self.player:
            self.set_pause(not self.player.pause)

    def set_loop(self, v: bool):
        self.loop = v
        self.mpris.changed(LoopStatus="Track" if v else "None")
        self.render_card()

    def action_toggle_loop(self):
        self.set_loop(not self.loop)

    def action_toggle_autoplay(self):
        self.autoplay = not self.autoplay
        if self.autoplay:
            self.maybe_refill()
        self.render_card()

    def action_radio_here(self):
        t = self.query_one("#tbl", DataTable)
        if t.cursor_row is None or not self.rows:
            return
        self.radio_from(self.rows[t.cursor_row])

    def radio_from(self, r: Row):
        if r["kind"] != "track":
            return
        self.rq.reset([r])
        self.autoplay = True
        self.run_worker(self.play_at(0), exclusive=True, group="play")
        self.extend_radio()
        self.flash(f"Rádio a partir de “{r['title']}”")

    # ---- mouse: botão direito, arrastar para reordenar, barras, divisória
    def table_mouse_down(self, ev: events.MouseDown):
        row = ev.style.meta.get("row", -1)
        self._drag_row = None
        if row is None or row < 0 or row >= len(self.rows):
            return
        if ev.button == 3:  # botão direito = rádio a partir desta faixa
            ev.stop()
            self.radio_from(self.rows[row])
        elif ev.button == 1 and self.view == "queue":
            self._drag_row = row

    def table_mouse_up(self, ev: events.MouseUp):
        src, self._drag_row = self._drag_row, None
        row = ev.style.meta.get("row", -1)
        if src is not None and self.view == "queue" and row is not None and 0 <= row < len(self.rows) and row != src:
            self.move_queue_item(src, row)

    def move_queue_item(self, src: int, dst: int):
        """Arrastar uma faixa para outra posição da fila."""
        if not (0 <= src < len(self.queue) and 0 <= dst < len(self.queue)):
            return
        item = self.queue.pop(src)
        self.queue.insert(dst, item)
        if self.qi == src:
            self.qi = dst
        elif src < self.qi <= dst:
            self.qi -= 1
        elif dst <= self.qi < src:
            self.qi += 1
        self.fill(list(self.queue), "Fila / rádio", "queue")
        self.query_one("#tbl", DataTable).move_cursor(row=dst, animate=False)
        self.prefetch_next()
        self.flash(f"Movida para a posição {dst + 1}")

    def slider_event(self, s: Slider, frac: float, final: bool):
        if s.kind == "seek":
            dur = self.duration()
            if not self.current or not dur:
                s.preview = None
                return
            if final:
                s.preview = None
                s.value = frac
                self.seek_abs(frac * dur)
            else:
                s.preview = frac
        else:
            self.set_volume(frac * 100)
        self.render_card()

    def slider_wheel(self, s: Slider, d: int):
        if s.kind == "seek":
            self.seek_rel(5 * d)
        else:
            self.set_volume(self.volume + 5 * d)

    def action_go_home(self):
        self.run_worker(self.show_home(), exclusive=True, group="view")

    def action_toggle_mute(self):
        if self.volume > 0:
            self._premute = self.volume
            self.set_volume(0)
        else:
            self.set_volume(self._premute or 80)

    def action_shuffle(self):
        if self.qi < 0:
            return
        rest = self.queue[self.qi + 1:]
        random.shuffle(rest)
        self.queue[self.qi + 1:] = rest
        self.flash("Fila embaralhada")
        self.prefetch_next()

    def action_like(self):
        if not self.current:
            return
        vid, title = self.current["id"], self.current["title"]

        async def go():
            try:
                await self.io(self.api.like, vid)
                self.flash(f"♥ Curtida: {title}")
            except Exception as e:
                self.flash(f"Não consegui curtir: {e}")

        self.run_worker(go(), exclusive=False)

    def seek_rel(self, secs: float):
        if self.player and self.current:
            try:
                self.player.seek(secs, "relative")
            except Exception:
                pass

    def seek_abs(self, secs: float):
        if self.player and self.current:
            try:
                self.player.seek(secs, "absolute")
            except Exception:
                pass

    def action_seek_back(self):
        self.seek_rel(-10)

    def action_seek_fwd(self):
        self.seek_rel(10)

    def set_volume(self, v: float):
        self.volume = max(0, min(130, v))
        if self.player:
            self.player.volume = self.volume
        self.mpris.changed(Volume=self.volume / 100)
        self.render_card()

    def action_vol_down(self):
        self.set_volume(self.volume - 5)

    def action_vol_up(self):
        self.set_volume(self.volume + 5)

    # ---- estado para MPRIS e barra
    def position(self) -> float:
        try:
            return float(self.player.time_pos or 0) if self.player else 0.0
        except Exception:
            return 0.0

    def duration(self) -> float:
        try:
            d = float(self.player.duration or 0) if self.player else 0.0
        except Exception:
            d = 0.0
        return d or (self.current["dur"] if self.current else 0)

    def is_paused(self) -> bool:
        try:
            return bool(self.player.pause) if self.player else True
        except Exception:
            return True

    def mpris_status(self) -> str:
        if not self.current:
            return "Stopped"
        return "Paused" if self.is_paused() else "Playing"

    def mpris_metadata(self) -> dict:
        c = self.current
        if not c:
            return {"mpris:trackid": Variant("o", "/org/mpris/MediaPlayer2/TrackList/NoTrack")}
        return {
            "mpris:trackid": Variant("o", "/org/ytradio/track/" + re.sub(r"\W", "_", c["id"])),
            "mpris:length": Variant("x", int(self.duration() * 1_000_000)),
            "mpris:artUrl": Variant("s", c["art"]),
            "xesam:title": Variant("s", c["title"]),
            "xesam:artist": Variant("as", [a.strip() for a in c["artist"].split(",") if a.strip()] or [""]),
            "xesam:album": Variant("s", c["album"] or c["title"]),
        }

    def push_mpris(self):
        self.mpris.changed(PlaybackStatus=self.mpris_status(), Metadata=self.mpris_metadata())

    def tick(self):
        if not self.player:
            return
        status = self.mpris_status()
        meta_id = (self.current["id"] if self.current else "") + str(int(self.duration()))
        if status != self._last_status or meta_id != self._last_meta_id:
            self._last_status, self._last_meta_id = status, meta_id
            self.push_mpris()
        self.render_card()
        self.check_scheme()

    def check_scheme(self):
        """No tema 'caelestia', reaplica as cores quando o matugen gera um esquema novo (troca de wallpaper)."""
        if self.theme_id != "caelestia":
            return
        try:
            m = os.path.getmtime(SCHEME_FILE)
        except OSError:
            return
        if m != self._scheme_mtime:
            first = self._scheme_mtime is None
            self._scheme_mtime = m
            if not first:
                pal = caelestia_palette()
                if pal and pal != self.palette:
                    self.apply_palette(pal)

    # ---- renderização
    def render_all(self):
        self.query_one("#splitter").refresh()
        self.render_logo()
        self.render_title()
        self.render_card()
        self.render_hints()

    def render_logo(self):
        pal = self.palette
        c_hi = hex_to_rgb(pal["art-hi"]) if not pal.get("ansi") else None
        c_ac = hex_to_rgb(pal["art"]) if not pal.get("ansi") else None
        logo = gradient_text("♫  ytradio", c_hi, c_ac, pal)
        logo.append("\nyoutube music", style="dim")
        self.query_one("#logo", Static).update(logo)

    def render_title(self):
        w = max(30, self.query_one("#viewtitle", Static).size.width or (self.size.width - 36))
        left = Text()
        left.append(self.view_title or "Início", style=f"bold {self.palette['art-hi']}")
        if self.loading and not self.rows:
            left.append("  carregando…", style="dim")
        elif self.rows:
            n = sum(1 for r in self.rows if r["kind"] == "track")
            left.append(f"   {n} faixas" if n else f"   {len(self.rows)} itens", style="dim")
        if self.message:
            right = Text(f" {self.message} ", style=Style(color=ink(self.palette), bgcolor=self.palette["art"], bold=True))
            pad = max(1, w - left.cell_len - right.cell_len - 1)
            left.append(" " * pad)
            left.append_text(right)
        self.query_one("#viewtitle", Static).update(left)

    def render_hints(self):
        sg = Segs()
        items, total = [], 0
        for i, (k, label, action) in enumerate(HINTS):
            need = len(k) + 1 + len(label) + (3 if i else 0)
            if total + need > self.size.width - 2:
                break
            items.append((k, label, action, i))
            total += need
        sg.add(" " * max(0, (self.size.width - total) // 2), "")
        for k, label, action, i in items:
            if i:
                sg.add("   ", "")
            sg.add(k, Style(color=self.palette["art"], bold=True), action)
            sg.add(f" {label}", Style(dim=True), action)
        self.query_one("#hints", ClickText).set(sg)

    def chip(self, label: str, on: bool) -> Text:
        pal = self.palette
        st = Style(color=ink(pal), bgcolor=pal["art"], bold=True) if on else Style(color=pal["art-dim"], bold=True)
        return Text(f" {label} ", style=st)

    def buttons_segs(self) -> Segs:
        pal = self.palette
        playing = bool(self.current) and not self.is_paused()
        sg = Segs()
        sg.add(f" {IC['prev']} ", Style(color=pal["art-hi"], bold=True), "prev")
        sg.add(" ", "")
        sg.add(f" {IC['pause'] if playing else IC['play']} ", Style(color=ink(pal), bgcolor=pal["art"], bold=True), "toggle_pause")
        sg.add(" ", "")
        sg.add(f" {IC['next']} ", Style(color=pal["art-hi"], bold=True), "next")
        sg.add("  ", "")
        sg.add(f" {IC['like']} ", Style(color=pal["art-hi"]), "like")
        sg.add(" ", "")
        sg.add(f" {IC['shuffle']} ", Style(color=pal["art-hi"]), "shuffle")
        return sg

    def render_card(self):
        pal = self.palette
        c = self.current
        self.query_one("#card").border_title = Text(" tocando agora ", style=pal["art"]) if c else ""
        meta = self.query_one("#meta", Static)
        pos, dur = self.position(), self.duration()
        paused = self.is_paused()
        if not c:
            pos = dur = 0
            m = Text()
            m.append("♫  ytradio\n", style=f"bold {pal['art-hi']}")
            m.append("Escolha uma música (Enter ou clique duplo), busque com ", style="dim")
            m.append("/", style=f"bold {pal['art']}")
            m.append(" ou comece um rádio com ", style="dim")
            m.append("r", style=f"bold {pal['art']}")
            m.append(" (ou botão direito).\nPassou da música? Aperte ", style="dim")
            m.append("n", style=f"bold {pal['art']}")
            m.append(" e ele busca outra parecida.", style="dim")
        else:
            state = "carregando…" if self.loading else ("pausado" if paused else "tocando")
            m = Text()
            if self.compact():
                m.append(("‖ " if paused else "▶ "), style=f"bold {pal['art']}")
                m.append(c["title"], style=Style(bold=True, color=pal["art-hi"]))
                m.append("  —  " + c["artist"], style="bold")
            else:
                m.append(f"{'‖' if paused else '▶'}  {state.upper()}\n", style=f"bold {pal['art-dim']}" if paused else f"bold {pal['art']}")
                m.append(c["title"] + "\n", style=Style(bold=True, color=pal["art-hi"]))
                sub = c["artist"] + (f"  ·  {c['album']}" if c["album"] and c["album"] != c["title"] else "")
                m.append(sub, style="dim")
        meta.update(m)
        seek, vol = self.query_one("#seek", Slider), self.query_one("#vol", Slider)
        if not seek.dragging:
            seek.value = min(1.0, pos / dur) if dur else 0.0
        shown = seek.preview * dur if (seek.preview is not None and dur) else pos
        self.query_one("#tcur", Static).update(Text(fmt_time(shown), style="dim"))
        self.query_one("#tdur", Static).update(Text(fmt_time(dur), style="dim"))
        vol.value = min(100, self.volume) / 100
        seek.refresh()
        vol.refresh()
        self.query_one("#volpct", Static).update(Text(f"{int(self.volume)}%", style="dim"))
        vl = Segs()
        vl.add("vol", Style(dim=True), "toggle_mute")
        self.query_one("#vollabel", ClickText).set(vl)
        self.query_one("#buttons", ClickText).set(self.buttons_segs())
        cs = Segs()
        cs.add_text(self.chip(f"{IC['loop']} loop", self.loop), "toggle_loop")
        cs.add(" ", "")
        cs.add_text(self.chip("∞ rádio", self.autoplay), "toggle_autoplay")
        self.query_one("#chips", ClickText).set(cs)

def main():
    os.makedirs(config_dir(), exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(config_dir(), "ytradio.log"), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    YTRadio().run()


if __name__ == "__main__":
    main()
