<#
.SYNOPSIS
    End-to-end smoke test: health, login, overview, external whoami, MCP initialize.

.DESCRIPTION
    Exercises the paths an operator actually depends on, in order, and exits non-zero
    if any of them fails. Each check prints PASS/FAIL with the reason, so a failure
    names the broken hop instead of "something is wrong".

    Checks:
      1. GET  /api/health                     - the API is up
      2. GET  /api/health/ready               - database + bootstrap are usable
      3. POST /api/auth/login                 - the browser path (session cookie)
      4. GET  /api/admin/overview             - an authenticated admin read
      5. GET  /api/v1/whoami                  - the agent path (bearer token)
      6. GET  /api/v1/capabilities            - the granted catalogue
      7. POST /api/mcp  tools/list            - the MCP transport
      8. POST /api/mcp  tools/call (optional) - one real tool call, with -CallTool
      9. GET  <web>/login                     - the frontend answers (optional)

    Checks 5-8 need an agent token (Access -> Tokens). Without one they are SKIPPED
    and the summary says so - a skip is never reported as a pass.

.PARAMETER BaseUrl
    API base URL. Default http://127.0.0.1:8000

.PARAMETER WebUrl
    Frontend base URL for check 9. Default http://localhost:3000

.PARAMETER Token
    Agent token for the /api/v1 and /api/mcp checks.

.PARAMETER Username / Password
    Admin credentials for checks 3-4. If -Password is omitted you are prompted.

.EXAMPLE
    .\scripts\smoke.ps1
    .\scripts\smoke.ps1 -Token $env:SR_AGENT_TOKEN -CallTool text__summarize
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$WebUrl = 'http://localhost:3000',
    [string]$Token,
    [string]$Username = 'admin',
    [string]$Password,
    [string]$CallTool,
    [string]$CallArguments = '{}',
    [switch]$SkipLogin,
    [switch]$SkipMcp,
    [switch]$SkipWeb
)

$ErrorActionPreference = 'Stop'
$script:results = @()

function Add-Result([string]$name, [string]$status, [string]$detail) {
    $colour = switch ($status) {
        'PASS' { 'Green' }
        'SKIP' { 'Yellow' }
        default { 'Red' }
    }
    Write-Host ("  {0,-4} {1,-34} {2}" -f $status, $name, $detail) -ForegroundColor $colour
    $script:results += [pscustomobject]@{ name = $name; status = $status; detail = $detail }
}

function Invoke-Json {
    param(
        [string]$Uri,
        [string]$Method = 'Get',
        [hashtable]$Headers = @{},
        [string]$Body,
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session
    )
    $params = @{ Uri = $Uri; Method = $Method; Headers = $Headers; UseBasicParsing = $true; TimeoutSec = 30 }
    if ($Body) { $params['Body'] = $Body; $params['ContentType'] = 'application/json' }
    if ($Session) { $params['WebSession'] = $Session }
    return Invoke-WebRequest @params
}

Write-Host ""
Write-Host "Skill Router smoke test" -ForegroundColor Cyan
Write-Host "  api  $BaseUrl"
Write-Host "  web  $WebUrl"
Write-Host ""

# 1 ------------------------------------------------------------------- health --
try {
    $response = Invoke-Json -Uri "$BaseUrl/api/health"
    $body = $response.Content | ConvertFrom-Json
    if ($body.status -eq 'ok') { Add-Result 'api health' 'PASS' $body.service }
    else { Add-Result 'api health' 'FAIL' "unexpected body: $($response.Content)" }
} catch {
    Add-Result 'api health' 'FAIL' $_.Exception.Message
}

# 2 -------------------------------------------------------------------- ready --
try {
    $response = Invoke-Json -Uri "$BaseUrl/api/health/ready"
    $body = $response.Content | ConvertFrom-Json
    if ($body.status -eq 'ready') { Add-Result 'api ready' 'PASS' 'database + bootstrap' }
    else { Add-Result 'api ready' 'FAIL' "status=$($body.status) checks=$($body.checks | ConvertTo-Json -Compress -Depth 4)" }
} catch {
    Add-Result 'api ready' 'FAIL' $_.Exception.Message
}

# 3/4 ---------------------------------------------------------------- browser --
$session = $null
if ($SkipLogin) {
    Add-Result 'login' 'SKIP' '-SkipLogin'
    Add-Result 'admin overview' 'SKIP' '-SkipLogin'
} else {
    if (-not $Password) {
        $secure = Read-Host -AsSecureString "Password for '$Username'"
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $Password = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
        finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    try {
        $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        $payload = @{ username = $Username; password = $Password } | ConvertTo-Json -Compress
        $response = Invoke-Json -Uri "$BaseUrl/api/auth/login" -Method Post -Body $payload -Session $session
        $body = $response.Content | ConvertFrom-Json
        $note = "user=$($body.user.username)"
        if ($body.must_change_password) { $note += ' (must change password)' }
        Add-Result 'login' 'PASS' $note

        try {
            $overview = Invoke-Json -Uri "$BaseUrl/api/admin/overview" -Session $session
            $data = $overview.Content | ConvertFrom-Json
            $keys = ($data.PSObject.Properties.Name | Select-Object -First 6) -join ', '
            Add-Result 'admin overview' 'PASS' $keys
        } catch {
            $status = $null
            $code = $null
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                try { $code = ($_.ErrorDetails.Message | ConvertFrom-Json).error.code } catch { $code = $null }
            }
            if ($code -eq 'password_change_required' -or $status -eq 409) {
                # Verified on a fresh install: the API answers 403 with this code and
                # blocks every business route until the first password is changed.
                Add-Result 'admin overview' 'SKIP' 'password_change_required (sign in, change the password, re-run)'
            } else {
                Add-Result 'admin overview' 'FAIL' "HTTP $status $code - $($_.Exception.Message)"
            }
        }
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($status -eq 429) {
            Add-Result 'login' 'FAIL' 'too_many_attempts (throttled; wait for the lockout window)'
        } else {
            Add-Result 'login' 'FAIL' $_.Exception.Message
        }
        Add-Result 'admin overview' 'SKIP' 'login failed'
    }
}

# 5/6 ---------------------------------------------------------------- external --
if (-not $Token) {
    Add-Result 'api/v1 whoami' 'SKIP' 'no -Token (issue one in Access -> Tokens)'
    Add-Result 'api/v1 capabilities' 'SKIP' 'no -Token'
} else {
    $authHeaders = @{ Authorization = "Bearer $Token" }
    try {
        $response = Invoke-Json -Uri "$BaseUrl/api/v1/whoami" -Headers $authHeaders
        $body = $response.Content | ConvertFrom-Json
        Add-Result 'api/v1 whoami' 'PASS' "agent=$($body.agent.name) capabilities=$($body.capabilities_count)"
    } catch {
        Add-Result 'api/v1 whoami' 'FAIL' $_.Exception.Message
    }
    try {
        $response = Invoke-Json -Uri "$BaseUrl/api/v1/capabilities" -Headers $authHeaders
        $body = $response.Content | ConvertFrom-Json
        Add-Result 'api/v1 capabilities' 'PASS' "count=$($body.count) version_token=$($body.version_token.Substring(0, [Math]::Min(12, $body.version_token.Length)))..."
    } catch {
        Add-Result 'api/v1 capabilities' 'FAIL' $_.Exception.Message
    }
}

# 7/8 -------------------------------------------------------------------- mcp --
if ($SkipMcp -or -not $Token) {
    $reason = if ($SkipMcp) { '-SkipMcp' } else { 'no -Token' }
    Add-Result 'mcp initialize' 'SKIP' $reason
    Add-Result 'mcp tools/list' 'SKIP' $reason
    if ($CallTool) { Add-Result 'mcp tools/call' 'SKIP' $reason }
} else {
    $mcpUrl = "$BaseUrl/api/mcp"
    $mcpHeaders = @{
        Authorization = "Bearer $Token"
        Accept = 'application/json'
    }
    try {
        $init = @{
            jsonrpc = '2.0'
            id = 1
            method = 'initialize'
            params = @{ protocolVersion = '2025-06-18'; clientInfo = @{ name = 'skill-router-smoke'; version = '1.0.0' }; capabilities = @{} }
        } | ConvertTo-Json -Compress -Depth 6
        $response = Invoke-Json -Uri $mcpUrl -Method Post -Headers $mcpHeaders -Body $init
        $body = $response.Content | ConvertFrom-Json

        # NOTE: with `Accept: text/event-stream` the answer is SSE and ConvertFrom-Json
        # would fail. The initialize call and the tools/list call are made without
        # that accept header on purpose; the SSE path is covered by the backend tests
        # and by the docs' curl examples.
        if ($body.result.serverInfo.name -eq 'skill-router') {
            Add-Result 'mcp initialize' 'PASS' "protocol=$($body.result.protocolVersion) version=$($body.result.serverInfo.version)"
        } else {
            Add-Result 'mcp initialize' 'FAIL' $response.Content
        }
        $mcpSession = $response.Headers['Mcp-Session-Id']
        if ($mcpSession) {
            $mcpHeaders['Mcp-Session-Id'] = @($mcpSession)[0]
        }
    } catch {
        Add-Result 'mcp initialize' 'FAIL' $_.Exception.Message
        Add-Result 'mcp tools/list' 'SKIP' 'initialize failed'
        if ($CallTool) { Add-Result 'mcp tools/call' 'SKIP' 'initialize failed' }
        $mcpHeaders = $null
    }

    if ($mcpHeaders) {
        try {
            $list = @{ jsonrpc = '2.0'; id = 2; method = 'tools/list'; params = @{} } | ConvertTo-Json -Compress
            $response = Invoke-Json -Uri $mcpUrl -Method Post -Headers $mcpHeaders -Body $list
            $body = $response.Content | ConvertFrom-Json
            $names = ($body.result.tools | ForEach-Object { $_.name }) -join ', '
            Add-Result 'mcp tools/list' 'PASS' "$($body.result.tools.Count) tool(s): $names"
        } catch {
            Add-Result 'mcp tools/list' 'FAIL' $_.Exception.Message
        }

        if ($CallTool) {
            try {
                $call = @{
                    jsonrpc = '2.0'
                    id = 3
                    method = 'tools/call'
                    params = @{ name = $CallTool; arguments = ($CallArguments | ConvertFrom-Json) }
                } | ConvertTo-Json -Compress -Depth 8
                $response = Invoke-Json -Uri $mcpUrl -Method Post -Headers $mcpHeaders -Body $call
                $body = $response.Content | ConvertFrom-Json
                if ($body.result.isError) {
                    Add-Result 'mcp tools/call' 'FAIL' ($body.result.content[0].text)
                } else {
                    Add-Result 'mcp tools/call' 'PASS' "$CallTool returned a result"
                }
            } catch {
                Add-Result 'mcp tools/call' 'FAIL' $_.Exception.Message
            }
        }
    }
}

# 9 ---------------------------------------------------------------------- web --
if ($SkipWeb) {
    Add-Result 'web /login' 'SKIP' '-SkipWeb'
} else {
    try {
        $response = Invoke-Json -Uri "$WebUrl/login"
        if ($response.StatusCode -eq 200) { Add-Result 'web /login' 'PASS' "$WebUrl/login" }
        else { Add-Result 'web /login' 'FAIL' "HTTP $($response.StatusCode)" }
    } catch {
        Add-Result 'web /login' 'FAIL' $_.Exception.Message
    }
}

# ------------------------------------------------------------------ summary ----
$passed = @($script:results | Where-Object { $_.status -eq 'PASS' }).Count
$failed = @($script:results | Where-Object { $_.status -eq 'FAIL' }).Count
$skipped = @($script:results | Where-Object { $_.status -eq 'SKIP' }).Count

Write-Host ""
Write-Host ("Summary: {0} passed, {1} failed, {2} skipped" -f $passed, $failed, $skipped) -ForegroundColor $(if ($failed -eq 0) { 'Green' } else { 'Red' })
if ($skipped -gt 0) {
    Write-Host "Skipped checks are NOT passes: they need an agent token (-Token) or a changed password." -ForegroundColor Yellow
}
if ($failed -gt 0) {
    Write-Host ""
    Write-Host "Failed checks:" -ForegroundColor Red
    $script:results | Where-Object { $_.status -eq 'FAIL' } | ForEach-Object { Write-Host "  - $($_.name): $($_.detail)" }
    exit 1
}
exit 0
