; TokenSave Manager installer. Adapted from templates/release/installer.iss.tmpl (itself from OpenChem
; Studio, xaerogonzo/OpenChem-Studio, master 2026-10-22) for this build: three onefile executables and
; two folders of data, no runtime folder, no plugins, no registry settings.
;
; Compile with scripts/make_installer.py, which extracts the VALIDATED release zip and compiles from
; that tree, so the installer holds exactly the inventory the zip check passed:
;     ISCC /DAppVersion=2.7.0 /DSourceDir=<extracted top folder> /DOutputDir=release packaging\tokensave-manager.iss
;
; WHAT IT DOES
;   * Install: per-user by default (no administrator), or all-users if the person picks that in the
;     dialog Inno shows at the start. Installs all THREE executables, templates\ and docs\.
;   * Upgrade: the same AppId replaces the old version in place. templates\ and docs\ are DELETED
;     first, so a file the new version no longer ships cannot linger. Settings are not in the install
;     folder at all (they live in %LOCALAPPDATA%\TokenSaveManager\, see src/config_home.py), so an
;     upgrade cannot reach them.
;   * Uninstall: removes what the installer put down. A small form then asks whether to ALSO remove
;     the settings and the logs. Both are unticked: the default is to keep everything. Projects, their
;     tokensave indexes and anything under ~\.claude are never touched.
;
; Silent use (what scripts/drive_installer.py drives): /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /DIR=...
;   Silent uninstall keeps everything unless /REMOVESETTINGS=1 and/or /REMOVEDATA=1 is given.
;
; NOT SIGNED. Windows SmartScreen will say "unknown publisher" until it is.

#ifndef AppVersion
  #error AppVersion must be passed: ISCC /DAppVersion=2.7.0 ...
#endif
#ifndef SourceDir
  #error SourceDir must be passed: ISCC /DSourceDir=<the extracted release folder> ...
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

; A test build uses its own AppId (so it can never be mistaken for, or clobber, a real installation),
; a sandbox data folder (so removal can be tested without touching real settings), and cheaper
; compression. The shipped script is this same file with none of these defined.
#ifdef TestBuild
  #define AppIdGuid "{{EBB59DAF-EF6D-4126-8D4E-15C3486A2BF9}"
  #define OutputName "Tokensave-Manager-TEST-setup"
#else
  #define AppIdGuid "{{FFE8F0D8-9084-4EF1-9C36-B9D5C220BA88}"
  #define OutputName "Tokensave-Manager-v" + AppVersion + "-windows-setup"
#endif

[Setup]
AppId={#AppIdGuid}
AppName=TokenSave Manager
AppVersion={#AppVersion}
AppVerName=TokenSave Manager {#AppVersion}
AppPublisher=Alexander L Corthell
AppPublisherURL=https://github.com/xaerogonzo/Tokensave-Manager
AppSupportURL=https://github.com/xaerogonzo/Tokensave-Manager/issues
AppUpdatesURL=https://github.com/xaerogonzo/Tokensave-Manager/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName=TokenSave Manager
VersionInfoCompany=Alexander L Corthell
VersionInfoDescription=TokenSave Manager Setup
VersionInfoCopyright=Copyright (c) 2026 Alexander L Corthell
DefaultDirName={autopf}\TokenSave Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#SourceDir}\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
WizardStyle=modern
UninstallDisplayName=TokenSave Manager
UninstallDisplayIcon={app}\tokensave-manager.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
#ifndef TestBuild
; The running Manager holds a single-instance mutex; naming it makes Setup wait for the person to
; close it instead of failing on a locked executable. Left out of the test build so a Manager the
; developer has open cannot stall an unattended install test.
AppMutex=TokenSaveManager_SingleInstance
#endif
#ifdef TestBuild
Compression=lzma2/fast
SolidCompression=no
#else
Compression=lzma2/normal
SolidCompression=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; The program's own data folders go first on an upgrade (see the header). The executables are
; replaced in place. Nothing here is a folder people add to.
Type: filesandordirs; Name: "{app}\templates"
Type: filesandordirs; Name: "{app}\docs"

[Icons]
Name: "{autoprograms}\TokenSave Manager"; Filename: "{app}\tokensave-manager.exe"
Name: "{autodesktop}\TokenSave Manager"; Filename: "{app}\tokensave-manager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\tokensave-manager.exe"; Description: "Launch TokenSave Manager"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\templates"
Type: filesandordirs; Name: "{app}\docs"

[Code]
var
  RemoveSettings: Boolean;
  RemoveData: Boolean;

{ Where the application keeps its settings and logs. Must match src/config_home.py (APP_DIR). A test
  build reads it from /TESTDATADIR so removal can be tested without touching real settings. }
function DataFolder(): String;
begin
#ifdef TestBuild
  Result := ExpandConstant('{param:TESTDATADIR|{localappdata}\TokenSaveManager-TEST}');
#else
  Result := ExpandConstant('{localappdata}\TokenSaveManager');
#endif
end;

function ParamIsOne(Name: String): Boolean;
begin
  Result := ExpandConstant('{param:' + Name + '|0}') = '1';
end;

function AskWhatToRemove(): Boolean;
var
  Form: TSetupForm;
  Heading, Note, Warning: TNewStaticText;
  SettingsBox, DataBox: TNewCheckBox;
  OkButton, CancelButton: TNewButton;
begin
  Result := True;
  Form := CreateCustomForm(ScaleX(460), ScaleY(330), True, False);
  try
    Form.Caption := 'Uninstall TokenSave Manager';

    Heading := TNewStaticText.Create(Form);
    Heading.Parent := Form;
    Heading.Left := ScaleX(16);
    Heading.Top := ScaleY(14);
    Heading.Width := Form.ClientWidth - ScaleX(32);
    Heading.AutoSize := False;
    Heading.WordWrap := True;
    Heading.Height := ScaleY(48);
    Heading.Caption := 'The program will be removed. Your projects and their indexes are never deleted. ' +
      'Tick below only if you also want to remove what the program stored for you:';

    SettingsBox := TNewCheckBox.Create(Form);
    SettingsBox.Parent := Form;
    SettingsBox.Left := ScaleX(16);
    SettingsBox.Top := Heading.Top + Heading.Height + ScaleY(6);
    SettingsBox.Width := Form.ClientWidth - ScaleX(32);
    SettingsBox.Caption := 'Remove my settings (manager-config.json)';

    DataBox := TNewCheckBox.Create(Form);
    DataBox.Parent := Form;
    DataBox.Left := ScaleX(16);
    DataBox.Top := SettingsBox.Top + SettingsBox.Height + ScaleY(8);
    DataBox.Width := Form.ClientWidth - ScaleX(32);
    DataBox.Caption := 'Remove the logs';

    Note := TNewStaticText.Create(Form);
    Note.Parent := Form;
    Note.Left := ScaleX(36);
    Note.Top := DataBox.Top + DataBox.Height + ScaleY(2);
    Note.Width := Form.ClientWidth - ScaleX(52);
    Note.AutoSize := False;
    Note.WordWrap := True;
    Note.Height := ScaleY(40);
    Note.Caption := DataFolder();

    Warning := TNewStaticText.Create(Form);
    Warning.Parent := Form;
    Warning.Left := ScaleX(16);
    Warning.Top := Note.Top + Note.Height + ScaleY(6);
    Warning.Width := Form.ClientWidth - ScaleX(32);
    Warning.AutoSize := False;
    Warning.WordWrap := True;
    Warning.Height := ScaleY(64);
    Warning.Caption := 'If you registered tokensave-wrapper.exe with Claude Desktop, that entry will point at a ' +
      'file that no longer exists. Remove or repoint it there.';

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'Uninstall';
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    OkButton.Width := ScaleX(90);
    OkButton.Height := ScaleY(26);
    OkButton.Left := Form.ClientWidth - ScaleX(16) - ScaleX(90) - ScaleX(8) - ScaleX(90);
    OkButton.Top := Form.ClientHeight - ScaleY(26) - ScaleY(14);

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    CancelButton.Width := ScaleX(90);
    CancelButton.Height := ScaleY(26);
    CancelButton.Left := Form.ClientWidth - ScaleX(16) - ScaleX(90);
    CancelButton.Top := OkButton.Top;

    if Form.ShowModal() = mrOk then
    begin
      RemoveSettings := SettingsBox.Checked;
      RemoveData := DataBox.Checked;
    end
    else
      Result := False;
  finally
    Form.Free();
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  RemoveSettings := ParamIsOne('REMOVESETTINGS');
  RemoveData := ParamIsOne('REMOVEDATA');
  { Silent: only what the command line asked for. Interactive: ask, defaulting to keep. }
  if not UninstallSilent() then
    Result := AskWhatToRemove();
  Log(Format('Uninstall choices: remove settings=%d, remove data=%d', [Ord(RemoveSettings), Ord(RemoveData)]));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Folder: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Folder := RemoveBackslash(DataFolder());
  { Only ever the application's own folder, never a drive root or a parent of it. The files are
    named one by one: a person may keep other things there, and a folder is removed only if it is
    empty afterwards. }
  if (Pos('TokenSaveManager', ExtractFileName(Folder)) <> 1) or (not DirExists(Folder)) then
  begin
    if RemoveSettings or RemoveData then
      Log('Not removing ' + Folder + ': it is not a TokenSaveManager folder or does not exist');
    Exit;
  end;
  if RemoveSettings then
  begin
    Log(Format('Removing manager-config.json: %d', [Ord(DeleteFile(Folder + '\manager-config.json'))]));
    Log(Format('Removing config-migration.json: %d', [Ord(DeleteFile(Folder + '\config-migration.json'))]));
  end;
  if RemoveData then
    Log(Format('Removing logs: %d', [Ord(DelTree(Folder + '\logs', True, True, True))]));
  if RemoveSettings or RemoveData then
    Log(Format('Removing the folder if it is now empty: %d', [Ord(RemoveDir(Folder))]));
end;
