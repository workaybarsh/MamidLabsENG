"""API provider table, DeepSeek conversion and key validation.

The real PowerShell functions are run; the network request is only imitated by
replacing Invoke-RestMethod with a fake function.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_ps(tmp_path, body):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    prelude = (
        "$ErrorActionPreference = 'Stop'\n"
        # Output is read as UTF-8; the console code page depends on the calling shell.
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        f". '{ROOT / 'agent' / 'api-providers.ps1'}'\n"
        "function Assert($condition, [string]$message) { if (!$condition) { throw $message } }\n"
    )
    script = tmp_path / "test.ps1"
    script.write_text(prelude + body, encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_catalog_lists_every_chat_provider_with_endpoint_and_key_page(tmp_path):
    out = run_ps(tmp_path, r"""
$rows = @(Get-ChatProviders | ForEach-Object { @{ id=$_.id; endpoint=$_.endpoint; env=$_.env; keyPage=$_.keyPage } })
$rows | ConvertTo-Json -Compress
""")
    rows = {row["id"]: row for row in json.loads(out)}
    assert set(rows) == {"openai", "claude", "gemini", "deepseek", "custom"}
    assert rows["deepseek"]["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert rows["deepseek"]["env"] == "DEEPSEEK_API_KEY"
    # 'custom' (the user's own OpenAI-compatible endpoint) has no fixed endpoint/key
    # page; the base address is read from the user's own settings.
    for provider_id, row in rows.items():
        if provider_id == "custom":
            assert row["endpoint"] == "" and row["keyPage"] == ""
        else:
            assert row["keyPage"].startswith("https://")


def test_custom_provider_endpoint_requires_configured_base_url(tmp_path):
    out = run_ps(tmp_path, r"""
try { Get-ProviderEndpoint 'custom'; throw 'expected an error' }
catch { Assert ($_.Exception.Message -like '*base address*') $_.Exception.Message }
""")


def test_custom_provider_endpoint_reads_saved_base_url_and_strips_trailing_slash(tmp_path):
    out = run_ps(tmp_path, r"""
$script:apiSettings = @{ providers = @{ custom = @{ baseUrl = 'http://127.0.0.1:1234/v1/' } } }
Get-ProviderEndpoint 'custom'
""")
    assert out.strip() == "http://127.0.0.1:1234/v1/chat/completions"


def test_custom_provider_endpoint_override_ignores_unsaved_settings(tmp_path):
    """'Verify' in the API settings window tries the on-screen address before it is saved."""
    out = run_ps(tmp_path, r"""
$script:apiSettings = @{ providers = @{} }
Get-ProviderEndpoint 'custom' 'http://192.168.1.50:8000/v1'
""")
    assert out.strip() == "http://192.168.1.50:8000/v1/chat/completions"


def test_custom_provider_does_not_require_an_api_key(tmp_path):
    """Most local/own servers (Ollama, LM Studio, vLLM etc.) do not require authentication."""
    out = run_ps(tmp_path, r"""
function Invoke-RestMethod { param($Uri, $Headers, $TimeoutSec, $ErrorAction)
    Assert (!$Headers.ContainsKey('Authorization')) 'must not send Authorization with an empty key'
    return [pscustomobject]@{ data = @([pscustomobject]@{ id = 'llama-3-local' }) }
}
(Get-ProviderModels 'custom' '' 'http://127.0.0.1:11434/v1') -join ','
""")
    assert out.strip() == "llama-3-local"


def test_unknown_provider_falls_back_to_local_server(tmp_path):
    out = run_ps(tmp_path, "Get-ProviderEndpoint 'local'")
    assert out.strip() == "http://127.0.0.1:8080/v1/chat/completions"


def test_deepseek_request_drops_image_parts_and_uses_max_tokens(tmp_path):
    out = run_ps(tmp_path, r"""
$body = @{ model='deepseek-chat'; max_tokens=512; messages=@(
    @{ role='system'; content='rule' },
    @{ role='user'; content=@(
        @{ type='text'; text='What is in this photo?' },
        @{ type='image_url'; image_url=@{ url='data:image/png;base64,AAAA' } }
    ) }
) }
$request = Convert-ProviderRequest $body 'deepseek'
$request | ConvertTo-Json -Depth 8 -Compress
""")
    request = json.loads(out)
    assert request["max_tokens"] == 512
    assert "max_completion_tokens" not in request
    user = request["messages"][1]
    assert isinstance(user["content"], str)
    assert "What is in this photo?" in user["content"]
    assert "image" in user["content"]
    assert "base64" not in json.dumps(request)


def test_vision_providers_keep_image_parts(tmp_path):
    out = run_ps(tmp_path, r"""
$body = @{ model='gpt-x'; max_tokens=10; messages=@(@{ role='user'; content=@(
    @{ type='image_url'; image_url=@{ url='data:image/png;base64,AAAA' } }) }) }
(Convert-ProviderRequest $body 'openai') | ConvertTo-Json -Depth 8 -Compress
""")
    assert "base64,AAAA" in out


def test_custom_provider_is_openai_compatible_passthrough(tmp_path):
    """'custom' speaks the OpenAI Chat Completions format; no Claude-specific conversion
    should apply, and images (vision=true) must be kept."""
    out = run_ps(tmp_path, r"""
$body = @{ model='local-model'; max_tokens=10; messages=@(@{ role='user'; content=@(
    @{ type='text'; text='what do you see' },
    @{ type='image_url'; image_url=@{ url='data:image/png;base64,AAAA' } }) }) }
(Convert-ProviderRequest $body 'custom') | ConvertTo-Json -Depth 8 -Compress
""")
    request = json.loads(out)
    assert request["max_tokens"] == 10
    assert "base64,AAAA" in out
    user = request["messages"][0]
    assert isinstance(user["content"], list)


def test_key_validation_lists_models_and_filters_non_chat_entries(tmp_path):
    out = run_ps(tmp_path, r"""
function Invoke-RestMethod { param($Uri, $Headers, $TimeoutSec, $ErrorAction)
    $script:seen = @{ uri = $Uri; headers = $Headers }
    if ($Uri -like '*openai.com*') {
        return [pscustomobject]@{ data = @(
            [pscustomobject]@{ id = 'gpt-5' }, [pscustomobject]@{ id = 'text-embedding-3-large' },
            [pscustomobject]@{ id = 'gpt-4o-audio-preview' }, [pscustomobject]@{ id = 'o4-mini' }) }
    }
    return [pscustomobject]@{ data = @([pscustomobject]@{ id = 'deepseek-chat' }, [pscustomobject]@{ id = 'deepseek-reasoner' }) }
}
$openai = @(Get-ProviderModels 'openai' 'sk-test')
$deepseek = @(Get-ProviderModels 'deepseek' 'sk-ds')
Assert ($script:seen.headers.Authorization -eq 'Bearer sk-ds') 'Bearer header missing'
@{ openai = $openai; deepseek = $deepseek } | ConvertTo-Json -Compress
""")
    models = json.loads(out)
    assert models["openai"] == ["gpt-5", "o4-mini"]
    assert models["deepseek"] == ["deepseek-chat", "deepseek-reasoner"]


def test_claude_key_validation_uses_anthropic_headers(tmp_path):
    out = run_ps(tmp_path, r"""
function Invoke-RestMethod { param($Uri, $Headers, $TimeoutSec, $ErrorAction)
    Assert ($Headers['x-api-key'] -eq 'sk-ant') 'x-api-key missing'
    Assert ($Headers['anthropic-version'] -eq '2023-06-01') 'anthropic-version missing'
    Assert (!$Headers.ContainsKey('Authorization')) 'Bearer must not be sent to Anthropic'
    return [pscustomobject]@{ data = @([pscustomobject]@{ id = 'claude-opus-5' }) }
}
(Get-ProviderModels 'claude' 'sk-ant') -join ','
""")
    assert out.strip() == "claude-opus-5"


def test_empty_key_is_rejected_before_any_request(tmp_path):
    run_ps(tmp_path, r"""
function Invoke-RestMethod { throw 'no request should have been sent' }
try { Get-ProviderModels 'deepseek' ''; throw 'an error was expected' }
catch { Assert ($_.Exception.Message -like '*key*') $_.Exception.Message }
""")


def test_desktop_and_registry_use_the_shared_provider_list():
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    registry = (ROOT / "agent/model-registry.ps1").read_text(encoding="utf-8-sig")
    settings = (ROOT / "agent/api-settings.ps1").read_text(encoding="utf-8-sig")
    assert "@('openai', 'claude', 'gemini')" not in desktop
    assert "@('openai','claude','gemini')" not in registry
    assert "Get-ChatProviderIds" in registry
    assert "Get-ProviderModels" in settings
    assert "keyPage" in settings


CLAUDE_TOOL_TURN = r"""
# Get-PlainText is defined in mamidlabs-inference.ps1; plain text is enough here.
function Get-PlainText($content) { [string]$content }
$state = @{}
$events = @(
    '{"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}',
    '{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":""}}',
    '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"I should check the weather."}}',
    '{"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"SIG-1"}}',
    '{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}',
    '{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Searching."}}',
    '{"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"toolu_1","name":"search_web","input":{}}}',
    '{"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\"query\":"}}',
    '{"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\"Zürich weather\"}"}}',
    '{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":40}}',
    '{"type":"message_stop"}'
)
foreach ($line in $events) { $null = Convert-ProviderChunk ($line | ConvertFrom-Json) 'claude' $state }
$body = @{ model='claude-opus-5-5'; max_tokens=4096; tool_choice=@{ type='function'; function=@{ name='search_web' } }
    tools=@(@{ type='function'; function=@{ name='search_web'; description='search'; parameters=@{ type='object' } } })
    messages=@(
        @{ role='system'; content='rule' },
        @{ role='user'; content='Zürich weather?' },
        @{ role='assistant'; content='Searching.'; tool_calls=@(@{ id='toolu_1'; type='function'; function=@{ name='search_web'; arguments='{"query":"Zürich weather"}' } }) },
        @{ role='tool'; tool_call_id='toolu_1'; content='22 degrees' }
    ) }
"""


def test_claude_request_omits_disabled_thinking_and_forced_tool_choice(tmp_path):
    out = run_ps(tmp_path, CLAUDE_TOOL_TURN + "(Convert-ProviderRequest $body 'claude') | ConvertTo-Json -Depth 12 -Compress")
    request = json.loads(out)
    # Opus 5.5 / Fable 5.1 reject 'disabled' and a forced tool_choice with 400.
    assert "thinking" not in request
    assert request["tool_choice"] == {"type": "auto"}
    # max_tokens limits thinking + answer together; the local profile's output cap (8192) is not enough.
    assert request["max_tokens"] >= 16000


def test_claude_tool_loop_replays_signed_thinking_blocks_in_order(tmp_path):
    out = run_ps(tmp_path, CLAUDE_TOOL_TURN + "(Convert-ProviderRequest $body 'claude') | ConvertTo-Json -Depth 12 -Compress")
    request = json.loads(out)
    assistant = request["messages"][1]
    assert assistant["role"] == "assistant"
    assert [b["type"] for b in assistant["content"]] == ["thinking", "text", "tool_use"]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "I should check the weather.", "signature": "SIG-1"}
    assert assistant["content"][2]["input"] == {"query": "Zürich weather"}
    assert request["messages"][2]["content"][0]["type"] == "tool_result"


def test_claude_replay_does_not_leak_to_other_providers_or_unknown_turns(tmp_path):
    out = run_ps(tmp_path, CLAUDE_TOOL_TURN + r"""
$other = (Convert-ProviderRequest $body 'openai') | ConvertTo-Json -Depth 12 -Compress
$body.messages[2].tool_calls[0].id = 'toolu_old'
$body.messages[3].tool_call_id = 'toolu_old'
$old = (Convert-ProviderRequest $body 'claude') | ConvertTo-Json -Depth 12 -Compress
@{ other = $other; old = $old } | ConvertTo-Json -Compress
""")
    data = json.loads(out)
    assert "SIG-1" not in data["other"]
    old = json.loads(data["old"])
    # A tool turn left over from earlier jobs (with no record) goes in the old form, without thinking.
    assert [b["type"] for b in old["messages"][1]["content"]] == ["text", "tool_use"]
