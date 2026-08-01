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

# Installer is optional -- skip quietly if Inno Setup's iscc isn't on PATH
# rather than failing the whole build over it. version is passed in via
# /D so installer.iss's own AppVersion never has to be hand-edited.
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if ($iscc) {
    Write-Output ""
    Write-Output "Building installer..."
    & $iscc.Source "/DMyAppVersion=$version" installer.iss
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Inno Setup build failed (exit $LASTEXITCODE)"
    }
    Write-Output "Done: dist\swtor-parser-v$version-setup.exe"
} else {
    Write-Output ""
    Write-Output "Skipping installer build: Inno Setup's iscc not found on PATH."
    Write-Output "Install it (winget install JRSoftware.InnoSetup) to also produce dist\swtor-parser-v$version-setup.exe."
}

Write-Output ""
Write-Output "Next: git tag v$version; git push origin v$version"
Write-Output "(pushing that tag triggers .github/workflows/release.yml, which builds and publishes the GitHub Release itself -- no need to run gh release create by hand)"
