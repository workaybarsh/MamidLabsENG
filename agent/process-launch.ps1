# Starts long-lived child processes (llama-server, ComfyUI) without inheriting the caller's
# std handles.
#
# Start-Process -RedirectStandardOutput calls CreateProcess with bInheritHandles=TRUE:
# the child receives all of the caller's inheritable handles, including the script's own
# stdout pipe. The pipe does not close while the child lives; so "manage-qwen.ps1 |
# Out-String", redirecting smoke-test.ps1 output to a file or Start-Process -Wait
# never finished (seen on a real machine). Fix: clear the inherit flag of the std handles
# at launch time and then restore it.

function Start-DetachedProcess([string]$FilePath, [string[]]$ArgumentList, [string]$WorkingDirectory,
                               [string]$StdOutPath, [string]$StdErrPath) {
    if (-not ('MamidLabs.StdHandles' -as [type])) {
        Add-Type -Namespace MamidLabs -Name StdHandles -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int id);
[DllImport("kernel32.dll")] public static extern bool GetHandleInformation(IntPtr h, out int flags);
[DllImport("kernel32.dll")] public static extern bool SetHandleInformation(IntPtr h, int mask, int flags);
'@
    }
    $handleFlagInherit = 1
    $restore = [Collections.Generic.List[IntPtr]]::new()
    # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
    foreach ($id in @(-10, -11, -12)) {
        $handle = [MamidLabs.StdHandles]::GetStdHandle($id)
        if ($handle -eq [IntPtr]::Zero -or $handle -eq [IntPtr]::new(-1)) { continue }
        $flags = 0
        if ([MamidLabs.StdHandles]::GetHandleInformation($handle, [ref]$flags) -and ($flags -band $handleFlagInherit) -and
            [MamidLabs.StdHandles]::SetHandleInformation($handle, $handleFlagInherit, 0)) {
            $restore.Add($handle)
        }
    }
    try {
        return Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory `
            -RedirectStandardOutput $StdOutPath -RedirectStandardError $StdErrPath -WindowStyle Hidden -PassThru
    } finally {
        foreach ($handle in $restore) { [MamidLabs.StdHandles]::SetHandleInformation($handle, $handleFlagInherit, $handleFlagInherit) | Out-Null }
    }
}
