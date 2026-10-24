# EAI Studio - fetch the Electron binary (2026-10-24)
#
# Why this script exists: npm's postinstall and electron/install.js can both
# exit 0 without downloading anything (the "up to date in 251ms" case), so the
# launcher keeps this as the last resort.
#
# Order of sources: official GitHub release first, npmmirror only as fallback.
# The zip goes into "<AppDir>\_install_cache\"; the zip and that folder are
# removed once extraction finishes - nothing is left outside the app dir.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools\get-electron.ps1 -AppDir "D:\...\EAI_Studio"

param(
  [Parameter(Mandatory = $true)][string]$AppDir
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$pkgDir = Join-Path $AppDir "node_modules\electron"
$dist = Join-Path $pkgDir "dist"
$exe = Join-Path $dist "electron.exe"

# Version resolution without npm (2026-10-24): npm is NOT required to install
# Electron. Try the installed package, then package-lock.json, then the range in
# package.json ("^33.4.11" -> 33.4.11), then the hard-coded default.
function Get-ElectronVersion {
  $candidates = @()
  $pkgJson = Join-Path $pkgDir "package.json"
  if (Test-Path $pkgJson) { $candidates += (Get-Content $pkgJson -Raw | ConvertFrom-Json).version }

  $lock = Join-Path $AppDir "package-lock.json"
  if (Test-Path $lock) {
    try {
      $j = Get-Content $lock -Raw | ConvertFrom-Json
      if ($j.packages -and $j.packages.'node_modules/electron') { $candidates += $j.packages.'node_modules/electron'.version }
      if ($j.dependencies -and $j.dependencies.electron) { $candidates += $j.dependencies.electron.version }
    } catch { }
  }

  $rootPkg = Join-Path $AppDir "package.json"
  if (Test-Path $rootPkg) {
    try {
      $j = Get-Content $rootPkg -Raw | ConvertFrom-Json
      foreach ($dep in @($j.devDependencies.electron, $j.dependencies.electron)) {
        if ($dep) { $candidates += ($dep -replace '^[~^>=<\s]+', '' -replace '\s.*$', '') }
      }
    } catch { }
  }

  $candidates += "33.4.11"
  foreach ($c in $candidates) {
    if ($c -match '^\d+\.\d+\.\d+') { return ($c -replace '\.\d+-.*$', '') }
  }
  return "33.4.11"
}

$version = Get-ElectronVersion
if (-not (Test-Path $pkgDir)) { New-Item -ItemType Directory -Path $pkgDir -Force | Out-Null }
$arch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "ia32" }
$zipName = "electron-v$version-win32-$arch.zip"

$cacheDir = Join-Path $AppDir "_install_cache"
$zip = Join-Path $cacheDir $zipName

if (Test-Path $exe) {
  Write-Host "  [electron] already present, nothing to do"
  exit 0
}
if (-not (Test-Path $cacheDir)) { New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null }

# Official source first, China mirror as fallback. A plain Invoke-WebRequest has no
# "too slow" guard: GitHub can be reachable but crawl at a few KB/s, so every attempt
# goes through curl with --speed-limit/--speed-time and gets aborted when it stalls.
$sources = @(
  @{ name = "official"; url = "https://github.com/electron/electron/releases/download/v$version/$zipName"; max = 600 },
  @{ name = "mirror";   url = "https://npmmirror.com/mirrors/electron/$version/$zipName";              max = 900 }
)

$curl = Join-Path $env:SystemRoot "System32\curl.exe"

function Save-Zip([string]$url, [string]$out, [int]$maxSec) {
  if (Test-Path $out) { Remove-Item $out -Force }
  if (Test-Path $curl) {
    # Start-Process keeps curl's progress/stderr out of PowerShell's error stream
    # ("$ErrorActionPreference = Stop" turns native stderr into a terminating error).
    $errLog = Join-Path $cacheDir "curl.log"
    $args = @("-L", "--fail", "--silent", "--show-error",
              "--max-time", "$maxSec", "--speed-limit", "50000", "--speed-time", "20",
              "-o", "`"$out`"", "`"$url`"")
    $p = Start-Process -FilePath $curl -ArgumentList $args -NoNewWindow -Wait -PassThru `
         -RedirectStandardError $errLog
    if ($p.ExitCode -ne 0) {
      $msg = ""
      if (Test-Path $errLog) { $msg = (Get-Content $errLog -Raw).Trim() }
      throw "curl exit $($p.ExitCode). $msg"
    }
  } else {
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec $maxSec
  }
  if (-not (Test-Path $out)) { throw "no file written" }
  if ((Get-Item $out).Length -lt 50MB) { throw "file too small ($((Get-Item $out).Length) bytes)" }
}

$got = $false
foreach ($s in $sources) {
  try {
    Write-Host "  [electron] downloading v$version ($arch) from $($s.name)"
    Write-Host "             $($s.url)"
    Save-Zip -url $s.url -out $zip -maxSec $s.max
    $got = $true
    break
  } catch {
    Write-Host "  [electron] $($s.name) failed: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

if (-not $got) {
  Remove-Item $cacheDir -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "  [electron] every source failed - check network/proxy and retry" -ForegroundColor Red
  exit 3
}

Write-Host "  [electron] extracting into node_modules\electron\dist ..."
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $dist -Force

# electron's install.js uses these two markers to decide "already installed"
[IO.File]::WriteAllText((Join-Path $pkgDir "path.txt"), "electron.exe", (New-Object System.Text.UTF8Encoding($false)))
[IO.File]::WriteAllText((Join-Path $dist "version"), "v$version", (New-Object System.Text.UTF8Encoding($false)))

# leave nothing behind: zip + cache folder go away together
Remove-Item $cacheDir -Recurse -Force -ErrorAction SilentlyContinue

if (Test-Path $exe) {
  $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
  Write-Host "  [electron] done: electron.exe $mb MB"
  exit 0
}
Write-Host "  [electron] electron.exe still missing after extraction (antivirus?)" -ForegroundColor Red
exit 4
