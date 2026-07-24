<#
.SYNOPSIS
  Build a self-contained zip to send a reviewer: the review-ui app + one or
  more humanN/ packs (their choice of which to work on), with unblinding
  keys defensively excluded and a plain-language start-here file dropped in.

.DESCRIPTION
  Never packages an unblinding_key_*.json (it lives outside every pack
  folder anyway, but this script double-checks). Never touches your original
  data/*/humanN/ folders — everything is copied into a temp staging area,
  then zipped, then the staging area is removed.

  The reviewer sees one card per pack you include and picks whichever they
  want from the app's pack-picker screen — they are not limited to a single
  pack unless you only include one.

.PARAMETER Pack
  Which reviewer pack(s) to send, e.g. "human1" or "human1","human2","human3".

.PARAMETER AllPacks
  Include every humanN/ pack folder found directly under -SourceRoot,
  instead of naming them individually with -Pack.

.PARAMETER SourceRoot
  The folder that directly contains humanN/ pack folders. Defaults to
  data/pilot_human_review (same default as review-ui/config.js). Pass
  data/human_review for the real study once export-human-review has been run.

.PARAMETER OutputDir
  Where to write the finished zip. Defaults to review-ui/dist/.

.EXAMPLE
  .\make-reviewer-package.ps1 -Pack human1
  .\make-reviewer-package.ps1 -Pack human1,human2,human3
  .\make-reviewer-package.ps1 -AllPacks
  .\make-reviewer-package.ps1 -AllPacks -SourceRoot "..\data\human_review"
#>
param(
  [string[]]$Pack,

  [switch]$AllPacks,

  [string]$SourceRoot = (Join-Path $PSScriptRoot "..\data\pilot_human_review"),

  [string]$OutputDir = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"

$SourceRoot = (Resolve-Path $SourceRoot).Path

# ---------------------------------------------------------------------------
# Resolve which packs to include.
# ---------------------------------------------------------------------------
if ($AllPacks) {
  $resolvedPacks = Get-ChildItem -Path $SourceRoot -Directory |
    Where-Object { $_.Name -match '^human\d+$' } |
    Select-Object -ExpandProperty Name |
    Sort-Object { [int]($_ -replace 'human', '') }
  if (-not $resolvedPacks) {
    throw "No humanN/ pack folders found under $SourceRoot."
  }
} elseif ($Pack) {
  foreach ($p in $Pack) {
    if ($p -notmatch '^human\d+$') {
      throw "Invalid pack name '$p' - must look like 'human1', 'human2', etc."
    }
  }
  $resolvedPacks = $Pack
} else {
  throw ("Specify which pack(s) to include: -Pack human1,human2  " +
    "or  -AllPacks to include every pack found under -SourceRoot ($SourceRoot).")
}

foreach ($p in $resolvedPacks) {
  $pd = Join-Path $SourceRoot $p
  if (-not (Test-Path $pd)) {
    throw "Pack folder not found: $pd`nCheck -Pack / -AllPacks and -SourceRoot."
  }
}
if (-not (Test-Path (Join-Path $PSScriptRoot "node_modules"))) {
  throw "review-ui\node_modules is missing. Run 'npm install' in review-ui\ first, " +
        "so the zip is self-contained for the reviewer (no npm install on their end)."
}

Write-Host ""
Write-Host ("Packaging {0} pack(s): {1}" -f $resolvedPacks.Count, ($resolvedPacks -join ", ")) -ForegroundColor Cyan
Write-Host "  from: $SourceRoot"
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

  # 2. Copy EACH chosen pack folder, into a sibling packs/ folder — never an
  #    unblinding key (which lives one level up from each pack folder, so a
  #    plain folder-copy of a pack directory can never pick one up).
  $packsDest = Join-Path $stage "packs"
  New-Item -ItemType Directory -Path $packsDest | Out-Null
  foreach ($p in $resolvedPacks) {
    Copy-Item -Path (Join-Path $SourceRoot $p) -Destination (Join-Path $packsDest $p) -Recurse -Force
  }

  # Defensive check: fail loudly if a key somehow ended up inside a pack
  # folder (should never happen, but this is the guard that matters). Checks
  # the WHOLE packs/ tree at once, so it covers every pack copied above with
  # no per-pack loop needed.
  $leakedKeys = Get-ChildItem -Path $packsDest -Recurse -Filter "unblinding_key_*.json" -ErrorAction SilentlyContinue
  if ($leakedKeys) {
    throw "REFUSING TO PACKAGE: found an unblinding key inside a pack folder: $($leakedKeys.FullName -join ', ')"
  }

  # 3. Point the shipped config at the sibling packs/ folder.
  $configPath = Join-Path $appDest "config.js"
  $configRaw = Get-Content $configPath -Raw
  $configPatched = $configRaw -replace 'return path\.resolve\(__dirname, "\.\.", "data", "\w+"\);', `
                                        'return path.resolve(__dirname, "..", "packs");'
  # Guard rail: if config.js's default REVIEW_ROOT line ever changes shape
  # (e.g. a different default folder, reworded code), this -replace would
  # silently match nothing and ship a zip whose config.js still points at
  # YOUR local data/ folder instead of the packs/ folder that travels in the
  # zip — a real, silent failure that would only surface as a confusing "no
  # packs found" on the reviewer's machine. Fail loud at build time instead.
  if ($configPatched -eq $configRaw) {
    # Single-quoted strings below on purpose: PowerShell has no backslash
    # escaping (\" is NOT valid PowerShell and previously broke this exact
    # line's parsing) — single-quoted strings need no escaping at all for the
    # double quotes in this message, only '' for a literal single quote.
    throw ('REFUSING TO PACKAGE: could not find/patch the REVIEW_ROOT default line in ' +
      'config.js (looked for a ''return path.resolve(__dirname, "..", "data", "...");'' ' +
      'line). config.js''s default may have changed shape - update the -replace pattern ' +
      'in this script to match.')
  }
  Set-Content -Path $configPath -Value $configPatched -NoNewline

  # 4. Drop in the plain-language instructions for the reviewer.
  #
  # IMPORTANT: this is a SINGLE-quoted here-string (@'...'@), not a
  # double-quoted one. In a double-quoted PowerShell here-string the backtick
  # is an escape character (e.g. `r means carriage-return), which silently
  # mangles literal Markdown backticks like `review-ui` into garbage (an
  # earlier version of this script shipped "eview-ui" — the "r" got eaten as
  # an escape). Single-quoted = no escape processing at all, so backticks
  # survive as plain text. Variables can't interpolate inside a single-quoted
  # here-string, so {{PACK_LIST}} is substituted afterward with -replace.
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
4. You'll see a card for each pack that's available to you: {{PACK_LIST}}.
   Click whichever one you'd like to start with - if you're not sure which
   is meant for you, check with the person who sent you this.
5. Read the guide, click "I'm ready", and score each item using the panel
   at the bottom of the screen. Your answers save automatically as you go.
6. When you're done for the session, just close the browser tab and the
   black window - your progress is saved and will be there when you reopen
   the same pack. You can click the "All packs" button at the top of the
   scoring screen at any time to go back and choose a different pack.

## When you're completely finished

1. Right-click the `packs` folder (a sibling of `review-ui`, in the same
   place you unzipped everything).
2. Choose "Send to" -> "Compressed (zipped) folder". Windows will create a
   file named `packs.zip` right next to it.
3. {{SEND_BACK_LINE}}

Do not rename anything or move files around inside the `packs` folder
before zipping it.

{{QUESTIONS_LINE}}
'@

  # SENDER_CONTACT is read from config.js's own default (single source of
  # truth shared with the in-app "I'm finished" screen, which reads the same
  # value at runtime via server.js's /api/packs — see public/app.js
  # finishReview()). Read it BEFORE the REVIEW_ROOT patch above changes
  # $configPath's content, though the two edits target different lines so
  # order doesn't actually matter here — kept after for clarity of intent.
  $senderContactMatch = [regex]::Match(
    (Get-Content $configPath -Raw),
    'SENDER_CONTACT:\s*process\.env\.SENDER_CONTACT\s*\|\|\s*"([^"]*)"'
  )
  $senderContact = if ($senderContactMatch.Success) { $senderContactMatch.Groups[1].Value } else { "" }

  if ($senderContact) {
    $sendBackLine = "Send that ``packs.zip`` file back to **$senderContact** the same way you received this one (email attachment, or the shared link you were sent)."
    $questionsLine = "Questions? Contact $senderContact."
  } else {
    $sendBackLine = "Send that ``packs.zip`` file back the same way you received this one (email attachment, or the shared link you were sent)."
    $questionsLine = "Questions? Contact the person who sent you this."
  }

  # .Replace() (literal string replace), not -replace (regex), for the last
  # two: -replace treats a literal '$' in the REPLACEMENT text as a regex
  # backreference marker (e.g. '$1'), which could silently corrupt an email
  # address or contact string containing one. Plain .Replace() has no such
  # special-character interpretation.
  $startHere = $startHereTemplate -replace '\{\{PACK_LIST\}\}', ($resolvedPacks -join ", ")
  $startHere = $startHere.Replace('{{SEND_BACK_LINE}}', $sendBackLine)
  $startHere = $startHere.Replace('{{QUESTIONS_LINE}}', $questionsLine)

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
  $packsLabel = if ($resolvedPacks.Count -le 4) { $resolvedPacks -join "-" } else { "{0}packs" -f $resolvedPacks.Count }
  $zipPath = Join-Path $OutputDir "review-ui-$packsLabel.zip"
  if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stage, $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false  # don't include the stage folder itself as a top-level entry
  )

  # 6. Final safety check on the ZIP CONTENTS themselves, not just the staged folder.
  $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
  $keyEntries = $zip.Entries | Where-Object { $_.FullName -match 'unblinding_key' }
  $zip.Dispose()
  if ($keyEntries) {
    Remove-Item $zipPath -Force
    throw "REFUSING TO SHIP: the zip contained an unblinding key entry. Zip deleted. Investigate before retrying."
  }

  $sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
  Write-Host "Done." -ForegroundColor Green
  Write-Host "  Zip:    $zipPath"
  Write-Host "  Size:   $sizeMB MB"
  Write-Host ("  Packs:  {0}" -f ($resolvedPacks -join ", "))
  Write-Host "  Verified: no unblinding_key_*.json in the zip."
  Write-Host ""
  Write-Host "Send this zip to your reviewer. They'll see a card for each pack and can choose which to work on."
}
finally {
  Remove-Item -Path $stage -Recurse -Force -ErrorAction SilentlyContinue
}
