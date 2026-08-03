; installer.iss
;
; Inno Setup script for a proper Windows installer (Start Menu shortcut,
; Add/Remove Programs entry, uninstaller) wrapping the PyInstaller onedir
; build build.ps1 already produces at dist\DPS-Dynamic-Parse-System\,
; instead of "unzip and run".
;
; Build with:  iscc installer.iss
; (build.ps1's optional installer step runs this automatically, passing
; the real version in via /DMyAppVersion so it never drifts from
; version.py by hand)

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyAppName "DPS - Dynamic Parse System"
#define MyAppExeName "DPS-Dynamic-Parse-System.exe"
#define MyAppPublisher "TheRealDaakal"

[Setup]
; Fixed, never regenerate -- Inno Setup uses this to recognise "this is the
; same app" across versions so upgrades replace in place instead of
; installing side-by-side.
AppId={{7E6C2F1A-6C39-4C86-9C0B-2F6E6E2A6F41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user (not {autopf}\Program Files) -- required for the in-app
; self-updater to be able to swap files in place without ever needing a
; UAC elevation prompt, the same reason Chrome/Discord/VS Code/Slack all
; install per-user by default. AppId is unchanged, so Inno Setup still
; recognises an existing install and reuses ITS location on upgrade
; (UsePreviousAppDir defaults on) -- an existing Program Files install
; from an older version stays there until a clean reinstall; only fresh
; installs land here going forward.
DefaultDirName={localappdata}\Programs\DPS-Dynamic-Parse-System
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=dist
OutputBaseFilename=swtor-parser-v{#MyAppVersion}-setup
Compression=lzma
SolidCompression=yes
DisableProgramGroupPage=yes
WizardStyle=modern
; Not code-signed (see README's distribution notes) -- SmartScreen will
; still warn on first run regardless of this setting; that's a signing-cost
; question, not something the installer script itself can fix.
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\DPS-Dynamic-Parse-System\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
