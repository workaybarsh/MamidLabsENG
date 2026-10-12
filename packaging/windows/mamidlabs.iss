; MamidLabs one-click setup wizard (Inno Setup 6).
;
; Build: iscc packaging\windows\mamidlabs.iss   -> dist\MamidLabs_Setup.exe
; GitHub Actions: .github/workflows/installer.yml
;
; The wizard extracts the program files under {app}\setup-source and leaves the actual
; work of the installation to scripts\setup-run.ps1: prerequisites, infrastructure, the
; chosen profile's models, own model/API preference, smoke test and summary. Wizard pages:
; welcome, license, folder, profile report, profile choice, your own models, API,
; additional tasks, ready, installing, summary, finish.

; The single source of the version. CI compares the tag (v1.0.0) with this value;
; CHANGELOG.md must have a section with the same number.
#define MyAppVersion "1.0.0"
#define MyAppName "MamidLabs"
#define MyAppPublisher "workaybarsh"
#define MyAppURL "https://github.com/workaybarsh/MamidLabsENG"

[Setup]
AppId={{BB5BDCB4-3C19-4599-88C4-5CEA1A7942CB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
VersionInfoProductName={#MyAppName}

; The standard per-user program location: PrivilegesRequired=lowest, so {autopf} resolves
; to %LOCALAPPDATA%\Programs. Program Files can't be used: the app writes settings/logs/
; models under its own root, which would need elevation there. Updates go to the existing
; install folder via UsePreviousAppDir. The user can change the folder.
DefaultDirName={autopf}\MamidLabs
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
LicenseFile=..\..\EULA.txt

OutputDir=..\..\dist
OutputBaseFilename=MamidLabs_Setup

SetupIconFile=..\..\assets\icons\mamidlabs.ico
WizardImageFile=..\..\assets\installer\wizard_side.bmp,..\..\assets\installer\wizard_side@2x.bmp
WizardSmallImageFile=..\..\assets\installer\wizard_small.bmp,..\..\assets\installer\wizard_small@2x.bmp
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\assets\icons\mamidlabs.ico

WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
Compression=lzma2
SolidCompression=yes
DisableWelcomePage=no
; The package is only a few MB; the real space goes to the infrastructure (PowerShell,
; llama.cpp, torch, ComfyUI). Let the folder page show the real minimum and warn if there is
; no room. The models need another ~36 GB on LITE and ~52 GB on Qwen3.8 27B (SelectDirLabel3).
ExtraDiskSpaceRequired=12884901888
DisableReadyMemo=no
Uninstallable=yes
CreateUninstallRegKey=yes

; Language selection: the user can pick a Turkish or English wizard UI at install time.
; The first entry is the default (English edition -> English).
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[Messages]
SetupWindowTitle=MamidLabs Setup Wizard
WelcomeLabel1=Welcome to MamidLabs
SelectDirDesc=Where should MamidLabs and its models be installed?
SelectDirLabel3=Including the models, ~58 GB (LITE) or ~75 GB (Qwen3.8 27B) of free space is needed; choose a fast SSD.
FinishedHeadingLabel=MamidLabs setup complete
FinishedLabelNoIcons=MamidLabs has been installed on your computer. You can open it with the MamidLabs shortcut on the desktop.

[Tasks]
Name: "media"; Description: "Image and video generation - generate/edit photos and generate video (ComfyUI + Krea-2 + Wan 2.2, ~20 GB depending on the profile). Uncheck to install only chat, documents, OCR and audio"; GroupDescription: "Components (recommended):"
Name: "audio"; Description: "Audio capture - transcribe a microphone recording or an audio file to text; speech plus non-speech audio events (Whisper large-v3 + AST, ~4 GB)"; GroupDescription: "Components (recommended):"
Name: "ocr"; Description: "OCR - reads the text inside a photo, a scanned page or a screenshot and converts it to text (e.g. an invoice or a handwritten note); about 5.5 GB"; GroupDescription: "Components (recommended):"
Name: "smoketest"; Description: "Run the smoke test at the end of setup (chat, document, image, model switch) - can take a long time (tens of minutes)"; GroupDescription: "Verification:"; Flags: checkedonce

[Files]
; Program source. setup-run.ps1 uses it as install-mamidlabs.ps1 -Source;
; on update the same folder is rewritten.
Source: "..\..\qwen-desktop.ps1"; DestDir: "{app}\setup-source"; Flags: ignoreversion
Source: "..\..\launch-qwen-desktop.vbs"; DestDir: "{app}\setup-source"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}\setup-source"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}\setup-source"; Flags: ignoreversion
Source: "..\..\EULA.txt"; DestDir: "{app}\setup-source"; Flags: ignoreversion
Source: "..\..\agent\*"; DestDir: "{app}\setup-source\agent"; Excludes: "__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\scripts\*"; DestDir: "{app}\setup-source\scripts"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\assets\*"; DestDir: "{app}\setup-source\assets"; Excludes: "installer\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\requirements\*"; DestDir: "{app}\setup-source\requirements"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\docs\installation.md"; DestDir: "{app}\setup-source\docs"; Flags: ignoreversion
; The profile report runs BEFORE the installation, from a temporary folder.
Source: "..\..\scripts\profile-report.ps1"; Flags: dontcopy
Source: "..\..\agent\profiles.json"; Flags: dontcopy

[Run]
Filename: "{app}\launch-qwen-desktop.vbs"; Description: "Open MamidLabs now"; Flags: postinstall shellexec nowait skipifsilent; Check: LauncherExists

[UninstallDelete]
; The real cleanup is done by scripts\uninstall-mamidlabs.ps1 (CurUninstallStepChanged): it stops
; the processes and deletes the models/caches/chats, the desktop shortcut belonging to this
; installation and the prerequisites MamidLabs installed. What follows is a fallback in case the
; script cannot run. The desktop shortcut is not deleted here: it may be another copy's shortcut.
Type: filesandordirs; Name: "{app}\setup-source"
Type: filesandordirs; Name: "{app}\agent"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\assets"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\llama.cpp"
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\logs"
Type: files; Name: "{app}\qwen-desktop.ps1"
Type: files; Name: "{app}\launch-qwen-desktop.vbs"
Type: files; Name: "{app}\README.md"
Type: files; Name: "{app}\LICENSE"
; The folder may be a user-chosen place containing other files; it is deleted only if empty.
Type: dirifempty; Name: "{app}"

[Code]
var
  ReportPage: TOutputMsgMemoWizardPage;
  ProfilePage: TInputOptionWizardPage;
  OwnModelPage: TInputFileWizardPage;
  ApiPage: TInputOptionWizardPage;
  SummaryPage: TOutputMsgMemoWizardPage;
  ReportedDir: String;
  SetupExitCode: Integer;
  InstallRunning: Boolean;
  InstallCancelled: Boolean;
  { Inno's own Cancel button does not fire the CancelButtonClick hook while ssPostInstall's
    CurStepChanged is running (not even inside the PumpMessages loop). While installing, our own
    button bound to a normal OnClick is shown in its place. }
  CustomCancelButton: TNewButton;

const
  OptLite = 0;
  { FLASH and MAX use exactly the same files (identical "target" paths in agent/profiles.json);
    only their runtime settings (reasoning effort/predict) differ. Setup offers a single
    "PRO" (Qwen3.8 27B) option and downloads with the 'flash' catalog entry. Switching between FLASH
    and MAX is done from the app's model bar (agent/model-registry.ps1). }
  OptQwen38 = 1;
  OptLater = 2;
  OptOwn = 3;

{ Abort inside CurStepChanged only stops the rest of the procedure; Inno's step machine keeps
  moving on to the Summary/Finished pages after ssPostInstall. In a cancelled setup the process
  is terminated right after the rollback finishes. }
procedure ExitProcess(uExitCode: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

function PowerShellExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function LauncherExists: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\launch-qwen-desktop.vbs'));
end;

procedure CustomCancelButtonClick(Sender: TObject); forward;

function SelectedProfile: String;
begin
  case ProfilePage.SelectedValueIndex of
    OptLite: Result := 'lite';
    OptQwen38: Result := 'flash';
  else
    Result := 'none';
  end;
end;

procedure InitializeWizard;
var
  NL: String;
begin
  NL := Chr(13) + Chr(10);

  { Welcome page: a large title and a short introduction. }
  WizardForm.WelcomeLabel1.Caption := 'Welcome to MamidLabs';
  WizardForm.WelcomeLabel1.Font.Size := 18;
  WizardForm.WelcomeLabel1.Font.Style := [fsBold];
  WizardForm.WelcomeLabel1.AutoSize := False;
  WizardForm.WelcomeLabel1.Top := ScaleY(22);
  WizardForm.WelcomeLabel1.Height := ScaleY(62);

  WizardForm.WelcomeLabel2.Caption :=
    'A fully local, multimodal AI workspace.' + NL + NL +
    'Chat, web search, documents, OCR, image understanding, image and video generation ' +
    'and photo editing in one window; the models run on your own graphics card.' + NL + NL +
    'The wizard first measures your computer and recommends a suitable profile. You can download ' +
    'the models now, leave them for later, or use your own model or API ' +
    'account.' + NL + NL +
    'Click Next to continue.';
  WizardForm.WelcomeLabel2.Font.Size := 10;
  WizardForm.WelcomeLabel2.AutoSize := False;
  WizardForm.WelcomeLabel2.Top := ScaleY(100);
  WizardForm.WelcomeLabel2.Height := ScaleY(190);

  ReportPage := CreateOutputMsgMemoPage(wpSelectDir,
    'Profile report', 'Which profile can this computer run?',
    'The graphics card, memory and disk were measured. The recommended profile comes preselected on the next page.', '');

  ProfilePage := CreateInputOptionPage(ReportPage.ID,
    'Model profile', 'Which models should be installed?',
    'The profile files are downloaded at the end of setup and their sizes are verified. If you like, skip the download now; ' +
    'you can install them any time in the app with Profiles → Profile manager → Download missing.',
    True, False);
  ProfilePage.Add('LITE - 8 GB graphics card (Qwen3.5 9B, ~36 GB download)');
  ProfilePage.Add('PRO - 16 GB graphics card (Qwen3.8 27B, ~52 GB download)');
  ProfilePage.Add('Do not download models now — I will install the profile later');
  ProfilePage.Add('I will use my own model or my API account');
  ProfilePage.SelectedValueIndex := OptLite;

  OwnModelPage := CreateInputFilePage(ProfilePage.ID,
    'Your own models', 'Choose the model files you want to use (all optional).',
    'The files are not copied, they are used where they are. You can add the ones you leave empty later from the app.');
  OwnModelPage.Add('Language model (.gguf):', 'GGUF language model|*.gguf|All files|*.*', '.gguf');
  OwnModelPage.Add('Image model — Krea 2 Turbo (.safetensors or .gguf):', 'Image model|*.safetensors;*.gguf|All files|*.*', '');
  OwnModelPage.Add('Video model — Wan 2.2 TI2V 5B (.safetensors):', 'Video model|*.safetensors|All files|*.*', '.safetensors');

  ApiPage := CreateInputOptionPage(OwnModelPage.ID,
    'API account', 'Connect your OpenAI, Claude, Gemini or DeepSeek account.',
    'The key is not asked for during setup. The app opens the API connections window on first launch: ' +
    '"Sign in to your account, get a key" opens the provider''s key page, "Verify and fetch models" ' +
    'tries the key and lists the models in your account. The key is stored encrypted, bound to your Windows account.',
    False, False);
  ApiPage.Add('Open the API connections window on first launch');
  ApiPage.Values[0] := False;

  SummaryPage := CreateOutputMsgMemoPage(wpInstalling,
    'Setup summary', 'Result of the steps and the smoke test',
    'A copy of the summary is in the logs\setup-summary.txt file in the install folder.', '');

  { Shown in place of Inno's Cancel button while installing: same position and size, a normal
    OnClick. The caption is fixed; CancelButton.Caption is not yet assigned at InitializeWizard. }
  CustomCancelButton := TNewButton.Create(WizardForm);
  CustomCancelButton.Parent := WizardForm;
  CustomCancelButton.Caption := 'Cancel';
  CustomCancelButton.Left := WizardForm.CancelButton.Left;
  CustomCancelButton.Top := WizardForm.CancelButton.Top;
  CustomCancelButton.Width := WizardForm.CancelButton.Width;
  CustomCancelButton.Height := WizardForm.CancelButton.Height;
  CustomCancelButton.Anchors := WizardForm.CancelButton.Anchors;
  CustomCancelButton.OnClick := @CustomCancelButtonClick;
  CustomCancelButton.Visible := False;
end;

{ Profile report: runs after the folder is chosen (free disk is measured on that drive). }
procedure RunProfileReport;
var
  ReportFile, RecommendFile, Params, Recommended: String;
  RecommendedRaw: AnsiString;
  Lines: TArrayOfString;
  Text: String;
  I, ResultCode: Integer;
begin
  if ReportedDir = WizardDirValue then Exit;
  ReportedDir := WizardDirValue;
  ExtractTemporaryFile('profile-report.ps1');
  ExtractTemporaryFile('profiles.json');
  ReportFile := ExpandConstant('{tmp}\profile-report.txt');
  RecommendFile := ExpandConstant('{tmp}\profile-recommendation.txt');
  Params := '-NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(ExpandConstant('{tmp}\profile-report.ps1')) +
    ' -Root ' + AddQuotes(WizardDirValue) +
    ' -Catalog ' + AddQuotes(ExpandConstant('{tmp}\profiles.json')) +
    ' -OutFile ' + AddQuotes(ReportFile) + ' -RecommendFile ' + AddQuotes(RecommendFile);
  ReportPage.RichEditViewer.Lines.Text := 'Measuring the hardware…';
  WizardForm.Refresh;
  if not Exec(PowerShellExe, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    ReportPage.RichEditViewer.Lines.Text := 'The profile report could not be created. You can choose the profile yourself.';
    Exit;
  end;
  Text := '';
  if LoadStringsFromFile(ReportFile, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      Text := Text + Lines[I] + #13#10;
  ReportPage.RichEditViewer.Lines.Text := Text;
  if LoadStringFromFile(RecommendFile, RecommendedRaw) then
  begin
    Recommended := Trim(String(RecommendedRaw));
    if (Recommended = 'flash') or (Recommended = 'max') then ProfilePage.SelectedValueIndex := OptQwen38
    else if Recommended = 'lite' then ProfilePage.SelectedValueIndex := OptLite
    else ProfilePage.SelectedValueIndex := OptOwn;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = ReportPage.ID then RunProfileReport;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = OwnModelPage.ID then
    Result := ProfilePage.SelectedValueIndex <> OptOwn
  else if PageID = ApiPage.ID then
    Result := ProfilePage.SelectedValueIndex < OptLater
  else if PageID = wpSelectTasks then
    { The smoke test and OCR only make sense for profiles whose models are downloaded. }
    Result := ProfilePage.SelectedValueIndex >= OptLater;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  I: Integer;
begin
  Result := True;
  if CurPageID = OwnModelPage.ID then
    for I := 0 to 2 do
      if (OwnModelPage.Values[I] <> '') and not FileExists(OwnModelPage.Values[I]) then
      begin
        MsgBox('File not found: ' + OwnModelPage.Values[I], mbError, MB_OK);
        Result := False;
        Exit;
      end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  Lines: String;
begin
  Lines := MemoDirInfo + NewLine + NewLine + 'Profile:' + NewLine + Space + ProfilePage.CheckListBox.ItemCaption[ProfilePage.SelectedValueIndex];
  if ProfilePage.SelectedValueIndex = OptOwn then
  begin
    if OwnModelPage.Values[0] <> '' then Lines := Lines + NewLine + Space + 'Language model: ' + OwnModelPage.Values[0];
    if OwnModelPage.Values[1] <> '' then Lines := Lines + NewLine + Space + 'Image model: ' + OwnModelPage.Values[1];
    if OwnModelPage.Values[2] <> '' then Lines := Lines + NewLine + Space + 'Video model: ' + OwnModelPage.Values[2];
  end;
  if (ProfilePage.SelectedValueIndex >= OptLater) and ApiPage.Values[0] then
    Lines := Lines + NewLine + Space + 'The API connections window will open on first launch';
  { The tasks page (smoke test, OCR) is shown and applied only for profiles whose models are
    downloaded; for the other options it is not listed in the summary either. }
  if (ProfilePage.SelectedValueIndex < OptLater) and (MemoTasksInfo <> '') then
    Lines := Lines + NewLine + NewLine + MemoTasksInfo;
  { The ready page does not wrap text; a long line was cut off on the right. Each sentence on its own line. }
  Lines := Lines + NewLine + NewLine + 'This setup requires an internet connection (it downloads ' +
    'PowerShell 7, llama.cpp, Python packages and ComfyUI).' +
    NewLine + 'Setup runs in the background; this window shows the progress.' +
    NewLine + 'Python, Git, 7-Zip and the VC++ Redistributable are installed with winget if missing.';
  if ProfilePage.SelectedValueIndex < OptLater then
    Lines := Lines + NewLine + 'Downloading the models can take 30-90 minutes depending on your connection speed.'
  else
    Lines := Lines + NewLine + 'Since no models are downloaded, setup takes about 10-15 minutes.';
  Result := Lines;
end;

function BuildSetupParams: String;
var
  P: String;
begin
  P := '-NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(ExpandConstant('{app}\setup-source\scripts\setup-run.ps1')) +
    ' -Root ' + AddQuotes(ExpandConstant('{app}')) +
    ' -Source ' + AddQuotes(ExpandConstant('{app}\setup-source')) +
    ' -Profile ' + SelectedProfile + ' -InstallPrereqs -FromWizard';
  if ProfilePage.SelectedValueIndex < OptLater then
  begin
    P := P + ' -DownloadModels';
    if WizardIsTaskSelected('smoketest') then P := P + ' -SmokeTest';
    if not WizardIsTaskSelected('media') then P := P + ' -SkipMedia';
    if not WizardIsTaskSelected('audio') then P := P + ' -SkipAudio';
    if WizardIsTaskSelected('ocr') then P := P + ' -Ocr';
  end;
  if ProfilePage.SelectedValueIndex = OptOwn then
  begin
    if OwnModelPage.Values[0] <> '' then P := P + ' -LlmPath ' + AddQuotes(OwnModelPage.Values[0]);
    if OwnModelPage.Values[1] <> '' then P := P + ' -ImageModelPath ' + AddQuotes(OwnModelPage.Values[1]);
    if OwnModelPage.Values[2] <> '' then P := P + ' -VideoModelPath ' + AddQuotes(OwnModelPage.Values[2]);
  end;
  if (ProfilePage.SelectedValueIndex >= OptLater) and ApiPage.Values[0] then P := P + ' -OpenApiSettings';
  Result := P;
end;

type
  { A flat (non-nested) match of the real Win32 MSG structure; PeekMessage/DispatchMessage
    expect exactly this, down to POINT's x/y fields. }
  TMsg = record
    hwnd: Longint;
    message: Cardinal;
    wParam: Longint;
    lParam: Longint;
    time: Cardinal;
    ptX: Longint;
    ptY: Longint;
  end;

function PeekMessageW(var lpMsg: TMsg; hWnd: Longint; wMsgFilterMin, wMsgFilterMax, wRemoveMsg: Cardinal): Boolean;
  external 'PeekMessageW@user32.dll stdcall';
function TranslateMessage(const lpMsg: TMsg): Boolean;
  external 'TranslateMessage@user32.dll stdcall';
function DispatchMessageW(const lpMsg: TMsg): Longint;
  external 'DispatchMessageW@user32.dll stdcall';

{ setup-run.ps1 is started with ewNoWait and the message queue is pumped here by hand while
  waiting. Exec(..., ewWaitUntilTerminated) stops the wizard's message loop, the window shows
  "Not Responding" and Cancel does not work. }
procedure PumpMessages;
var
  Msg: TMsg;
begin
  while PeekMessageW(Msg, 0, 0, 0, 1) do
  begin
    TranslateMessage(Msg);
    DispatchMessageW(Msg);
  end;
end;

{ Is the setup-run.ps1 process still running? Used so the wizard does not wait forever if the
  process ends without writing the done flag. Exec does not capture stdout, so the exit code of
  tasklist | find is read (0 if the PID was found). }
function ProcessIsRunning(const Pid: String): Boolean;
var
  ResultCode: Integer;
  Params: String;
begin
  Params := '/C tasklist /FI "PID eq ' + Pid + '" /NH | find "' + Pid + '" >nul';
  if not Exec(ExpandConstant('{cmd}'), Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    { If even tasklist couldn't be started, be cautious and assume it's still running -
      the loop can still exit via DoneFlag/InstallCancelled, so this won't hang forever. }
    Result := True;
    Exit;
  end;
  Result := (ResultCode = 0);
end;

{ For other ways of cancelling (closing via X / Alt+F4); does nothing outside InstallRunning.
  The Cancel click during installation is handled by CustomCancelButtonClick. The actual
  termination/rollback of a confirmed cancel happens after CurStepChanged's wait loop exits. }
procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if not InstallRunning then Exit;
  Confirm := False;
  Cancel := False;
  if InstallCancelled then Exit;
  if MsgBox('Cancel setup? Everything downloaded/installed so far ' +
    '(including any models) will be removed.' + #13#10#13#10 + 'Cancel setup?',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    InstallCancelled := True;
end;

{ Replaces Inno's Cancel button while installing (see CustomCancelButton). }
procedure CustomCancelButtonClick(Sender: TObject);
begin
  if not InstallRunning then Exit;
  if InstallCancelled then Exit;
  if MsgBox('Cancel setup? Everything downloaded/installed so far ' +
    '(including any models) will be removed.' + #13#10#13#10 + 'Cancel setup?',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    InstallCancelled := True;
end;

{ The setup script runs hidden, so progress is shown in the wizard. Reads the 3-line progress
  file written by setup-run.ps1 (step number / total / step name, see Set-InstallProgress) and
  updates FilenameLabel and the progress bar. If the file does not exist yet or is malformed it
  does nothing; the previous state stays on screen. }
procedure UpdateInstallProgress(const ProgressFile: String);
var
  PLines: TArrayOfString;
  StepIdx, StepTotal: Integer;
begin
  if not LoadStringsFromFile(ProgressFile, PLines) then Exit;
  if GetArrayLength(PLines) < 3 then Exit;
  StepIdx := StrToIntDef(PLines[0], 0);
  StepTotal := StrToIntDef(PLines[1], 0);
  if StepTotal <= 0 then Exit;
  WizardForm.FilenameLabel.Caption := PLines[2] + ' (' + PLines[0] + '/' + PLines[1] + ')';
  WizardForm.ProgressGauge.Style := npbstNormal;
  WizardForm.ProgressGauge.Max := StepTotal;
  { Step StepIdx is still running; the bar fills up to the completed steps and does not show
    100% early on the last step. }
  WizardForm.ProgressGauge.Position := StepIdx - 1;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  SummaryFile, DoneFlag, PidFile, ProgressFile, PidText, KillParams, RollbackScript, RollbackParams, SetupPid: String;
  PidAnsi: AnsiString;
  Lines: TArrayOfString;
  Text: String;
  I, KillResult, RollbackResult, Ticks: Integer;
  Started, ProcessExited: Boolean;
begin
  if CurStep <> ssPostInstall then Exit;
  WizardForm.StatusLabel.Caption := 'Installing MamidLabs…';
  WizardForm.FilenameLabel.Caption := 'This step can take a long time. You can click Cancel to stop and remove everything installed so far.';
  WizardForm.ProgressGauge.Style := npbstMarquee;

  DoneFlag := ExpandConstant('{app}\logs\setup-done.flag');
  PidFile := ExpandConstant('{app}\logs\setup.pid');
  ProgressFile := ExpandConstant('{app}\logs\setup-step.txt');
  DeleteFile(DoneFlag);
  DeleteFile(ProgressFile);

  InstallCancelled := False;
  InstallRunning := True;
  { The script runs hidden (SW_HIDE); its output goes to logs\setup.log. }
  Started := Exec(PowerShellExe, BuildSetupParams, ExpandConstant('{app}'), SW_HIDE, ewNoWait, SetupExitCode);
  if Started then
  begin
    { Inno's Cancel button does not work in this wait loop; CustomCancelButton is used instead. }
    WizardForm.CancelButton.Visible := False;
    CustomCancelButton.Visible := True;
    CustomCancelButton.Enabled := True;
    ProcessExited := False;
    Ticks := 0;
    repeat
      PumpMessages;
      Sleep(200);
      Ticks := Ticks + 1;
      { Check every ~2 s whether the process is still running (tasklist is not cheap). This keeps
        the wizard from hanging if the process ends without writing the done flag. }
      if (Ticks mod 10 = 0) and (not FileExists(DoneFlag)) and (not InstallCancelled) then
      begin
        if LoadStringFromFile(PidFile, PidAnsi) then SetupPid := Trim(String(PidAnsi)) else SetupPid := '';
        if (SetupPid <> '') and (not ProcessIsRunning(SetupPid)) then ProcessExited := True;
      end;
      { The progress file is read on every tick (200 ms). }
      UpdateInstallProgress(ProgressFile);
    until FileExists(DoneFlag) or InstallCancelled or ProcessExited;
    CustomCancelButton.Visible := False;
    WizardForm.CancelButton.Visible := True;
    { On a successful finish complete the bar (see UpdateInstallProgress). }
    if FileExists(DoneFlag) and (WizardForm.ProgressGauge.Max > 0) then
      WizardForm.ProgressGauge.Position := WizardForm.ProgressGauge.Max;
  end;
  InstallRunning := False;

  if (not Started) then
  begin
    SummaryPage.RichEditViewer.Lines.Text := 'The setup script could not be started.';
    Exit;
  end;

  if ProcessExited and not FileExists(DoneFlag) then
    MsgBox('The setup script stopped unexpectedly and could not write its completion flag.' + #13#10#13#10 +
      'Setup most likely completed. Details: ' + ExpandConstant('{app}\logs\setup.log'), mbError, MB_OK);

  { The DoneFlag condition is deliberately left out: if the user confirmed the cancel, the
    rollback runs and the wizard closes even if the script finished at that very moment. }
  if InstallCancelled then
  begin
    WizardForm.StatusLabel.Caption := 'Cancelling; removing everything installed so far…';
    WizardForm.FilenameLabel.Caption := 'This can take a few minutes.';
    WizardForm.Refresh;
    { setup-run.ps1 writes the PID file when it starts; /T also ends its child processes (winget,
      pip, model download). LoadStringFromFile expects an AnsiString second parameter. }
    if LoadStringFromFile(PidFile, PidAnsi) then PidText := String(PidAnsi) else PidText := '';
    if Trim(PidText) <> '' then
    begin
      KillParams := '/PID ' + Trim(PidText) + ' /T /F';
      Exec(ExpandConstant('{sys}\taskkill.exe'), KillParams, '', SW_HIDE, ewWaitUntilTerminated, KillResult);
    end;
    { The source copy is extracted in the [Files] step (ssInstall), so it always exists here. }
    RollbackScript := ExpandConstant('{app}\setup-source\scripts\uninstall-mamidlabs.ps1');
    if FileExists(RollbackScript) then
    begin
      RollbackParams := '-NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(RollbackScript) +
        ' -Root ' + AddQuotes(ExpandConstant('{app}')) + ' -Yes -FromWizard';
      Exec(PowerShellExe, RollbackParams, GetTempDir, SW_HIDE, ewWaitUntilTerminated, RollbackResult);
    end;
    MsgBox('Setup was cancelled. Everything installed so far has been removed.', mbInformation, MB_OK);
    { Abort is not enough here (see ExitProcess); the wizard closes directly. }
    ExitProcess(1);
  end;

  SummaryFile := ExpandConstant('{app}\logs\setup-summary.txt');
  Text := '';
  if LoadStringsFromFile(SummaryFile, Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      Text := Text + Lines[I] + #13#10
  else
    Text := 'The setup script could not write a summary. Details: ' + ExpandConstant('{app}\logs\setup.log');
  SummaryPage.RichEditViewer.Lines.Text := Text;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App, Script, Params: String;
  KeepUserData: Boolean;
  ResultCode: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  App := ExpandConstant('{app}');
  Script := App + '\scripts\uninstall-mamidlabs.ps1';
  { Default: a full cleanup as if it had never been installed. A silent uninstall also does a full cleanup. }
  KeepUserData := False;
  if not UninstallSilent then
    KeepUserData := MsgBox('MamidLabs will be removed completely: the program, models, caches, chats, ' +
      'generated files, settings and the prerequisites MamidLabs installed (Python, Git, 7-Zip, VC++).' + #13#10#13#10 +
      'Do you want to keep your chats, generated images/videos and settings?' + #13#10 +
      'Yes: they stay under ' + App + '.   No: everything is deleted.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  if FileExists(Script) then
  begin
    Params := '-NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(Script) + ' -Root ' + AddQuotes(App) + ' -Yes -FromWizard';
    if KeepUserData then Params := Params + ' -KeepUserData';
    { The working folder must not be the install root; otherwise the folder stays locked. }
    if not Exec(PowerShellExe, Params, GetTempDir, SW_SHOW, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      MsgBox('Some files could not be deleted. If a MamidLabs window is open, close it and delete the ' + App +
        ' folder manually.', mbInformation, MB_OK);
  end
  else if not KeepUserData then
  begin
    DelTree(App + '\models', True, True, True);
    DelTree(App + '\ComfyUI', True, True, True);
    DelTree(App + '\chats', True, True, True);
    DelTree(App + '\outputs', True, True, True);
    DelTree(App + '\settings', True, True, True);
  end;
end;

