Option Explicit
Dim shell, fso, pwsh, app, command, root
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Derive the install root from this file's own location: this file always lives in the
' root folder, so the app runs from whichever folder it was installed to.
root = fso.GetParentFolderName(WScript.ScriptFullName)

' The PowerShell 7 bundled with the installation; if missing, pwsh on PATH is tried.
pwsh = root & "\runtime\pwsh\pwsh.exe"
If Not fso.FileExists(pwsh) Then
  pwsh = "pwsh.exe"
End If

app = root & "\mamidlabs-desktop.ps1"
command = """" & pwsh & """ -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File """ & app & """"
shell.Run command, 0, False
