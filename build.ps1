# build.ps1
#
# Builds the standalone Windows package and zips it into a versioned
# release artifact. Run from the repo root:
#
#   .\build.ps1
#
# Output: dist\DPS-Dynamic-Parse-System\ (the runnable app folder)
#         dist\swtor-parser-v<version>-win64.zip (what gets uploaded to
#         a GitHub Release)

$ErrorActionPreference = "Stop"

$version = (Get-Content version.py | Select-String '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
if (-not $version) {
    Write-Error "Could not read __version__ from version.py"
}
Write-Output "Building swtor-parser v$version..."

python -m PyInstaller swtor_parser.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed (exit $LASTEXITCODE)"
}

$zipName = "swtor-parser-v$version-win64.zip"
$zipPath = "dist\$zipName"
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}
Compress-Archive -Path "dist\DPS-Dynamic-Parse-System" -DestinationPath $zipPath

Write-Output "Done: $zipPath"
Write-Output ""
Write-Output "Next: gh release create v$version $zipPath --title `"v$version`" --notes `"...`""
