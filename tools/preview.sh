#!/usr/bin/env bash
# Captura do ytradio num kitty REAL, sem aparecer na sua tela (Hyprland + kitty + grim).
# uso: preview.sh saida.png [TEMA] [CAPA] [PICKER]   (YTRADIO_PY = python com as dependências)
# Abre o kitty numa workspace escondida (9, "silent"), captura a janela com `grim -T` e fecha.
# `sync_to_monitor=no` é necessário: numa workspace escondida o kitty não recebe frame callbacks.
OUT=${1:?uso: preview.sh saida.png [TEMA] [CAPA] [PICKER]}
DIR=$(dirname "$(readlink -f "$0")"); PY=${YTRADIO_PY:-python3}
CMD="env YTR_THEME='$2' YTR_COVER='$3' YTR_PICKER='$4' $PY $DIR/live_preview.py"
hyprctl dispatch "hl.dsp.exec_cmd(\"[workspace 9 silent; float; size 1400 860] kitty --class ytr-preview -o background_opacity=1 -o sync_to_monitor=no $CMD\")" >/dev/null
for i in $(seq 1 40); do
  sleep 1
  read -r SID PID < <(hyprctl clients -j | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    if c['class']=='ytr-preview': print(c['stableId'], c['pid'])")
  [ -n "$SID" ] && [ "$i" -ge 18 ] && break   # a consulta ao terminal atrasa a subida do app
done
grim -T "$SID" "$OUT" && echo "ok $OUT"
kill "$PID" 2>/dev/null
