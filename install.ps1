# Instala o ytradio no Windows: cria um venv local e instala as dependências.
# Rode no PowerShell, dentro da pasta do projeto:  powershell -ExecutionPolicy Bypass -File .\install.ps1
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
python -c "import sys; assert sys.version_info >= (3, 12), 'precisa de Python 3.12 ou superior'"
python -m venv "$dir\.venv"
& "$dir\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$dir\.venv\Scripts\python.exe" -m pip install --quiet -r "$dir\requirements.txt"
Write-Host ""
Write-Host "ytradio instalado. Para abrir: $dir\ytradio.cmd"
Write-Host ""
Write-Host "FALTA (se ainda não tiver):"
Write-Host " 1. libmpv: baixe mpv-2.dll (ou libmpv-2.dll) em https://sourceforge.net/projects/mpv-player-windows/files/libmpv/"
Write-Host "    e coloque na pasta do ytradio ou em $env:APPDATA\ytradio\mpv"
Write-Host " 2. Um runtime JavaScript:  winget install OpenJS.NodeJS   (ou bun / deno)"
Write-Host " 3. Login: Firefox ou Zen com a conta logada em music.youtube.com (o Chrome não serve no Windows — veja o README)"
