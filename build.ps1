$ErrorActionPreference = 'Stop'

python -m pip install --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install the build dependencies.' }
python -m PyInstaller --noconfirm --clean --windowed --onefile --name "SNOW Reminder" --distpath "build\release" --hidden-import win32timezone reminder.py
if ($LASTEXITCODE -ne 0) { throw 'Could not build the standalone app.' }

$compiler = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $compiler) {
    $candidates = @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe',
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    $compiler = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $compiler) {
    throw 'Inno Setup 6 is required to build the installer. Install it from https://jrsoftware.org/isinfo.php and run this script again.'
}

& $compiler 'installer.iss'
if ($LASTEXITCODE -ne 0) { throw 'Could not compile the Windows installer.' }
Write-Host "Installer complete: $PWD\dist\SNOW Reminder Setup.exe"
