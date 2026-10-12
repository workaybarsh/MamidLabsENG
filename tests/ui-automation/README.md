# UI test helpers

Helpers used to drive MamidLabs' WinForms app and the Inno Setup wizard from PowerShell, without a
desktop control tool. The full UI tests are done with them.

| File | Content |
|---|---|
| `ui.ps1` | UI Automation: `Get-Window`, `Show-Tree`, `Find-El`, `Click-Name`, `Set-Text`, `Save-Shot`; `Click-Pane` / `Click-Hwnd` (BM_CLICK) for Inno buttons; `Set-FileDialogTarget` / `Dismiss-FileDialogError` for the file dialog |
| `win32.ps1` | Pure Win32: `Find-Top`, `Kids-Text`, `Shot-Hwnd`, `Click-Btn` (PostMessage, non-blocking); the `[FileDlg]` class (file-dialog control/P-Invoke helpers) |

Usage (Windows PowerShell 5.1):

```powershell
. .\tests\ui-automation\ui.ps1
. .\tests\ui-automation\win32.ps1
```

Screenshots are written under `%TEMP%\claude\ui\shots\`.

## Lessons learned

- **Inno Setup:** the buttons show up as `Pane` in UIA; use `Click-Pane` (BM_CLICK). Since the profile
  choice is an owner-drawn list, `WM_LBUTTONDOWN/UP` is sent to the position of the row.
- **Items that open a modal window** (menu item, button): UIA `Invoke` does not return until the
  window closes and can lock the app's UIA connection. For these items:
  - For buttons use `Click-Btn` (PostMessage).
  - In a context menu press the button first, then send `SendKeys('{DOWN}{DOWN}{ENTER}')`.
- **Hidden tabs:** WinForms creates the handles of the buttons on a hidden tab (Model Control) when
  the tab is first shown. Open View → Model control first.
- **Attaching an image:** put it on the clipboard with `Set-Clipboard -Path <file>`, focus the prompt
  box and send `SendKeys('^v')`.
- **Approval window:** its title is `Permission required — <tool>`, its buttons `Yes, allow` / `No`.
- **Encoding:** if the scripts that call these helpers contain non-ASCII text they must be saved as
  UTF-8 **with BOM**. Windows PowerShell 5.1 reads a BOM-less file as ANSI and such text gets garbled.
- **Settings:** back up the `<root>\settings\` folder before a test. Restore it after the app is
  closed; a file written while the app is open gets overwritten on exit.
- **For WinForms `ComboBoxStyle.DropDownList`, UIA `ValuePattern.SetValue` and
  `SelectionItemPattern.Select()` are not reliable:** they may change the text on screen but not
  trigger the real `SelectedIndexChanged` event (the UI and the real state drift apart — in the 16 GB
  PC test the profile box showed "MAX" but the model was still FLASH). Fix: simulate a real mouse
  click — open the box first with `SetCursorPos` + `mouse_event` (`LEFTDOWN`/`LEFTUP`) (click its
  edge), read the `BoundingRectangle` of the `ListItem`s in the list from UIA, then click that
  coordinate.
- **If a UIA `Invoke` call on a button times out because it opens a modal window** (the pitfall
  above), find the button's HWND and retry with `Click-Btn` in `win32.ps1` (PostMessage,
  non-blocking) — the window may already be open on the server side, only the pending client-side call
  timed out.
- **Never write a full path into the file picker → invalid-character error; go to the folder first,
  then write only the file name.** Writing the full path into the file-name box (`GetDlgItem` 1148 →
  `ComboBoxEx32`→`ComboBox`→`Edit`) with `SetWindowText` makes Windows 11 show
  **"A file name cannot contain any of the following characters: \\ / : * ? \" < > |"** (Windows treats
  it as a rename). Fix with `Set-FileDialogTarget`: (1) point the dialog at the target folder —
  preferably the address bar's editable `Edit` (`AutomationId` **41477**, 1001 on some builds; in UIA
  `AutomationId` "1001" is the address Toolbar, named "Address: …"), otherwise write the **folder**
  path into the file-name box + `Enter`; (2) clear the file-name box and write **only the file name**
  (`Split-Path -Leaf`); (3) accept with `Open`/`Save` (control id **1**); (4) if an error popup
  (#32770, "cannot contain") appears, click OK to close it and retry with file-name-only. Give focus
  with `Thread.AttachThreadInput` + `SetFocus` before writing; verify navigation via the address-bar
  text. Convert 8.3 short paths to long with `GetLongPathName`; the dialog may not navigate a short
  path.
