"""Testes da camada multiplataforma (simula Windows no Linux). uso: python -I platform_test.py"""
import asyncio, json, os, sqlite3, subprocess, sys, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("YTRADIO_PROFILE", None); os.environ.pop("YTRADIO_COOKIES", None)
import ytradio as y

R = []
def check(name, ok, extra=""):
    R.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))

tmp = tempfile.mkdtemp()
# --- 1. pastas no estilo Windows
y.IS_WIN = True
os.environ["APPDATA"] = os.path.join(tmp, "AppData", "Roaming")
check("config_dir no Windows = %APPDATA%\\ytradio", y.config_dir() == os.path.join(tmp, "AppData", "Roaming", "ytradio"), y.config_dir())
bases = y.firefox_bases()
check("perfis Zen e Firefox do Windows na lista", any("zen" in b for b in bases) and any("Firefox" in b for b in bases), str([os.path.basename(os.path.dirname(b)) for b in bases]))

# --- 2. escolha do perfil: o que tem login do YouTube vence o mais recente
def mkprofile(base, name, login):
    d = os.path.join(base, name); os.makedirs(d)
    db = sqlite3.connect(os.path.join(d, "cookies.sqlite"))
    db.execute("create table moz_cookies (name text, value text, host text, expiry integer)")
    db.execute("insert into moz_cookies values ('SAPISID' if 0 else 'x','1','example.com',0)") if False else None
    if login: db.execute("insert into moz_cookies values ('SAPISID','abc','.youtube.com',0)")
    else: db.execute("insert into moz_cookies values ('NID','abc','.google.com',0)")
    db.commit(); db.close(); return d
zen = bases[0]
logged = mkprofile(zen, "aaaa.Default (release)", True)
os.utime(os.path.join(logged, "cookies.sqlite"), (1, 1))                  # antigo
anon = mkprofile(zen, "bbbb.Default Profile", False)                     # mais recente, SEM login
check("perfil com login vence o mais recente sem login", y.find_profile() == logged, os.path.basename(y.find_profile() or ""))
os.environ["YTRADIO_PROFILE"] = anon
check("YTRADIO_PROFILE força o perfil", y.find_profile() == anon); os.environ.pop("YTRADIO_PROFILE")

# --- 3. cookies.txt (exportado do Chrome) vira login para a API
cj = os.path.join(tmp, "cookies.txt")
open(cj, "w").write("# Netscape HTTP Cookie File\n" + "".join(
    f".youtube.com\tTRUE\t/\tTRUE\t2000000000\t{n}\tv_{n}\n" for n in ("SAPISID", "__Secure-3PAPISID", "LOGIN_INFO", "SID")))
y.COOKIES_FILE = cj
jar = y.load_cookie_jar()
hdr = jar.get_cookie_header("https://music.youtube.com/")
check("cookies.txt: cabeçalho para music.youtube.com tem o login", "SAPISID=v_SAPISID" in hdr and "__Secure-3PAPISID" in hdr, hdr[:60])
api = y.Api(); api.connect()
check("cookies.txt: cliente do ytmusicapi é criado", api.yt is not None)
open(cj, "w").write("# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t2000000000\tNID\tx\n")
try: y.load_cookie_jar(); check("cookies.txt sem login levanta CookieError", False)
except y.CookieError as e: check("cookies.txt sem login levanta CookieError", "SAPISID" in str(e))
y.COOKIES_FILE = None

# --- 4. Chrome no Windows: erro explicativo
y.ENGINE = "chrome"; y.PROFILE = None
orig = y.extract_cookies_from_browser
class EmptyJar(list): pass
y.extract_cookies_from_browser = lambda *a, **k: EmptyJar()      # o yt-dlp devolve vazio quando não decifra (v20)
try: y.load_cookie_jar(); check("Chrome sem cookies legíveis: erro orientando", False)
except y.CookieError as e:
    m = str(e); check("Chrome: mensagem cita Firefox e cookies.txt", "Firefox" in m and "YTRADIO_COOKIES" in m and "v127" in m)
y.extract_cookies_from_browser = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("failed to decrypt with DPAPI"))
try: y.load_cookie_jar(); check("Chrome: exceção do yt-dlp vira CookieError", False)
except y.CookieError as e: check("Chrome: exceção do yt-dlp vira CookieError", "DPAPI" in str(e))
y.extract_cookies_from_browser = orig; y.ENGINE = "firefox"

# --- 5. runtime JS no Windows (node.exe / bun.exe fora do PATH)
home = os.path.join(tmp, "home"); os.makedirs(os.path.join(home, ".bun", "bin"))
exe = os.path.join(home, ".bun", "bin", "bun.exe"); open(exe, "w").write(""); os.chmod(exe, 0o755)
old_home, old_which = os.environ.get("HOME"), y.shutil.which
os.environ["HOME"] = home; y.shutil.which = lambda n: None
name, path = y.find_js_runtime()
check("sem node: usa bun.exe de ~/.bun/bin", name == "bun" and path == exe, f"{name} {path and os.path.basename(path)}")
y.shutil.which = old_which; os.environ["HOME"] = old_home; y.IS_WIN = False

# --- 6. backends de mídia
w = y.WinMedia(None)
try: asyncio.run(w.start()); check("WinMedia sem winrt: erro claro (não quebra)", False)
except RuntimeError as e: check("WinMedia sem winrt: erro claro (não quebra)", "winrt" in str(e))
check("NullMedia é no-op", asyncio.run(y.NullMedia(None).start()) is None)

# --- 7. ícones: padrão por sistema (e override por config)
y.IS_WIN = True; check("Windows: ícones padrão = plain (sem Nerd Font)", y.default_icons() == "plain")
y.IS_WIN = False; check("Linux: ícones padrão = nerd", y.default_icons() == "nerd")

# --- 8. janela explicativa quando falta o login
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shot_data as D
y.STATE_FILE = os.path.join(tmp, "st.json")
y.mpv.MPV = type("F", (), {"__init__": lambda s, **k: None, "event_callback": lambda s, *a: (lambda f: f), "terminate": lambda s: None,
                           "pause": False, "time_pos": 0, "duration": 0, "volume": 80})
async def _noop(self): pass
y.Mpris.start = _noop
def boom(self): raise y.CookieError("Não consegui ler o login do YouTube (chrome): teste")
y.Api.playlists = boom; y.Api.home = boom
async def ui():
    app = y.YTRadio()
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(1.5)
        top = type(app.screen).__name__
        await pilot.press("escape"); await pilot.pause(0.3)
        return top, type(app.screen).__name__
top, after = asyncio.run(ui())
check("falha de login abre a janela de ajuda (uma só)", top == "InfoScreen", top)
check("Esc fecha a janela de ajuda", after != "InfoScreen", after)

print(f"\n{sum(R)}/{len(R)} testes passaram"); sys.exit(0 if all(R) else 1)
