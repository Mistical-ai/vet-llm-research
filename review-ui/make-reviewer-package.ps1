<#
.SYNOPSIS
  Build a self-contained zip to send a reviewer: the review-ui app + their one
  humanN/ pack, with the unblinding key defensively excluded and a plain-
  language start-here file dropped in for them.

.DESCRIPTION
  Never packages an unblinding_key_*.json (it lives outside the pack folder
  anyway, but this script double-checks). Never touches your original
  data/*/humanN/ folder — everything is copied into a temp staging area, then
  zipped, then the staging area is removed.

.PARAMETER Pack
  Which reviewer pack to send, e.g. "human1".

.PARAMETER SourceRoot
  The folder that directly contains humanN/ pack folders. Defaults to
  data/pilot_human_review (same default as review-ui/config.js). Pass
  data/human_review for the real study once export-human-review has been run.

.PARAMETER OutputDir
  Where to write the finished zip. Defaults to review-ui/dist/.

.EXAMPLE
  .\make-reviewer-package.ps1 -Pack human1
  .\make-reviewer-package.ps1 -Pack human1 -SourceRoot "..\data\human_review"
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^human\d+$')]
  [string]$Pack,

  [string]$SourceRoot = (Join-Path $PSScriptRoot "..\data\pilot_human_review"),

  [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$SourceRoot = (Resolve-Path $SourceRoot).Path
$packDir = Join-Path $SourceRoot $Pack

if (-not (Test-Path $packDir)) {
  throw "Pack folder not found: $packDir`nCheck -Pack and -SourceRoot."
}
if (-not (Test-Path (Join-Path $PSScriptRoot "node_modules"))) {
  throw "review-ui\node_modules is missing. Run 'npm install' in review-ui\ first, " +
        "so the zip is self-contained for the reviewer (no npm install on their end)."
}

Write-Host ""
Write-Host "Packaging $Pack from:" -ForegroundColor Cyan
Write-Host "  $packDir"
Write-Host ""

# ---------------------------------------------------------------------------
# Stage in a throwaway temp folder, never touch the real data/ folder.
# ---------------------------------------------------------------------------
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("review-ui-pkg-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $stage | Out-Null

try {
  # 1. Copy review-ui/ itself (app code + node_modules), excluding dev-only
  #    folders a reviewer never needs (tests, this packaging script, dist/).
  $appDest = Join-Path $stage "review-ui"
  New-Item -ItemType Directory -Path $appDest | Out-Null
  $exclude = @("test", "dist", ".gitignore", "make-reviewer-package.ps1")
  Get-ChildItem -Path $PSScriptRoot -Force | Where-Object { $exclude -notcontains $_.Name } |
    ForEach-Object {
      Copy-Item -Path $_.FullName -Destination (Join-Path $appDest $_.Name) -Recurse -Force
    }

  # 2. Copy ONLY the chosen pack folder, into a sibling packs/ folder — never
  #    the unblinding key (which lives one level up from packDir, so a plain
  #    folder-copy of packDir can never pick it up in the first place).
  $packsDest = Join-Path $stage "packs"
  New-Item -ItemType Directory -Path $packsDest | Out-Null
  Copy-Item -Path $packDir -Destination (Join-Path $packsDest $Pack) -Recurse -Force

  # Defensive check: fail loudly if a key somehow ended up inside the pack
  # folder (should never happen, but this is the guard that matters).
  $leakedKeys = Get-ChildItem -Path $packsDest -Recurse -Filter "unblinding_key_*.json" -ErrorAction SilentlyContinue
  if ($leakedKeys) {
    throw "REFUSING TO PACKAGE: found an unblinding key inside the pack folder: $($leakedKeys.FullName -join ', ')"
  }

  # 3. Point the shipped config at the sibling packs/ folder.
  $configPath = Join-Path $appDest "config.js"
  (Get-Content $configPath -Raw) `
    -replace 'return path\.resolve\(__dirname, "\.\.", "data", "pilot_human_review"\);', `
             'return path.resolve(__dirname, "..", "packs");' |
    Set-Content $configPath -NoNewline

  # 4. Drop in the plain-language instructions for the reviewer.
  #
  # IMPORTANT: this is a SINGLE-quoted here-string (@'...'@), not a
  # double-quoted one. In a double-quoted PowerShell here-string the backtick
  # is an escape character (e.g. `r means carriage-return), which silently
  # mangles literal Markdown backticks like `review-ui` into garbage (an
  # earlier version of this script shipped "eview-ui" — the "r" got eaten as
  # an escape). Single-quoted = no escape processing at all, so backticks
  # survive as plain text. Variables can't interpolate inside a single-quoted
  # here-string, so {{PACK}} is substituted afterward with -replace instead.
  $startHereTemplate = @'
# Start here

This folder has everything you need to score your assigned articles. You do
not need to install anything except Node.js (one-time, see below).

## First time only

1. If you don't already have Node.js, install it from https://nodejs.org
   (the "LTS" version, the big green button). Just click through the installer.

## Every time you want to work on this

1. Open the `review-ui` folder.
2. Double-click `start.bat`.
   - Windows may show a blue "Windows protected your PC" (SmartScreen)
     screen, or your antivirus may ask you to confirm running it. This is
     normal for a file that didn't come from an app store - click "More
     info" then "Run anyway" (SmartScreen), or "Allow"/"Trust" in your
     antivirus. The file only starts a small program on your own computer;
     it doesn't send anything anywhere over the internet.
3. A black window will open, then your web browser should open automatically
   to a page titled "Human Validation". (If your browser doesn't open by
   itself after a few seconds, open it yourself and go to
   http://localhost:5173)
4. Click the card for your pack ({{PACK}}) to begin.
5. Read the guide, click "I'm ready", and score each item using the panel
   at the bottom of the screen. Your answers save automatically as you go.
6. When you're done for the session, just close the browser tab and the
   black window - your progress is saved and will be there when you reopen it.

## When you're completely finished

1. Open the `packs` folder (a sibling of `review-ui`, in the same place you
   unzipped everything).
2. Right-click the `{{PACK}}` folder inside it.
3. Choose "Send to" -> "Compressed (zipped) folder". Windows will create a
   file named `{{PACK}}.zip` right next to it.
4. Send that `{{PACK}}.zip` file back the same way you received this one
   (email attachment, or the shared link you were sent).

Do not rename anything or move files around inside the `{{PACK}}` folder
before zipping it.

Questions? Contact the person who sent you this.
'@
  $startHere = $startHereTemplate -replace '\{\{PACK\}\}', $Pack

  # Guard rail: -Encoding ascii below silently replaces any non-ASCII
  # character with '?' rather than erroring, which is exactly how an em-dash
  # once shipped to a reviewer as "???" before this check existed. Fail loud
  # here instead, at build time, rather than shipping mangled text silently.
  $nonAscii = $startHere.ToCharArray() | Where-Object { [int]$_ -gt 127 }
  if ($nonAscii) {
    throw ("START_HERE.md template contains non-ASCII character(s): " +
      (($nonAscii | Select-Object -Unique | ForEach-Object { "'$_' (U+{0:X4})" -f [int]$_ }) -join ', ') +
      ". Replace with a plain-ASCII equivalent (e.g. an em-dash -> ' - ') before packaging.")
  }

  # ASCII on purpose: the template above is deliberately kept to plain ASCII
  # (no em-dash, no smart quotes), and Windows PowerShell 5.1's -Encoding utf8
  # always prepends a byte-order-mark. Since there's nothing non-ASCII to
  # protect, plain ASCII avoids that invisible BOM entirely — simpler and
  # more universally readable for a file a non-technical reviewer opens in
  # whatever text app they have.
  Set-Content -Path (Join-Path $stage "START_HERE.md") -Value $startHere -NoNewline -Encoding ascii

  # 5. Zip it. Uses System.IO.Compression.ZipFile (not Compress-Archive):
  #    Compress-Archive embeds Windows backslashes as the internal zip entry
  #    separator, which is not the ZIP-spec standard ('/') and isn't
  #    guaranteed to unzip correctly everywhere. ZipFile.CreateFromDirectory
  #    normalizes to forward slashes, so the archive is portable regardless
  #    of what OS the reviewer unzips it on.
  if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }
  $zipPath = Join-Path $OutputDir "review-ui-$Pack.zip"
  if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stage, $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false  # don't include the stage folder itself as a top-level entry
  )

  # 6. Final safety check on the ZIP CONTENTS themselves, not just the staged folder.
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
  $keyEntries = $zip.Entries | Where-Object { $_.FullName -match 'unblinding_key' }
  $zip.Dispose()
  if ($keyEntries) {
    Remove-Item $zipPath -Force
    throw "REFUSING TO SHIP: the zip contained an unblinding key entry. Zip deleted. Investigate before retrying."
  }

  $sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
  Write-Host "Done." -ForegroundColor Green
  Write-Host "  Zip:  $zipPath"
  Write-Host "  Size: $sizeMB MB"
  Write-Host "  Verified: no unblinding_key_*.json in the zip."
  Write-Host ""
  Write-Host "Send this zip to your reviewer. They just need to unzip it and read START_HERE.md."
}
finally {
  Remove-Item -Path $stage -Recurse -Force -ErrorAction SilentlyContinue
}
