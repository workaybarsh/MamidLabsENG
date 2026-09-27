# UI test helpers

Helpers used to drive MamidLabs' WinForms app and the Inno Setup wizard from PowerShell, without a
desktop control tool. The full UI tests are done with them.

| File | Content |
|---|---|
| `ui.ps1` | UI Automation: `Get-Window`, `Show-Tree`, `Find-El`, `Click-Name`, `Set-Text`, `Save-Shot`; `Click-Pane` / `Click-Hwnd` (BM_CLICK) for Inno buttons |
| `win32.ps1` | Pure Win32: `Find-Top`, `Kids-Text`, `Shot-Hwnd`, `Click-Btn` (PostMessage, non-blocking) |

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
