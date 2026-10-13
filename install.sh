#!/usr/bin/env bash
# Instala o ytradio (Linux/macOS): cria um venv local, instala as dependências e o comando ~/.local/bin/ytradio
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 12), "precisa de Python 3.12 ou superior"'
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/ytradio" <<LAUNCHER
#!/usr/bin/env bash
exec "$DIR/.venv/bin/python" "$DIR/ytradio.py" "\$@"
LAUNCHER
chmod +x "$HOME/.local/bin/ytradio"
echo "ytradio instalado em ~/.local/bin/ytradio"
"$DIR/.venv/bin/python" -c "import mpv" 2>/dev/null || echo "AVISO: libmpv não encontrado — instale (Fedora: mpv-libs · Debian/Ubuntu: libmpv2 · Arch: mpv)."
command -v node >/dev/null || command -v bun >/dev/null || command -v deno >/dev/null || echo "AVISO: instale node, bun ou deno (o yt-dlp precisa de um runtime JavaScript)."
echo "Entre no music.youtube.com no Firefox/Zen e rode: ytradio"
