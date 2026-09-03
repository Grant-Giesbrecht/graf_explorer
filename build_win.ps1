#requires -Version 5.1
<#
  build_windows.ps1
  -----------------
  Build "GrAF Explorer.exe" on Windows with PyInstaller, embedding the app icon,
  and (optionally) register the .graf document type + its icon for the current
  user so .graf files show document.ico in Explorer.

  Run from the project root (same folder as build_macos.sh):

      .\build_windows.ps1                       # onedir build + register .graf for current user
      .\build_windows.ps1 -OneFile              # single-file .exe instead of a folder
      .\build_windows.ps1 -NoRegister           # just build, don't touch the registry
      .\build_windows.ps1 -InstallDir "$env:LOCALAPPDATA\Programs\GrAF Explorer"

  Prereqs (in the SAME Python environment where `python -m graf_explorer` runs):
      pip install pyinstaller pillow

  Icons
    Windows needs .ico files. This script builds them automatically, preferring a
    same-named .png source (best quality) and falling back to the .icns:
        icons\app\graf_app.png   (or graf_app.icns)   -> icons\app\graf_app.ico
        icons\file\document.png  (or document.icns)   -> icons\file\document.ico
    Delete the generated .ico to force a rebuild from source.

  Notes
    * PyInstaller does not cross-compile: this must run on Windows.
    * The file association is written to HKCU (no admin needed). It points at an
      absolute path, so if you move the app afterwards, re-run with -Register
      (or -InstallDir) so the paths stay valid.
#>

[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$NoRegister,
    [string]$InstallDir
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$AppName = 'GrAF Explorer'
$ProgId  = 'GrafExplorer.Document'
$Entry   = 'src\graf_explorer\__main__.py'
$AppIco  = 'src\graf_explorer\icons\app\graf_app.ico'
$DocIco  = 'src\graf_explorer\icons\file\document.ico'

function Resolve-IconSource {
    # Prefer a .png (cleanest), then .icns, sharing the target's basename.
    param([string]$IcoPath)
    $dir  = Split-Path -Parent $IcoPath
    $base = [System.IO.Path]::GetFileNameWithoutExtension($IcoPath)
    foreach ($ext in '.png', '.icns') {
        $cand = Join-Path $dir ($base + $ext)
        if (Test-Path $cand) { return $cand }
    }
    throw "No source icon found for $IcoPath (looked for $base.png / $base.icns in $dir)"
}

function Convert-ToIco {
    # Pillow reads PNG and ICNS the same way; ICO images are capped at 256x256.
    param([string]$Src, [string]$Dst)
    $py = @'
from PIL import Image
import sys
img = Image.open(sys.argv[1]).convert("RGBA")
img.save(sys.argv[2], format="ICO",
         sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
'@
    $tmp = Join-Path $env:TEMP 'png2ico.py'
    $py | Set-Content -Encoding UTF8 $tmp
    & python $tmp $Src $Dst
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Dst)) {
        throw "Failed to convert $Src -> $Dst (is Pillow installed? pip install pillow)"
    }
    Write-Host "  $Src -> $Dst"
}

# --- 0. sanity checks -----------------------------------------------------------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python not found on PATH. Activate the env where the app runs first."
}
if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    throw "pyinstaller not found. Run: pip install pyinstaller"
}
if (-not (Test-Path $Entry)) { throw "Cannot find $Entry. Run this from the project root." }

# --- 1. make sure we have .ico files (Windows can't use .icns/.png directly) ----
Write-Host "==> Preparing icons"
if (-not (Test-Path $AppIco)) { Convert-ToIco -Src (Resolve-IconSource $AppIco) -Dst $AppIco }
if (-not (Test-Path $DocIco)) { Convert-ToIco -Src (Resolve-IconSource $DocIco) -Dst $DocIco }

# --- 2. clean previous output ---------------------------------------------------
Write-Host "==> Cleaning old build/dist"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "build\$AppName"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "dist\$AppName"
Remove-Item -Force -ErrorAction SilentlyContinue "dist\$AppName.exe"

# --- 3. build -------------------------------------------------------------------
# graf and stardust are editable installs that PyInstaller's static analysis can
# fail to locate, which silently drops *their* dependencies (e.g. cryptography,
# pulled in only by stardust.io) from the bundle -- the frozen app then dies on
# launch with a bare ModuleNotFoundError. build_support.py resolves the dirs that
# have to go on the search path; see it for the full explanation.
Write-Host "==> Resolving editable-install paths"
$PkgPaths = @(& python build_support.py)
if ($LASTEXITCODE -ne 0) { throw "build_support.py failed (exit $LASTEXITCODE)." }
$PkgPaths = $PkgPaths | Where-Object { $_ -and $_.Trim() }
foreach ($p in $PkgPaths) { Write-Host "  $p" }

Write-Host "==> Running PyInstaller"
$piArgs = @(
    '--noconfirm', '--clean', '--windowed',
    '--name', $AppName,
    '--icon', $AppIco,
    # Match the macOS spec's dependency collection so graf's bundled assets
    # (graf/assets/portable_fonts.json, fonts) and stardust submodules ship too.
    '--collect-all', 'graf',
    '--collect-all', 'stardust',
    '--collect-submodules', 'pylogfile',
    '--collect-submodules', 'colorama',
    '--hidden-import', 'PyQt5.sip',
    # Reached only through graf/stardust, so absent from this app's own import
    # graph. --paths below should surface them anyway; naming them explicitly
    # means a future editable-install quirk degrades into a bigger bundle
    # rather than a crash. Keep in sync via build_support.EXTRA_HIDDEN_IMPORTS.
    '--hidden-import', 'cryptography',
    '--hidden-import', 'h5py',
    # The app uses PyQt5, but graf.widgets imports PyQt6 and --collect-all graf
    # sweeps it in. PyInstaller refuses to bundle two Qt bindings, so exclude the
    # unused bindings and the one graf module that pulls them (never imported here).
    '--exclude-module', 'PyQt6',
    '--exclude-module', 'PySide6',
    '--exclude-module', 'PySide2',
    '--exclude-module', 'graf.widgets',
    # Ship the icons folder so the in-app custom_icon_qss() can find sprites
    # (e.g. icons\tab_close.png) if you add them later. (; is the Windows sep.)
    '--add-data', 'src\graf_explorer\icons;icons'
)
foreach ($p in $PkgPaths) { $piArgs += @('--paths', $p) }
if ($OneFile) { $piArgs += '--onefile' }
$piArgs += $Entry

& pyinstaller @piArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)." }

# --- 4. resolve build output, copy the document icon next to the exe -----------
if ($OneFile) {
    $AppRoot = (Resolve-Path 'dist').Path
    $ExePath = Join-Path $AppRoot "$AppName.exe"
} else {
    $AppRoot = (Resolve-Path "dist\$AppName").Path
    $ExePath = Join-Path $AppRoot "$AppName.exe"
}
Copy-Item -Force $DocIco (Join-Path $AppRoot 'document.ico')

# --- 5. optional: install to a stable location ---------------------------------
if ($InstallDir) {
    Write-Host "==> Installing to $InstallDir"
    if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null }
    if ($OneFile) {
        Copy-Item -Force $ExePath (Join-Path $InstallDir "$AppName.exe")
        Copy-Item -Force $DocIco  (Join-Path $InstallDir 'document.ico')
    } else {
        robocopy $AppRoot $InstallDir /MIR /NJH /NJS /NFL /NDL | Out-Null
    }
    $AppRoot = (Resolve-Path $InstallDir).Path
    $ExePath = Join-Path $AppRoot "$AppName.exe"
}

$DocIconPath = Join-Path $AppRoot 'document.ico'

Write-Host ""
Write-Host "Built:   $ExePath"
Write-Host "DocIcon: $DocIconPath"

# --- 6. register the .graf association + document icon (HKCU) -------------------
if (-not $NoRegister) {
    Write-Host "==> Registering .graf for the current user"
    $classes = 'HKCU:\Software\Classes'

    New-Item -Force -Path "$classes\.graf" | Out-Null
    Set-ItemProperty -Path "$classes\.graf" -Name '(default)' -Value $ProgId

    New-Item -Force -Path "$classes\$ProgId" | Out-Null
    Set-ItemProperty -Path "$classes\$ProgId" -Name '(default)' -Value 'GrAF File'

    New-Item -Force -Path "$classes\$ProgId\DefaultIcon" | Out-Null
    # DefaultIcon: bare path (no quotes); spaces are allowed, ',0' picks the first icon.
    Set-ItemProperty -Path "$classes\$ProgId\DefaultIcon" -Name '(default)' -Value "$DocIconPath,0"

    New-Item -Force -Path "$classes\$ProgId\shell\open\command" | Out-Null
    # Command: quote the exe and the %1 argument (both can contain spaces).
    Set-ItemProperty -Path "$classes\$ProgId\shell\open\command" -Name '(default)' -Value ('"{0}" "%1"' -f $ExePath)

    # Tell the shell the associations changed so Explorer redraws icons.
    if (-not ('Win32.Shell' -as [type])) {
        Add-Type -Namespace Win32 -Name Shell -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, int flags, System.IntPtr item1, System.IntPtr item2);
"@
    }
    [Win32.Shell]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)  # SHCNE_ASSOCCHANGED
    Start-Process -NoNewWindow -FilePath "$env:SystemRoot\System32\ie4uinit.exe" -ArgumentList '-show' -ErrorAction SilentlyContinue

    Write-Host "    .graf -> $ProgId  (icon: $DocIconPath)"
    Write-Host "    open  -> `"$ExePath`" `"%1`""
}

Write-Host ""
Write-Host "Done. If a .graf file still shows a blank icon, log out/in or run:"
Write-Host '    ie4uinit.exe -ClearIconCache'