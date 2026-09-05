; Inno Setup script for Markdown Sidekick (roadmap X5, distribution Tier 3).
;
; Build (after a fresh `pyinstaller MarkdownSidekick.spec`):
;   ISCC.exe installer\MarkdownSidekick.iss
; Output: installer\Output\MarkdownSidekick-Setup-<version>.exe
;
; Defaults to a per-user install (no UAC prompt — right for freeware), with
; "install for all users" reachable via PrivilegesRequiredOverridesAllowed.

#define MyAppName "Markdown Sidekick"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "VibeProSoft"
#define MyAppURL "https://github.com/boblauzon/markdown-sidekick"
#define MyAppExeName "MarkdownSidekick.exe"
#define DistDir "..\dist\MarkdownSidekick"

[Setup]
; Stable GUID so upgrades replace instead of duplicating. Generated for this app.
AppId={{7E1B7C52-9C05-4D8E-A63B-2B54B1A5C21D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=MarkdownSidekick-Setup-{#MyAppVersion}
SetupIconFile=..\assets\MarkdownSidekick.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The PyInstaller folder is ~490 MB installed; say so up front.
ExtraDiskSpaceRequired=524288000
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nothing beyond {app}: settings/models in %LOCALAPPDATA%\MarkdownSidekick are
; the user's data and survive uninstall by design (documented in the guide).
