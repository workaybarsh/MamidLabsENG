"""Action audit log (logs/audit.jsonl).

After every tool call a JSON line is written: job/tool id, source channel, approval
state, result and changed paths. Document/command content and API keys are never
written. The functions are extracted via AST and run in a real pwsh:
1) A tool that needs no approval (read_bound_file) -> approval=not_required, result=ok.
2) File write under full access (write_bound_file) -> approval=full_access, changedPath set.
3) A denied approval -> approval=asked_no, result=denied.
The lock object (Get-AuditLock) is a lazily created function, not a top-level
assignment, so it also works when only the functions are loaded.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_write_audit_log_function_exists_and_never_logs_full_content():
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "function Write-AuditLog" in inference
    assert "function Get-ToolAuditDetail" in inference
    # For write_bound_file/create_document relative_path/filename is passed,
    # the 'content' field (document/file CONTENT) is NEVER passed.
    detail_fn = inference.split("function Get-ToolAuditDetail")[1].split("function Wait-LocalModelStopped")[0]
    assert "'write_bound_file' { return [string]$arguments.relative_path }" in detail_fn
    assert "$arguments.content" not in detail_fn


def test_audit_lock_is_a_lazy_accessor_function_not_a_top_level_assignment():
    """Caught on a real machine: $script:auditLock was a top-level assignment;
    when only the functions were extracted with the AST and loaded (behavior tests,
    or when the whole file did not run for another reason) it stayed undefined,
    and the .WaitOne() call inside Write-AuditLog gave a null reference error."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "function Get-AuditLock" in inference
    assert "$lock = Get-AuditLock" in inference
    # The mutex creation line must be INSIDE the Get-AuditLock body, not outside
    # the function (top-level).
    lock_fn = inference.split("function Get-AuditLock")[1].split("function Write-AuditLog")[0]
    assert "[System.Threading.Mutex]::new($false, 'Local\\MamidLabsAuditLog')" in lock_fn


def test_tool_call_site_logs_both_success_and_error_paths():
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "Write-AuditLog $name $approval $auditResult $auditDetail" in inference
    assert "Write-AuditLog $name (if ($script:lastApprovalOutcome)" in inference


def test_denied_approval_is_recorded_as_denied_not_ok():
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "$auditResult = if ($approval -in @('asked_no', 'cancelled', 'timeout')) { 'denied' } else { 'ok' }" in inference


def test_write_bound_file_tracks_its_changed_path():
    """create_document already set $script:createdArtifact; write_bound_file
    did not - changedPath in the audit record was always empty."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    section = inference.split("if ($name -eq 'write_bound_file')")[1].split(
        "if ($name -eq 'run_command')"
    )[0]
    assert "$script:createdArtifact = $target" in section
