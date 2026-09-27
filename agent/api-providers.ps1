# MamidLabs provider adapters. This file contains no credentials.

# Chat providers in one table: the UI lists, key environment variables, the key page opened
# by the "Connect account" button and the model list address used for key validation are
# read from here. To add a new provider it is enough to add a row to this table; the ones
# that are not OpenAI-compatible (claude) are converted separately.
function Get-ChatProviders {
    return @(
        @{ id = 'openai';   label = 'OpenAI';   env = 'OPENAI_API_KEY'
           endpoint = 'https://api.openai.com/v1/chat/completions'
           models = 'https://api.openai.com/v1/models'
           keyPage = 'https://platform.openai.com/api-keys'; vision = $true }
        @{ id = 'claude';   label = 'Claude';   env = 'ANTHROPIC_API_KEY'
           endpoint = 'https://api.anthropic.com/v1/messages'
           models = 'https://api.anthropic.com/v1/models?limit=100'
           keyPage = 'https://console.anthropic.com/settings/keys'; vision = $true }
        @{ id = 'gemini';   label = 'Gemini';   env = 'GEMINI_API_KEY'
           endpoint = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
           models = 'https://generativelanguage.googleapis.com/v1beta/openai/models'
           keyPage = 'https://aistudio.google.com/apikey'; vision = $true }
        # DeepSeek offers an OpenAI-compatible endpoint; its chat models do not accept image input.
        @{ id = 'deepseek'; label = 'DeepSeek'; env = 'DEEPSEEK_API_KEY'
           endpoint = 'https://api.deepseek.com/chat/completions'
           models = 'https://api.deepseek.com/models'
           keyPage = 'https://platform.deepseek.com/api_keys'; vision = $false }
        # Any OpenAI Chat Completions-compatible endpoint (a local server - Ollama, LM Studio,
        # vLLM - or another provider). The base address is entered in the API connections
        # window; unlike the others it has no fixed endpoint/models/keyPage, and the key is
        # optional (most local servers do not check one).
        @{ id = 'custom'; label = 'Custom (OpenAI-compatible)'; env = 'CUSTOM_API_KEY'
           endpoint = ''; models = ''; keyPage = ''; vision = $true; needsBaseUrl = $true }
    )
}

function Get-ChatProviderIds { return @(Get-ChatProviders | ForEach-Object { $_.id }) }

function Get-ChatProvider([string]$provider) {
    return @(Get-ChatProviders | Where-Object { $_.id -eq $provider })[0]
}

# The custom provider's base address, as saved in settings\api.json (providers.custom.baseUrl).
function Get-CustomBaseUrl {
    $saved = $null
    if ($script:apiSettings -and $script:apiSettings.providers -and $script:apiSettings.providers['custom']) {
        $saved = [string]$script:apiSettings.providers['custom'].baseUrl
    }
    return ([string]$saved).TrimEnd('/')
}

function Get-ProviderEndpoint([string]$provider, [string]$baseUrlOverride = '') {
    if ($provider -eq 'custom') {
        $base = if ($baseUrlOverride) { $baseUrlOverride.TrimEnd('/') } else { Get-CustomBaseUrl }
        if (!$base) { throw 'For the custom API, first enter a base address in the API connections window.' }
        return "$base/chat/completions"
    }
    $entry = Get-ChatProvider $provider
    if ($entry) { return [string]$entry.endpoint }
    return 'http://127.0.0.1:8080/v1/chat/completions'
}

# Validates the key against the provider's model list endpoint and returns the chat models
# the account can access. The key is written only into the request header. For the custom
# provider the key is optional (most local servers do not check one).
function Get-ProviderModels([string]$provider, [string]$key, [string]$baseUrlOverride = '') {
    $entry = Get-ChatProvider $provider
    if (!$entry) { throw "Unknown provider: $provider" }
    if (!$key -and $provider -ne 'custom') { throw 'Enter the API key first.' }
    $modelsUrl = if ($provider -eq 'custom') {
        $base = if ($baseUrlOverride) { $baseUrlOverride.TrimEnd('/') } else { Get-CustomBaseUrl }
        if (!$base) { throw 'First enter a base address.' }
        "$base/models"
    } else { [string]$entry.models }
    $headers = if (!$key) { @{} }
               elseif ($provider -eq 'claude') { @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01' } }
               else { @{ Authorization = "Bearer $key" } }
    try {
        $response = Invoke-RestMethod -Uri $modelsUrl -Headers $headers -TimeoutSec 20 -ErrorAction Stop
    } catch {
        $status = try { [int]$_.Exception.Response.StatusCode } catch { 0 }
        if ($status -in @(401, 403)) { throw "$($entry.label) rejected the key ($status). Check the key." }
        throw "Could not connect to $($entry.label): $($_.Exception.Message)"
    }
    $ids = @($response.data | ForEach-Object { ([string]$_.id) -replace '^models/', '' } | Where-Object { $_ })
    if ($provider -eq 'openai') {
        # The OpenAI list also contains embedding, audio and image models; the non-chat ones are filtered out.
        $ids = @($ids | Where-Object { $_ -match '^(gpt|o\d|chatgpt)' -and $_ -notmatch '(?i)audio|realtime|transcribe|tts|image|search|embedding' })
    } elseif ($provider -eq 'gemini') {
        $ids = @($ids | Where-Object { $_ -match '^gemini' -and $_ -notmatch '(?i)embedding|image|tts|live' })
    }
    return @($ids | Sort-Object -Unique)
}

# Providers that do not accept images (DeepSeek) reject the request with 400 when they see an
# image_url part. The parts are reduced to text; a short note is left in place of the image so
# the model knows the user attached an image.
function ConvertTo-TextOnlyMessages($messages) {
    foreach ($message in @($messages)) {
        if ($message.content -is [string] -or $null -eq $message.content) { $message; continue }
        $texts = [Collections.Generic.List[string]]::new()
        foreach ($part in @($message.content)) {
            if ($part.type -eq 'text') { $texts.Add([string]$part.text) }
            elseif ($part.type -eq 'image_url') { $texts.Add('[The user attached an image; this provider cannot read images. Use the OCR result if needed.]') }
        }
        $copy = @{}
        if ($message -is [Collections.IDictionary]) { foreach ($name in $message.Keys) { $copy[$name] = $message[$name] } }
        else { foreach ($property in $message.PSObject.Properties) { $copy[$property.Name] = $property.Value } }
        $copy.content = $texts -join "`n"
        $copy
    }
}

function Convert-ProviderRequest([hashtable]$body, [string]$provider) {
    if (!$provider -or $provider -eq 'local') { return $body }
    $request = @{model=$body.model; stream=$true}
    if ($provider -ne 'claude') {
        $request.messages = $body.messages
        $entry = Get-ChatProvider $provider
        if ($entry -and !$entry.vision) { $request.messages = @(ConvertTo-TextOnlyMessages $body.messages) }
        $request.stream_options = @{include_usage=$true}
        if ($provider -eq 'openai') { $request.max_completion_tokens=$body.max_tokens }
        else { $request.max_tokens=$body.max_tokens }
        if ($body.tools) { $request.tools=$body.tools }
        if ($body.tool_choice) { $request.tool_choice=$body.tool_choice }
        # Omit sampler/local-only options: providers/models have different contracts.
        return $request
    }
    # thinking is not sent: Opus 5.5 and Fable 5/5.1 reject the 'disabled' value with 400,
    # and Opus 5 rejects it at high effort. Without the field newer models think adaptively,
    # older ones run without thinking. Since max_tokens is the combined limit of thinking + answer,
    # FLASH's 4096 cuts the answer short on newer models; the lower bound is 16000.
    $request.max_tokens = [math]::Max([int]$body.max_tokens, 16000)
    $system = [Collections.Generic.List[string]]::new()
    $messages = [Collections.Generic.List[object]]::new()
    foreach ($message in $body.messages) {
        if ($message.role -in @('system','developer')) {
            $system.Add((Get-PlainText $message.content)); continue
        }
        $blocks = [Collections.Generic.List[object]]::new()
        $role = [string]$message.role
        if ($role -eq 'tool') {
            $role = 'user'
            $blocks.Add(@{type='tool_result';tool_use_id=[string]$message.tool_call_id;content=[string]$message.content})
        } else {
            if ($message.content -is [string]) {
                if ($message.content) { $blocks.Add(@{type='text';text=[string]$message.content}) }
            } else {
                foreach ($part in @($message.content)) {
                    if ($part.type -eq 'text') { $blocks.Add(@{type='text';text=[string]$part.text}) }
                    elseif ($part.type -eq 'image_url') {
                        $url = [string]$part.image_url.url
                        if ($url -match '^data:([^;]+);base64,(.+)$') {
                            $blocks.Add(@{type='image';source=@{type='base64';media_type=$Matches[1];data=$Matches[2]}})
                        } else { $blocks.Add(@{type='image';source=@{type='url';url=$url}}) }
                    }
                }
            }
            foreach ($tc in @($message.tool_calls)) {
                if (!$tc) { continue }
                $toolArgs = $tc.function.arguments | ConvertFrom-Json -AsHashtable
                $blocks.Add(@{type='tool_use';id=[string]$tc.id;name=[string]$tc.function.name;input=$toolArgs})
            }
            # With thinking on, Claude's signed thinking blocks must be sent back in the same order
            # in the tool loop; if a response was recorded in this job, use it as is.
            $firstTool = @($message.tool_calls | Where-Object { $_ })[0]
            if ($role -eq 'assistant' -and $firstTool -and $script:ClaudeReplay -and $script:ClaudeReplay.ContainsKey([string]$firstTool.id)) {
                $blocks = [Collections.Generic.List[object]]::new()
                $blocks.AddRange([object[]]$script:ClaudeReplay[[string]$firstTool.id])
            }
        }
        if ($blocks.Count -eq 0) { continue }
        if ($messages.Count -gt 0 -and $messages[-1].role -eq $role) {
            $messages[-1].content = @($messages[-1].content) + @($blocks.ToArray())
        } else { $messages.Add(@{role=$role;content=@($blocks.ToArray())}) }
    }
    $request.messages = @($messages.ToArray())
    if ($system.Count) { $request.system = $system -join "`n`n" }
    if ($body.tools) {
        $request.tools = @($body.tools | ForEach-Object { @{name=$_.function.name;description=$_.function.description;input_schema=$_.function.parameters} })
    }
    # A forced tool choice (any/tool) is rejected while thinking is on; Opus 5.5 and Fable 5.1
    # return 400 in every case. Only 'auto' goes to Claude, the prompt steers the tool.
    if ($body.tool_choice -and $request.tools) { $request.tool_choice = @{type='auto'} }
    return $request
}

# Collects the content blocks of the Claude stream (thinking + signature, text, tool_use) in order.
# If the response ends with a tool call the blocks are stored under the first tool_use id; the
# next turn of the same request sends them back unchanged via Convert-ProviderRequest. The record
# is kept only in memory: it does not go to the chat file or to other providers.
function Add-ClaudeReplayChunk($chunk, [hashtable]$state) {
    if (!$state.blocks) { $state.blocks = @{} }
    $index = [int]$chunk.index
    switch ([string]$chunk.type) {
        'content_block_start' {
            $b = $chunk.content_block
            switch ([string]$b.type) {
                'thinking' { $state.blocks[$index] = @{type='thinking';thinking=[string]$b.thinking;signature=[string]$b.signature} }
                'redacted_thinking' { $state.blocks[$index] = @{type='redacted_thinking';data=[string]$b.data} }
                'text' { $state.blocks[$index] = @{type='text';text=[string]$b.text} }
                'tool_use' { $state.blocks[$index] = @{type='tool_use';id=[string]$b.id;name=[string]$b.name;json=''} }
            }
        }
        'content_block_delta' {
            $block = $state.blocks[$index]
            if (!$block) { return }
            switch ([string]$chunk.delta.type) {
                'thinking_delta' { $block.thinking += [string]$chunk.delta.thinking }
                'signature_delta' { $block.signature = [string]$chunk.delta.signature }
                'text_delta' { $block.text += [string]$chunk.delta.text }
                'input_json_delta' { $block.json += [string]$chunk.delta.partial_json }
            }
        }
        'message_stop' {
            $blocks = @($state.blocks.GetEnumerator() | Sort-Object Key | ForEach-Object { $_.Value })
            $tools = @($blocks | Where-Object { $_.type -eq 'tool_use' })
            if (!$tools.Count -or !@($blocks | Where-Object { $_.type -in @('thinking', 'redacted_thinking') }).Count) { return }
            $content = [Collections.Generic.List[object]]::new()
            foreach ($block in $blocks) {
                if ($block.type -eq 'tool_use') {
                    $toolInput = @{}
                    if ($block.json) {
                        try { $toolInput = $block.json | ConvertFrom-Json -AsHashtable -ErrorAction Stop } catch { return }
                    }
                    $content.Add(@{type='tool_use';id=$block.id;name=$block.name;input=$toolInput})
                } elseif ($block.type -ne 'text' -or $block.text) { $content.Add($block) }
            }
            if (!$script:ClaudeReplay) { $script:ClaudeReplay = @{} }
            $script:ClaudeReplay[[string]$tools[0].id] = $content.ToArray()
        }
    }
}

function Convert-ProviderChunk($chunk, [string]$provider, [hashtable]$state) {
    if ($provider -ne 'claude') { return $chunk }
    Add-ClaudeReplayChunk $chunk $state
    $normalized = @{choices=@()}
    switch ([string]$chunk.type) {
        'error' { return @{error=$chunk.error} }
        'message_start' {
            $u = $chunk.message.usage
            $state.prompt = [int]$u.input_tokens + [int]$u.cache_read_input_tokens + [int]$u.cache_creation_input_tokens
            $state.completion = [int]$u.output_tokens
        }
        'content_block_start' {
            if ($chunk.content_block.type -eq 'tool_use') {
                $b = $chunk.content_block
                $normalized.choices = @(@{delta=@{tool_calls=@(@{index=[int]$chunk.index;id=$b.id;function=@{name=$b.name;arguments=''}})}})
            }
        }
        'content_block_delta' {
            $delta = @{}
            switch ([string]$chunk.delta.type) {
                'text_delta' { $delta.content=[string]$chunk.delta.text }
                'thinking_delta' { $delta.reasoning_content=[string]$chunk.delta.thinking }
                'input_json_delta' { $delta.tool_calls=@(@{index=[int]$chunk.index;function=@{arguments=[string]$chunk.delta.partial_json}}) }
            }
            $normalized.choices=@(@{delta=$delta})
        }
        'message_delta' {
            $state.completion = [int]$chunk.usage.output_tokens
            $reason = switch ($chunk.delta.stop_reason) { 'max_tokens' {'length'} 'tool_use' {'tool_calls'} default {'stop'} }
            $normalized.choices=@(@{delta=@{};finish_reason=$reason})
        }
        'message_stop' { $normalized.providerDone=$true }
    }
    if ($chunk.type -in @('message_start','message_delta','message_stop')) {
        $normalized.usage=@{prompt_tokens=[int]$state.prompt;completion_tokens=[int]$state.completion}
    }
    return $normalized
}
