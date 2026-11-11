; Inno Setup script for Markdown Sidekick (roadmap X5, distribution Tier 3).
;
; Build (after a fresh `pyinstaller MarkdownSidekick.spec`):
;   ISCC.exe installer\MarkdownSidekick.iss
; Output: installer\Output\MarkdownSidekick-Setup-<version>.exe
;
; Per-user installs only (no UAC prompt, and updates never need one): an
; all-users copy from 1.2 or earlier is offered a one-time removal.
; Test builds pass /DOldInstallRoot=HKCU /DAllUsersKey=... /DUserDataDir=...
; so they never touch a real install or the real user data, and
; /DWebView2Key=... to act as a PC without the WebView2 runtime.
;
; The app opens the window chosen in its Settings, and that is the new one
; unless the user picked the classic one, so a first install and every
; upgrade from 1.3 or earlier opens the new window. The new window needs
; Microsoft's Edge WebView2 Runtime; without it the app opens the classic
; window and says why, and Setup's last page offers the download page.

#define MyAppName "Markdown Sidekick"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "VibeProSoft"
#define MyAppURL "https://github.com/boblauzon/markdown-sidekick"
#define MyAppExeName "MarkdownSidekick.exe"
#ifndef DistDir
  #define DistDir "..\dist\MarkdownSidekick"
#endif
#ifndef OldInstallRoot
  #define OldInstallRoot "HKLM64"
#endif
#ifndef AllUsersKey
  #define AllUsersKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E1B7C52-9C05-4D8E-A63B-2B54B1A5C21D}_is1"
#endif
#ifndef UserDataDir
  #define UserDataDir "MarkdownSidekick"
#endif
; The WebView2 runtime's EdgeUpdate client key (Microsoft's documented check),
; and where errors.WEBVIEW2_URL sends the user from the app.
#ifndef WebView2Key
  #define WebView2Key "Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
#endif
#define WebView2Url "https://developer.microsoft.com/microsoft-edge/webview2/"

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
; Running copies are closed, never overwritten in use: the window, the CLI and
; the --mcp server an AI tool keeps open (Restart Manager finds them by the
; files they hold). The app relaunches itself after an update (/RELAUNCH=1).
CloseApplications=force
RestartApplications=no
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

[InstallDelete]
; Every upgrade starts from an empty _internal. Inno never deletes files a new
; version stopped shipping, and the app reads what's left: an upgraded 1.2.0
; kept 164 old files, and its MCP server reported fastmcp 3.4.2 instead of 4.1.0.
; User data lives in %LOCALAPPDATA%\MarkdownSidekick, never here.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Only on a PC without the WebView2 runtime: Microsoft's download page, so
; the next start can open the new window (until then the classic one opens).
Filename: "{#WebView2Url}"; Description: "Get Microsoft Edge WebView2 Runtime (the new window needs it)"; Flags: shellexec nowait postinstall skipifsilent; Check: WebView2Missing
; No arguments: the app opens the window chosen in Settings (new by default).
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
; An in-app update runs Setup silently; this reopens the app afterwards. An
; update started from the new window reopens it (/RELAUNCHUI=new), even if
; it was opened with --new-ui and Settings say classic.
Filename: "{app}\{#MyAppExeName}"; Parameters: "{code:RelaunchArgs}"; Flags: nowait skipifnotsilent; Check: RelaunchRequested

[UninstallDelete]
; Nothing beyond {app}: settings, logs and models in %LOCALAPPDATA% are the
; user's data. An interactive uninstall asks about them (CurUninstallStepChanged).

[Code]
function RelaunchRequested: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

{ Only a fixed word is accepted, so nothing on Setup's command line is ever
  passed on to the app. }
function RelaunchArgs(Param: String): String;
begin
  if ExpandConstant('{param:RELAUNCHUI|}') = 'new' then Result := '--new-ui' else Result := '';
end;

{ The WebView2 runtime is installed for all users (the 32-bit registry view,
  WOW6432Node) or for this user only. An uninstalled one can leave pv at
  0.0.0.0. }
function WebView2Installed(Root: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, 'SOFTWARE\{#WebView2Key}', 'pv', Version) and
            (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2Missing: Boolean;
begin
  Result := not (WebView2Installed(HKLM32) or WebView2Installed(HKCU));
  if Result then Log('Microsoft Edge WebView2 Runtime not found: the app will open its classic window');
end;

function AllUsersCopy(var Version, Uninstaller, Location: String): Boolean;
begin
  Result := RegQueryStringValue({#OldInstallRoot}, '{#AllUsersKey}', 'UninstallString', Uninstaller);
  if Result then begin
    if not RegQueryStringValue({#OldInstallRoot}, '{#AllUsersKey}', 'DisplayVersion', Version) then Version := '';
    if not RegQueryStringValue({#OldInstallRoot}, '{#AllUsersKey}', 'InstallLocation', Location) then Location := '';
  end;
end;

{ Offer once to remove an all-users copy (1.2 and earlier could install one).
  Its uninstaller asks Windows for permission itself, then hands off to a temp
  copy and exits at once, so we wait for its uninstall entry to go instead. }
procedure OfferToRemoveAllUsersCopy;
var
  Version, Uninstaller, Location: String;
  Code, Waited: Integer;
begin
  if not AllUsersCopy(Version, Uninstaller, Location) then exit;
  if MsgBox('Markdown Sidekick ' + Version + ' is installed for all users in:' + #13#10 + Location + #13#10#13#10 +
            'This version installs for you only, so it can update without asking for administrator permission. ' +
            'Remove the all-users copy now? Windows asks for permission once. Your settings are kept.' + #13#10#13#10 +
            'Choose No to keep both copies.', mbConfirmation, MB_YESNO) <> IDYES then begin
    Log('Keeping the all-users copy at ' + Location);
    exit;
  end;
  if ShellExec('', RemoveQuotes(Uninstaller), '/SILENT /NORESTART', '', SW_SHOW, ewNoWait, Code) then
    for Waited := 1 to 240 do begin
      if not AllUsersCopy(Version, Uninstaller, Location) then exit;
      Sleep(500);
    end;
  MsgBox('The all-users copy is still installed (permission declined, or its uninstaller failed). ' +
         'Setup continues; you can remove it later in Windows Settings > Apps.', mbInformation, MB_OK);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if WizardSilent() then exit;  { an in-app update: nothing to ask }
  OfferToRemoveAllUsersCopy;
  if CheckForMutexes('MarkdownSidekickMCP') then
    MsgBox('An AI tool (such as Claude Desktop) is using Markdown Sidekick. Setup will close it; ' +
           'restart the AI tool afterwards to reconnect.', mbInformation, MB_OK);
end;

function DirSize(const Dir: String): Int64;
var
  Rec: TFindRec;
begin
  Result := 0;
  if FindFirst(Dir + '\*', Rec) then
    try
      repeat
        if (Rec.Name <> '.') and (Rec.Name <> '..') then
          if Rec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
            Result := Result + DirSize(Dir + '\' + Rec.Name)
          else
            Result := Result + Int64(Rec.SizeHigh) * 4294967296 + Rec.SizeLow;
      until not FindNext(Rec);
    finally
      FindClose(Rec);
    end;
end;

{ Uninstall asks whether the user's own data goes too (default: keep it).
  A silent uninstall never asks and keeps it. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if (CurUninstallStep <> usPostUninstall) or UninstallSilent() then exit;
  Data := ExpandConstant('{localappdata}\{#UserDataDir}');
  if not DirExists(Data) then exit;
  if MsgBox('Also remove your settings, logs and downloaded models (' + IntToStr(DirSize(Data) div 1048576) +
            ' MB)?' + #13#10#13#10 + Data, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(Data, True, True, True);
end;
