param(
    [string]$ConfigFile,
    [string]$OutDir,
    [int]$Port = 19301,
    [int]$SavePng = 1,
    [switch]$DryRun,
    [switch]$DeadlineProbe,
    [ValidateSet('connect','login','reload','output','hash','cleanup','next','ok','ipcgood','ipcnoise','ipcbad','ipcerror','ipcnull')][string]$ProbeCase = 'connect',
    [int]$ProbeMs = 150,
    [switch]$ComboWorker,
    [int]$ComboIndex = 0,
    [switch]$ProbeWorker,
    [switch]$IpcProbe,
    [ValidateSet('next','good','noise','bad','error','null','timeout')][string]$IpcCase = 'next'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Send-JsonAndExit {
    param($Obj)
    if (-not $Obj.ContainsKey('visibility')) { $Obj['visibility'] = 'unknown' }
    if (-not $Obj.ContainsKey('focus_mode')) { $Obj['focus_mode'] = 'none' }
    if (-not $Obj.ContainsKey('login_evidence')) { $Obj['login_evidence'] = 'none' }
    if (-not $Obj.ContainsKey('login_wait_ms')) { $Obj['login_wait_ms'] = 0 }
    if (-not $Obj.ContainsKey('warnings')) { $Obj['warnings'] = @() }
    $json = ConvertTo-Json -InputObject $Obj -Depth 15 -Compress
    [Console]::Out.WriteLine($json)
    exit 0
}

# One monotonic budget for every blocking action in a combination.
function Get-ComboRemaining {
    param([System.Diagnostics.Stopwatch]$Clock, [int]$LimitMs = 60000)
    $left = [int]($LimitMs - $Clock.ElapsedMilliseconds)
    if ($left -le 0) { throw 'combo_timeout' }
    return $left
}

function Wait-Combo {
    param([System.Diagnostics.Stopwatch]$Clock, [int]$Ms, [int]$LimitMs = 60000)
    $left = Get-ComboRemaining -Clock $Clock -LimitMs $LimitMs
    $cts = [System.Threading.CancellationTokenSource]::new($left)
    try {
        [void]([System.Threading.Tasks.Task]::Delay($Ms, $cts.Token).GetAwaiter().GetResult())
    } catch [System.OperationCanceledException] {
        throw 'combo_timeout'
    } finally {
        $cts.Dispose()
    }
    [void](Get-ComboRemaining -Clock $Clock -LimitMs $LimitMs)
}

# Own each combination in a killable process. The parent owns no CDP session.
function Invoke-Worker {
    param([string[]]$WorkerArgs, [int]$BudgetMs = 60000)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = (Get-Process -Id $PID).Path
    $items = @('-NoProfile','-File',$PSCommandPath) + $WorkerArgs
    $psi.Arguments = ($items | ForEach-Object { '"' + ($_ -replace '"','\"') + '"' }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try {
        if (-not $proc.Start()) { throw 'worker_start_failed' }
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        $left = [Math]::Max(1, $BudgetMs - 1000 - [int]$watch.ElapsedMilliseconds)
        $timedOut = -not $proc.WaitForExit($left)
        if ($timedOut) {
            $proc.Kill()
            if (-not $proc.WaitForExit(500)) { throw 'worker_not_terminated' }
        }
        $stdout = $outTask.GetAwaiter().GetResult()
        $stderr = $errTask.GetAwaiter().GetResult()
        $watch.Stop()
        return @{ timeout = ($timedOut -or $watch.ElapsedMilliseconds -ge $BudgetMs);
            elapsed_ms = [int]$watch.ElapsedMilliseconds; stdout = $stdout; stderr = $stderr; rc = $proc.ExitCode }
    } finally {
        if ($proc -and -not $proc.HasExited) {
            try { $proc.Kill(); [void]$proc.WaitForExit(500) } catch {}
            if (-not $proc.HasExited) { throw 'worker_not_terminated' }
        }
        $proc.Dispose()
    }
}

# Both the production parent and the opt-in IPC test parse the whole stdout.
function Read-WorkerJson {
    param($Worker)
    if ($Worker.timeout -or $Worker.rc -ne 0) { return $null }
    try { return ConvertFrom-Json -InputObject (([string]$Worker.stdout).Trim()) -ErrorAction Stop }
    catch { return $null }
}

# Windows PowerShell 5.1 child/parent protocol, with real Wait-Combo calls.
if ($IpcProbe) {
    $names = if ($IpcCase -eq 'next') {
        @('noise','good','error','null','bad','timeout','good')
    } else { @($IpcCase) }
    $records = @()
    foreach ($name in $names) {
        $child = if ($name -eq 'timeout') { 'hash' } else { 'ipc' + $name }
        $w = Invoke-Worker -WorkerArgs @('-ProbeWorker','-ProbeCase',$child) -BudgetMs 4000
        $v = Read-WorkerJson -Worker $w
        $records += @{
            case = $name; accepted = ($null -ne $v);
            status = $(if ($v) { [string]$v.status } else { $null });
            error = $(if ($v -and @($v.results).Count) { $v.results[0].error } else { $null });
            stderr_ok = (([string]$w.stderr).Contains('ipc_expected_error'));
            timeout = [bool]$w.timeout; rc = $w.rc; elapsed_ms = $w.elapsed_ms
        }
    }
    Send-JsonAndExit @{schema=1;status='ipc_probe';results=$records}
}

# Explicit offline fault injection enters production methods in a supervised child.
if ($DeadlineProbe) {
    if ($ProbeMs -lt 1000 -or $ProbeMs -gt 60000) { throw 'invalid_probe_budget' }
    $names = if ($ProbeCase -eq 'next') { @('connect','ok','cleanup') } else { @($ProbeCase) }
    $probeResults = @()
    foreach ($name in $names) {
        $w = Invoke-Worker -WorkerArgs @('-ProbeWorker','-ProbeCase',$name) -BudgetMs $ProbeMs
        $passed = ($name -eq 'ok' -and -not $w.timeout -and $w.rc -eq 0)
        $err = if ($passed) { $null } elseif ($w.timeout) { 'combo_timeout' } else { 'worker_error' }
        $detail = if ($err -eq 'worker_error') { ([string]$w.stderr).Trim() } else { '' }
        $probeResults += @{ coin = $name; ok = $passed; error = $err; elapsed_ms = $w.elapsed_ms; detail = $detail.Substring(0, [Math]::Min(300, $detail.Length)) }
    }
    Send-JsonAndExit @{schema=1; status='deadline_probe'; budget_ms=$ProbeMs; results=$probeResults}
}

if (-not $ComboWorker -and -not $ProbeWorker -and -not $DryRun) {
    if ($ConfigFile -and (Test-Path $ConfigFile)) {
        $cfg = Get-Content $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $enabled = @($cfg.combos | Where-Object { $_.enabled -eq $true })
    } else {
        $enabled = @(
            @{coin='BTC'; mode='pair'; timeframe='24 hour'; model=1},
            @{coin='ETH'; mode='pair'; timeframe='24 hour'; model=1},
            @{coin='SOL'; mode='pair'; timeframe='24 hour'; model=1},
            @{coin='XRP'; mode='pair'; timeframe='24 hour'; model=1},
            @{coin='BTC'; mode='pair'; timeframe='1 week'; model=1},
            @{coin='ETH'; mode='pair'; timeframe='1 week'; model=1}
        )
    }
    $results = @()
    $meta = $null
    $runLimitMs = 230000
    $runClock = [System.Diagnostics.Stopwatch]::StartNew()
    [Console]::Error.WriteLine('PHASE start')
    for ($ix = 0; $ix -lt $enabled.Count; $ix++) {
        $c = $enabled[$ix]
        $elapsed = [int]$runClock.ElapsedMilliseconds
        [Console]::Error.WriteLine("PHASE combo $ix start t=$elapsed")
        $remaining = $runLimitMs - $elapsed
        if ($remaining -lt 20000) {
            $results += @{coin=$c.coin; mode=$c.mode; timeframe=$c.timeframe; model=$c.model;
                ok=$false; error='run_deadline'; login_required=$false; elapsed_ms=0}
            [Console]::Error.WriteLine("PHASE combo $ix end ok=false t=$([int]$runClock.ElapsedMilliseconds)")
            continue
        }
        $workerBudgetMs = [Math]::Min(60000, $remaining - 5000)
        $args = @('-ComboWorker','-ComboIndex',"$ix",'-Port',"$Port",'-SavePng',"$SavePng")
        if ($ConfigFile) { $args += @('-ConfigFile',$ConfigFile) }
        if ($OutDir) { $args += @('-OutDir',$OutDir) }
        $w = Invoke-Worker -WorkerArgs $args -BudgetMs $workerBudgetMs
        $v = Read-WorkerJson -Worker $w
        if ($v -and @($v.results).Count -eq 1 -and $v.results[0].coin -eq $c.coin -and $v.results[0].timeframe -eq $c.timeframe -and $w.elapsed_ms -lt $workerBudgetMs) {
            $meta = $v
            $rec = $v.results[0]
            $rec.elapsed_ms = $w.elapsed_ms
            $results += $rec
        } else {
            $err = if ($w.timeout) { 'combo_timeout' } elseif ($v -and $v.status -ne 'ok') { [string]$v.status } else { 'powershell_error' }
            $results += @{coin=$c.coin; mode=$c.mode; timeframe=$c.timeframe; model=$c.model;
                ok=$false; error=$err; login_required=$false; elapsed_ms=$w.elapsed_ms}
        }
        [Console]::Error.WriteLine("PHASE combo $ix end ok=$(([bool]$results[-1].ok).ToString().ToLowerInvariant()) t=$([int]$runClock.ElapsedMilliseconds)")
    }
    $allOk = @($results | Where-Object { -not $_.ok }).Count -eq 0
    Send-JsonAndExit @{
        schema=1; status=$(if ($allOk) {'ok'} else {'partial'});
        chrome=$(if ($meta) {$meta.chrome} else {@{version='';port=19301;owner_ok=$false}});
        page=$(if ($meta) {$meta.page} else {@{title='';url='';tabs=@()}});
        logged_in=$(if ($meta) {$meta.logged_in} else {'unknown'});
        login_evidence=$(if ($meta) {$meta.login_evidence} else {'none'});
        login_wait_ms=$(if ($meta) {$meta.login_wait_ms} else {0});
        visibility=$(if ($meta) {$meta.visibility} else {'unknown'});
        focus_mode=$(if ($meta) {$meta.focus_mode} else {'none'});
        warnings=$(if ($meta) {$meta.warnings} else {@()});
        viewport=@(1400,1200); browser_tz_offset_min=$(if ($meta) {$meta.browser_tz_offset_min} else {0});
        results=$results
    }
}

if (-not $ProbeWorker) {
# 1. Port whitelist check - strictly only 19301
if ($Port -ne 19301) {
    Send-JsonAndExit @{
        schema = 1
        status = 'wrong_chrome'
        chrome = @{ version = ''; port = $Port; owner_ok = $false }
        page = @{ title = ''; url = ''; tabs = @() }
        logged_in = 'unknown'
        viewport = @(1400, 1200)
        results = @()
    }
}

# 2. Verify listener process command line contains CoinglassDedicatedChrome
try {
    $conns = @(Get-NetTCPConnection -LocalPort 19301 -State Listen -ErrorAction Stop)
    if ($conns.Count -eq 0) {
        Send-JsonAndExit @{
            schema = 1
            status = 'chrome_unreachable'
            chrome = @{ version = ''; port = 19301; owner_ok = $false }
            page = @{ title = ''; url = ''; tabs = @() }
            logged_in = 'unknown'
            viewport = @(1400, 1200)
            results = @()
        }
    }
    $owningPid = $conns[0].OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $owningPid" -ErrorAction Stop
    if (-not ($proc.CommandLine -like "*CoinglassDedicatedChrome*")) {
        Send-JsonAndExit @{
            schema = 1
            status = 'wrong_chrome'
            chrome = @{ version = ''; port = 19301; owner_ok = $false }
            page = @{ title = ''; url = ''; tabs = @() }
            logged_in = 'unknown'
            viewport = @(1400, 1200)
            results = @()
        }
    }
} catch {
    Send-JsonAndExit @{
        schema = 1
        status = 'chrome_unreachable'
        chrome = @{ version = ''; port = 19301; owner_ok = $false }
        page = @{ title = ''; url = ''; tabs = @() }
        logged_in = 'unknown'
        viewport = @(1400, 1200)
        results = @()
    }
}

# 3. Connect to Chrome HTTP endpoints
try {
    $versionInfo = Invoke-RestMethod -Uri "http://127.0.0.1:19301/json/version" -TimeoutSec 5
    $targets = Invoke-RestMethod -Uri "http://127.0.0.1:19301/json/list" -TimeoutSec 5
} catch {
    Send-JsonAndExit @{
        schema = 1
        status = 'chrome_unreachable'
        chrome = @{ version = ''; port = 19301; owner_ok = $true }
        page = @{ title = ''; url = ''; tabs = @() }
        logged_in = 'unknown'
        viewport = @(1400, 1200)
        results = @()
    }
}

$browserVersion = [string]$versionInfo.Browser
$allTabs = @()
foreach ($t in $targets) {
    $hostName = ''
    try {
        if ($t.url) {
            $u = [System.Uri]$t.url
            $hostName = $u.Host
        }
    } catch {}
    $allTabs += @{ type = [string]$t.type; host = $hostName }
}

$coinglassPages = @($targets | Where-Object { $_.type -eq 'page' -and ([string]$_.url) -like 'https://www.coinglass.com/*' })
if ($coinglassPages.Count -lt 1) {
    Send-JsonAndExit @{
        schema = 1
        status = 'no_coinglass_tab'
        chrome = @{ version = $browserVersion; port = 19301; owner_ok = $true }
        page = @{ title = ''; url = ''; tabs = $allTabs }
        logged_in = 'unknown'
        viewport = @(1400, 1200)
        results = @()
    }
}

$targetTab = $coinglassPages[0]
}

# Add PdSess C# WebSocket transport
if (-not ([System.Management.Automation.PSTypeName]'PdSess').Type) {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.IO;
using System.Net.WebSockets;
using System.Threading;

public class PdSess : IDisposable {
    ClientWebSocket ws = new ClientWebSocket();
    CancellationTokenSource ct = new CancellationTokenSource();
    string ep = "";
    public static string BlockAt = "";
    public void Open(string endpoint) {
        ep = endpoint;
        using (var init = new CancellationTokenSource(5000)) {
            Connect(init.Token);
        }
    }
    private void Connect(CancellationToken token) {
        if (BlockAt == "connect") new ManualResetEvent(false).WaitOne();
        if (ws != null) {
            try { ws.Dispose(); } catch {}
        }
        ws = new ClientWebSocket();
        ws.ConnectAsync(new Uri(ep), token).GetAwaiter().GetResult();
    }
    public void EnsureOpen(CancellationToken token) {
        if (ws == null || ws.State != WebSocketState.Open) {
            Connect(token);
        }
    }
    public string Call(string payload, int id, int timeoutMs = 60000) {
        if (BlockAt == "login" || BlockAt == "reload") new ManualResetEvent(false).WaitOne();
        if (timeoutMs <= 0) throw new Exception("combo_timeout");
        using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct.Token)) {
            cts.CancelAfter(timeoutMs);
            try {
                EnsureOpen(cts.Token);
                var data = Encoding.UTF8.GetBytes(payload);
                ws.SendAsync(new ArraySegment<byte>(data), WebSocketMessageType.Text, true, cts.Token).GetAwaiter().GetResult();
                var buffer = new byte[65536];
                for (int n = 0; n < 1000; n++) {
                    using (var stream = new MemoryStream()) {
                        WebSocketReceiveResult r;
                        do {
                            r = ws.ReceiveAsync(new ArraySegment<byte>(buffer), cts.Token).GetAwaiter().GetResult();
                            stream.Write(buffer, 0, r.Count);
                        } while (!r.EndOfMessage);
                        var m = Encoding.UTF8.GetString(stream.ToArray());
                        if (m.StartsWith("{\"id\":" + id + ",") || m.StartsWith("{\"id\": " + id + ",")) {
                            return m;
                        }
                    }
                }
                throw new Exception("no response for id " + id);
            } catch (OperationCanceledException) {
                if (ct.IsCancellationRequested) {
                    throw new Exception("global_session_timeout");
                }
                throw new Exception("combo_timeout");
            }
        }
    }
    public void Dispose() {
        if (BlockAt == "cleanup") new ManualResetEvent(false).WaitOne();
        try { ws.Dispose(); } catch {}
        try { ct.Dispose(); } catch {}
    }
}
'@
}

$reqIdCounter = 0

function Invoke-CdpCall {
    param($Sess, [string]$Method, $Params = @{}, [int]$TimeoutMs = 60000, [System.Diagnostics.Stopwatch]$Clock)
    $script:reqIdCounter++
    $id = $script:reqIdCounter
    $payload = @{
        id = $id
        method = $Method
        params = $Params
    } | ConvertTo-Json -Depth 15 -Compress
    if ($Clock) { $TimeoutMs = [Math]::Min($TimeoutMs, (Get-ComboRemaining -Clock $Clock)) }
    $raw = $Sess.Call($payload, $id, $TimeoutMs)
    if ($Clock) { [void](Get-ComboRemaining -Clock $Clock) }
    return ($raw | ConvertFrom-Json)
}

function Invoke-CdpEval {
    param($Sess, [string]$Expression, [switch]$AwaitPromise, [int]$TimeoutMs = 60000, [System.Diagnostics.Stopwatch]$Clock)
    $script:reqIdCounter++
    $id = $script:reqIdCounter
    $p = @{
        expression = $Expression
        returnByValue = $true
    }
    if ($AwaitPromise) {
        $p.awaitPromise = $true
    }
    $payload = @{
        id = $id
        method = 'Runtime.evaluate'
        params = $p
    } | ConvertTo-Json -Depth 15 -Compress
    if ($Clock) { $TimeoutMs = [Math]::Min($TimeoutMs, (Get-ComboRemaining -Clock $Clock)) }
    $raw = $Sess.Call($payload, $id, $TimeoutMs)
    if ($Clock) { [void](Get-ComboRemaining -Clock $Clock) }
    $resp = $raw | ConvertFrom-Json
    if ($resp.error) {
        throw $resp.error.message
    }
    if ($resp.result.exceptionDetails) {
        $desc = $resp.result.exceptionDetails.exception.description
        throw ($desc -join "`n")
    }
    return $resp.result.result.value
}

function Wait-CanvasReady {
    param($Sess, [System.Diagnostics.Stopwatch]$Clock, [int]$MaxMs = 8000, [int]$StepMs = 500)
    $waitClock = [System.Diagnostics.Stopwatch]::StartNew()
    $streak = 0
    $probeJs = "(()=>{const c=document.querySelector('canvas');return !!(c&&c.width>0&&c.height>0)})()"
    while ($waitClock.ElapsedMilliseconds -lt $MaxMs) {
        $left = Get-ComboRemaining -Clock $Clock
        $waitLeft = $MaxMs - [int]$waitClock.ElapsedMilliseconds
        if ($waitLeft -le 0) { break }
        $limit = [Math]::Min($left, $waitLeft)
        try {
            $ready = Invoke-CdpEval -Sess $Sess -Expression $probeJs -TimeoutMs $limit -Clock $Clock
        } catch {
            if ($_.Exception.Message -eq 'combo_timeout' -and
                $waitClock.ElapsedMilliseconds -ge $MaxMs) {
                [void](Get-ComboRemaining -Clock $Clock)
                return $false
            }
            throw
        }
        if ($waitClock.ElapsedMilliseconds -ge $MaxMs) {
            [void](Get-ComboRemaining -Clock $Clock)
            return $false
        }
        if ($ready -eq $true) {
            $streak++
            if ($streak -ge 2) { return $true }
        } else {
            $streak = 0
        }
        $waitLeft = $MaxMs - [int]$waitClock.ElapsedMilliseconds
        if ($waitLeft -le 0) { break }
        Wait-Combo -Clock $Clock -Ms ([Math]::Min($StepMs, $waitLeft))
    }
    return $false
}

function Get-ChartCapture {
    param($Sess, [System.Diagnostics.Stopwatch]$Clock, [string]$Js, [ref]$RetryUsed)
    $json = Invoke-CdpEval -Sess $Sess -Expression $Js -TimeoutMs (Get-ComboRemaining -Clock $Clock) -Clock $Clock
    $capture = $json | ConvertFrom-Json
    if ($capture.err -eq 'no canvas' -and
        ($null -eq $RetryUsed -or -not $RetryUsed.Value)) {
        if (Wait-CanvasReady -Sess $Sess -Clock $Clock) {
            if ($null -ne $RetryUsed) { $RetryUsed.Value = $true }
            $json = Invoke-CdpEval -Sess $Sess -Expression $Js -TimeoutMs (Get-ComboRemaining -Clock $Clock) -Clock $Clock
            $capture = $json | ConvertFrom-Json
        }
    }
    return $capture
}

function Compress-GzipData {
    param([string]$Data, [string]$FilePath)
    if ($ProbeWorker -and $ProbeCase -eq 'output') { [void]([System.Threading.ManualResetEvent]::new($false).WaitOne()) }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Data)
    $fs = [System.IO.File]::Create($FilePath)
    try {
        $gz = New-Object System.IO.Compression.GZipStream($fs, [System.IO.Compression.CompressionMode]::Compress)
        try {
            $gz.Write($bytes, 0, $bytes.Length)
        } finally {
            $gz.Dispose()
        }
    } finally {
        $fs.Dispose()
    }
}

function Get-Sha256Hex {
    param([string]$FilePath)
    if ($ProbeWorker -and $ProbeCase -eq 'hash') { [void]([System.Threading.ManualResetEvent]::new($false).WaitOne()) }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $fs = [System.IO.File]::OpenRead($FilePath)
        try {
            $hashBytes = $sha256.ComputeHash($fs)
            return (($hashBytes | ForEach-Object { $_.ToString("x2") }) -join "")
        } finally {
            $fs.Dispose()
        }
    } finally {
        $sha256.Dispose()
    }
}

function Convert-TfToTag {
    param([string]$Tf)
    if ($Tf -eq '24 hour') { return '24h' }
    if ($Tf -eq '1 week') { return '1w' }
    if ($Tf -eq '12 hour') { return '12h' }
    if ($Tf -eq '48 hour') { return '48h' }
    if ($Tf -eq '3 day') { return '3d' }
    if ($Tf -eq '2 week') { return '2w' }
    if ($Tf -eq '1 month') { return '1m' }
    if ($Tf -eq '3 month') { return '3m' }
    if ($Tf -eq '6 month') { return '6m' }
    if ($Tf -eq '1 Year') { return '1Y' }
    if ($Tf -eq '2 Year') { return '2Y' }
    return ($Tf -replace '\s+', '')
}

if ($ProbeWorker) {
    [PdSess]::BlockAt = $ProbeCase
    $probeSess = New-Object PdSess
    switch ($ProbeCase) {
        'connect' { $probeSess.Open('ws://127.0.0.1:19301/') }
        'login' { Invoke-CdpEval -Sess $probeSess -Expression 'true' | Out-Null }
        'reload' { Invoke-CdpCall -Sess $probeSess -Method 'Page.reload' | Out-Null }
        'output' { Compress-GzipData -Data '{}' -FilePath $null }
        'hash' { Get-Sha256Hex -FilePath $null | Out-Null }
        'cleanup' { $probeSess.Dispose() }
        'ok' { Send-JsonAndExit @{schema=1;status='ok';results=@()} }
        'ipcgood' {
            Wait-Combo -Clock ([System.Diagnostics.Stopwatch]::StartNew()) -Ms 20
            Send-JsonAndExit @{schema=1;status='ok';results=@(@{coin='BTC';ok=$true;error=$null})}
        }
        'ipcerror' {
            Wait-Combo -Clock ([System.Diagnostics.Stopwatch]::StartNew()) -Ms 20
            [Console]::Error.WriteLine('ipc_expected_error')
            Send-JsonAndExit @{schema=1;status='partial';results=@(@{coin='BTC';ok=$false;error='chart_missing'})}
        }
        'ipcnull' {
            Wait-Combo -Clock ([System.Diagnostics.Stopwatch]::StartNew()) -Ms 20
            Send-JsonAndExit @{schema=1;status='partial';results=@(@{coin='BTC';ok=$false;error=$null})}
        }
        'ipcnoise' {
            Wait-Combo -Clock ([System.Diagnostics.Stopwatch]::StartNew()) -Ms 20
            [Console]::Out.WriteLine('unexpected_prefix')
            Send-JsonAndExit @{schema=1;status='ok';results=@(@{coin='BTC';ok=$true;error=$null})}
        }
        'ipcbad' {
            Wait-Combo -Clock ([System.Diagnostics.Stopwatch]::StartNew()) -Ms 20
            [Console]::Out.WriteLine('{"status":')
            exit 0
        }
    }
    throw 'probe_did_not_block'
}

$sess = New-Object PdSess
$sessConnected = $false

try {
    $sess.Open($targetTab.webSocketDebuggerUrl)
    $sessConnected = $true

    # (1) Emulation.setFocusEmulationEnabled enabled=$true per Task 2
    Invoke-CdpCall -Sess $sess -Method 'Emulation.setFocusEmulationEnabled' -Params @{
        enabled = $true
    } | Out-Null

    # (2) setDeviceMetricsOverride to 1400x1200
    Invoke-CdpCall -Sess $sess -Method 'Emulation.setDeviceMetricsOverride' -Params @{
        width = 1400
        height = 1200
        deviceScaleFactor = 1
        mobile = $false
    } | Out-Null

    # (3) Start-Sleep 500 ms and read document.visibilityState
    Start-Sleep -Milliseconds 500
    $pageVisibility = Invoke-CdpEval -Sess $sess -Expression 'document.visibilityState'
    if (-not $pageVisibility) { $pageVisibility = 'unknown' }

    # (4) ONLY if it is 'hidden': call Page.bringToFront fallback
    $captureWarnings = @()
    if ($pageVisibility -eq 'hidden') {
        Invoke-CdpCall -Sess $sess -Method 'Page.bringToFront' | Out-Null
        Start-Sleep -Milliseconds 500
        $pageVisibility = Invoke-CdpEval -Sess $sess -Expression 'document.visibilityState'
        if (-not $pageVisibility) { $pageVisibility = 'unknown' }
        $focusMode = 'bringToFront_fallback'
        if ($pageVisibility -eq 'hidden') {
            $captureWarnings += 'page_hidden'
        }
    } else {
        $focusMode = 'emulation_only'
    }

    # Check current URL and navigate if necessary
    $curUrl = Invoke-CdpEval -Sess $sess -Expression 'window.location.href'
    if ($curUrl -ne 'https://www.coinglass.com/pro/futures/LiquidationHeatMap') {
        Invoke-CdpCall -Sess $sess -Method 'Page.navigate' -Params @{
            url = 'https://www.coinglass.com/pro/futures/LiquidationHeatMap'
        } | Out-Null
        
        # Wait up to 40 seconds for chart container or canvas
        $loaded = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Seconds 1
            $found = Invoke-CdpEval -Sess $sess -Expression 'Boolean(document.querySelector(".echarts-for-react") || document.querySelector("canvas"))'
            if ($found -eq $true) {
                $loaded = $true
                break
            }
        }
    }

    $pageTitle = Invoke-CdpEval -Sess $sess -Expression 'document.title'
    $pageUrl = Invoke-CdpEval -Sess $sess -Expression 'window.location.href'
    $tzOffset = Invoke-CdpEval -Sess $sess -Expression 'new Date().getTimezoneOffset()'

    # Login detection per Task 1: async polling function kept in $loginPollJs
    $loginPollJs = @'
(async (maxMs, intervalMs) => {
    const start = Date.now();
    let userMenuConsecutive = 0;
    let loginStart = null;
    while (true) {
        const allElements = document.querySelectorAll('*');
        const leaves = [];
        for (const el of allElements) {
            if (el.children.length === 0) {
                leaves.push(el);
            }
        }
        let hasLogout = false;
        for (const el of leaves) {
            const txt = (el.innerText !== undefined ? el.innerText : el.textContent || '').trim();
            if (txt.toLowerCase() === 'log out' || txt === 'Log out') {
                hasLogout = true;
                break;
            }
        }
        if (hasLogout) {
            return JSON.stringify({ state: 'true', evidence: 'logout_leaf', waited_ms: Date.now() - start });
        }
        let hasLogin = false;
        for (const el of leaves) {
            const tag = (el.tagName || '').toUpperCase();
            if (tag === 'BUTTON' || tag === 'A') {
                const txt = (el.innerText !== undefined ? el.innerText : el.textContent || '').trim().toLowerCase();
                if (txt === 'login' || txt === 'log in' || txt === 'sign up' || txt === 'sign in') {
                    hasLogin = true;
                    break;
                }
            }
        }
        if (hasLogin) {
            if (loginStart === null) loginStart = Date.now();
        } else {
            loginStart = null;
        }
        let hasUserButton = false;
        if (window.innerWidth >= 1000) {
            const header = document.querySelector('header');
            if (header) {
                const buttons = header.querySelectorAll('button');
                for (const btn of buttons) {
                    const txt = (btn.innerText !== undefined ? btn.innerText : btn.textContent || '').trim();
                    if (txt === '' && btn.querySelector('svg')) {
                        const rect = btn.getBoundingClientRect();
                        if (rect && rect.right >= window.innerWidth - 40 && rect.top >= 0 && rect.bottom <= 140) {
                            hasUserButton = true;
                            break;
                        }
                    }
                }
            }
        }
        if (hasUserButton && !hasLogin) {
            userMenuConsecutive++;
            if (userMenuConsecutive >= 2) {
                return JSON.stringify({ state: 'true', evidence: 'user_menu_button', waited_ms: Date.now() - start });
            }
        } else {
            userMenuConsecutive = 0;
        }
        if (loginStart !== null && Date.now() - loginStart >= 2000) {
            return JSON.stringify({ state: 'false', evidence: 'login_leaf', waited_ms: Date.now() - start });
        }
        if (Date.now() - start >= maxMs) {
            return JSON.stringify({ state: 'unknown', evidence: 'timeout', waited_ms: Date.now() - start });
        }
        await new Promise(r => setTimeout(r, intervalMs));
    }
})
'@

    $loginResJson = Invoke-CdpEval -Sess $sess -Expression "($loginPollJs)(10000, 500)" -AwaitPromise
    $loginRes = $loginResJson | ConvertFrom-Json
    $loggedIn = if ($loginRes.state -eq 'true') { $true } elseif ($loginRes.state -eq 'false') { $false } else { 'unknown' }
    $loginEvidence = [string]$loginRes.evidence
    $loginWaitMs = [int]$loginRes.waited_ms

    if ($DryRun) {
        $dryStatus = if ($loggedIn -eq $false) { 'not_logged_in' } else { 'ok' }
        Send-JsonAndExit @{
            schema = 1
            status = $dryStatus
            chrome = @{ version = $browserVersion; port = 19301; owner_ok = $true }
            page = @{ title = $pageTitle; url = $pageUrl; tabs = $allTabs }
            logged_in = $loggedIn
            login_evidence = $loginEvidence
            login_wait_ms = $loginWaitMs
            visibility = $pageVisibility
            focus_mode = $focusMode
            warnings = $captureWarnings
            viewport = @(1400, 1200)
            browser_tz_offset_min = $tzOffset
            results = @()
        }
    }

    # Load configuration
    $config = $null
    if ($ConfigFile -and (Test-Path $ConfigFile)) {
        $config = Get-Content -Path $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if (-not $config) {
        $config = @{
            combos = @(
                @{ coin = 'BTC'; mode = 'pair'; timeframe = '24 hour'; model = 1; enabled = $true },
                @{ coin = 'ETH'; mode = 'pair'; timeframe = '24 hour'; model = 1; enabled = $true },
                @{ coin = 'SOL'; mode = 'pair'; timeframe = '24 hour'; model = 1; enabled = $true },
                @{ coin = 'XRP'; mode = 'pair'; timeframe = '24 hour'; model = 1; enabled = $true },
                @{ coin = 'BTC'; mode = 'pair'; timeframe = '1 week'; model = 1; enabled = $true },
                @{ coin = 'ETH'; mode = 'pair'; timeframe = '1 week'; model = 1; enabled = $true }
            )
        }
    }

    if ($OutDir -and -not (Test-Path $OutDir)) {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    }

    $results = @()
    $combos = @($config.combos | Where-Object { $_.enabled -eq $true })
    if ($ComboWorker) {
        if ($ComboIndex -lt 0 -or $ComboIndex -ge $combos.Count) { throw 'invalid_combo_index' }
        $combos = @($combos[$ComboIndex])
    }

    $driverJsCode = @'
(async (coin, model, tf, mode) => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const fire = (el, t) => el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window, button:0}));
    const click = el => { for (const t of ['pointerdown','mousedown','pointerup','mouseup','click']) fire(el, t); };
    const out = {steps:[]};
    const tabs = () => [...document.querySelectorAll('[role=tab]')];
    const tab = n => tabs().find(e => e.innerText.trim() === n);
    const head = () => ((document.querySelector('h1,h2,h3,h4')||{}).innerText||'').trim();

    const checkLoginDialog = () => {
        const dlgs = [...document.querySelectorAll('[role=dialog],.MuiModal-root')];
        for (const d of dlgs) {
            const txt = d.innerText || '';
            if (/Log in to unlock/i.test(txt)) {
                return true;
            }
        }
        return false;
    };

    const waitForOption = async (text, maxMs = 8000, intervalMs = 250) => {
        const start = Date.now();
        while (Date.now() - start < maxMs) {
            if (checkLoginDialog()) {
                return null;
            }
            const opts = [...document.querySelectorAll('[role=option]')];
            const found = opts.find(e => e.innerText.trim() === text);
            if (found) return found;
            await sleep(intervalMs);
        }
        return null;
    };

    const typeInto = async (ac, text) => {
        ac.focus();
        click(ac);
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(ac, text);
        ac.dispatchEvent(new Event('input',{bubbles:true}));
    };
    
    if (!tab('Symbol')) return JSON.stringify({err:'no tabs'});
    const wantHead = mode === 'pair' ? 'Binance ' + coin + '/USDT Liquidation Heatmap' : coin + ' Liquidation Heatmap';
    
    if (head() !== wantHead) {
        if (tab('Symbol').getAttribute('aria-selected') !== 'true') {
            click(tab('Symbol')); await sleep(1500);
        }
        if (!head().startsWith(coin + ' ')) {
            const acCoin = document.querySelector('.MuiAutocomplete-input');
            if (acCoin) {
                await typeInto(acCoin, coin);
                const o = await waitForOption(coin, 8000, 250);
                if (!o) {
                    if (checkLoginDialog()) {
                        out.login_required = true;
                        out.err = 'login_required';
                        return JSON.stringify(out);
                    }
                    out.err = 'coin option missing';
                    return JSON.stringify(out);
                }
                click(o); await sleep(1500); out.steps.push('coin');
            }
        }
        if (mode === 'pair') {
            click(tab('Pair')); await sleep(1500);
            const acPair = document.querySelector('.MuiAutocomplete-input');
            if (acPair) {
                acPair.focus(); click(acPair);
                const o = await waitForOption('Binance ' + coin + '/USDT Perpetual', 8000, 250);
                if (!o) {
                    if (checkLoginDialog()) {
                        out.login_required = true;
                        out.err = 'login_required';
                        return JSON.stringify(out);
                    }
                    out.err = 'pair option missing';
                    return JSON.stringify(out);
                }
                click(o); await sleep(1500); out.steps.push('pair');
            }
        }
    }
    if (model) {
        const m = tab('Model ' + model);
        if (m && m.getAttribute('aria-selected') !== 'true') {
            click(m); await sleep(1500); out.steps.push('model');
        }
    }
    if (tf) {
        const btn = [...document.querySelectorAll('.MuiSelect-button')].find(e => /hour|day|week|month|Year/.test(e.innerText));
        if (btn && btn.innerText.trim() !== tf) {
            click(btn);
            const o = await waitForOption(tf, 8000, 250);
            if (!o) {
                if (checkLoginDialog()) {
                    out.login_required = true;
                    out.err = 'login_required';
                    return JSON.stringify(out);
                }
                out.err = 'tf option missing';
                return JSON.stringify(out);
            }
            click(o); await sleep(1500); out.steps.push('tf');
        }
    }
    // Check for login required dialog
    if (checkLoginDialog()) {
        out.login_required = true;
        out.err = 'login_required';
        return JSON.stringify(out);
    }
    out.head = head();
    return JSON.stringify(out);
})
'@

    $captureChartJsCode = @'
(() => {
    const c = document.querySelector('.echarts-for-react');
    const cv = document.querySelector('canvas');
    if (!cv) return JSON.stringify({err: 'no canvas'});
    const png = cv.toDataURL('image/png');
    const tzOffset = new Date().getTimezoneOffset();
    if (!c) {
        return JSON.stringify({chart_data_ok: false, png, w: cv.width, h: cv.height, tzOffset});
    }
    const fk = Object.keys(c).find(k => k.startsWith('__reactFiber'));
    let f = c[fk]; let n = null;
    for (let i = 0; i < 30 && f; i++) {
        if (f.stateNode && f.stateNode.getEchartsInstance) { n = f.stateNode; break; }
        f = f.return;
    }
    if (!n) {
        return JSON.stringify({chart_data_ok: false, png, w: cv.width, h: cv.height, tzOffset});
    }
    const o = n.getEchartsInstance().getOption();
    const hm = o.series ? o.series.find(s => s.type === 'heatmap') : null;
    const cs = o.series ? o.series.find(s => s.type === 'candlestick') : null;
    const cells = [];
    if (hm && hm.data) {
        for (const d of hm.data) {
            const a = Array.isArray(d) ? d : d.value;
            if (a && a[2] > 0) cells.push([a[0], a[1], Math.round(a[2])]);
        }
    }
    const x = (o.xAxis && o.xAxis[0] && o.xAxis[0].data) || [];
    const ybins = (o.yAxis && o.yAxis[0] && o.yAxis[0].data) || [];
    const ymin = o.yAxis && o.yAxis[1] ? o.yAxis[1].min : null;
    const ymax = o.yAxis && o.yAxis[1] ? o.yAxis[1].max : null;
    const candles = cs && cs.data ? cs.data : [];
    const vmax = (o.visualMap || [{}])[0].max || null;
    const grid = o.grid && o.grid[0] ? o.grid[0] : null;
    return JSON.stringify({
        chart_data_ok: true,
        x, ybins, ymin, ymax, cells, candles, vmax, grid,
        w: cv.width, h: cv.height, png, tzOffset
    });
})()
'@

    $comboTimeoutMs = 60000
    foreach ($combo in $combos) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $coin = $combo.coin
        $mode = $combo.mode
        $timeframe = $combo.timeframe
        $model = $combo.model
        $tfTag = Convert-TfToTag -Tf $timeframe
        $utcStr = [System.DateTime]::UtcNow.ToString("yyyyMMdd'T'HHmmss'Z'")
        $baseName = "${utcStr}_${coin}_${mode}_${tfTag}_m${model}"

        $comboOk = $false
        $comboError = $null
        $loginRequired = $false
        $header = ''
        $price = $null
        $yMin = $null
        $yMax = $null
        $nX = 0
        $nY = 0
        $xFirst = ''
        $xLast = ''
        $canvasSize = @(0, 0)
        $pngFile = $null
        $pngSha256 = $null
        $chartFile = $null
        $chartSha256 = $null
        $chartDataOk = $false

        # A single retry is shared by switch and canvas capture.
        $retryUsed = $false
        for ($attempt = 0; $attempt -lt 2; $attempt++) {
            try {
                $remMs = Get-ComboRemaining -Clock $sw
                if ($remMs -le 0 -or $sw.ElapsedMilliseconds -ge $comboTimeoutMs) {
                    $comboError = 'combo_timeout'
                    break
                }
                $callTimeout = $remMs

                $switchExpr = "($driverJsCode)('$coin', $model, '$timeframe', '$mode')"
                $switchResJson = Invoke-CdpEval -Sess $sess -Expression $switchExpr -AwaitPromise -TimeoutMs $callTimeout -Clock $sw
                $switchRes = $switchResJson | ConvertFrom-Json

                if ($switchRes.login_required -eq $true -or $switchRes.err -eq 'login_required') {
                    $loginRequired = $true
                    $comboError = 'login_required'
                    $loggedIn = $false

                    # Close dialog using real Input.dispatchKeyEvent Escape and verify closed
                    Invoke-CdpCall -Sess $sess -Method 'Input.dispatchKeyEvent' -Params @{
                        type = 'rawKeyDown'
                        key = 'Escape'
                        code = 'Escape'
                        windowsVirtualKeyCode = 27
                        nativeVirtualKeyCode = 27
                    } -TimeoutMs 5000 -Clock $sw | Out-Null
                    Invoke-CdpCall -Sess $sess -Method 'Input.dispatchKeyEvent' -Params @{
                        type = 'keyUp'
                        key = 'Escape'
                        code = 'Escape'
                        windowsVirtualKeyCode = 27
                        nativeVirtualKeyCode = 27
                    } -TimeoutMs 5000 -Clock $sw | Out-Null

                    # Read back to verify dialog is closed
                    for ($k = 0; $k -lt 10; $k++) {
                        Wait-Combo -Clock $sw -Ms 100
                        $dlgOpen = Invoke-CdpEval -Sess $sess -Expression 'Boolean([...document.querySelectorAll("[role=dialog],.MuiModal-root")].find(d => /Log in to unlock/i.test(d.innerText || "")))' -TimeoutMs 2000 -Clock $sw
                        if (-not $dlgOpen) { break }
                    }
                    break
                }
                if ($switchRes.err) {
                    $remMs = Get-ComboRemaining -Clock $sw
                    if ($attempt -eq 0 -and -not $retryUsed -and $remMs -gt 9500) {
                        $retryUsed = $true
                        Invoke-CdpCall -Sess $sess -Method 'Page.reload' -TimeoutMs 5000 -Clock $sw | Out-Null
                        Wait-Combo -Clock $sw -Ms 4000
                        continue
                    }
                    $comboError = if ($sw.ElapsedMilliseconds -ge $comboTimeoutMs) { 'combo_timeout' } else { [string]$switchRes.err }
                    break
                }

                $header = [string]$switchRes.head
                $remMs = Get-ComboRemaining -Clock $sw
                if ($remMs -le 4000) {
                    $comboError = 'combo_timeout'
                    break
                }
                Wait-Combo -Clock $sw -Ms 4000

                $remMs = Get-ComboRemaining -Clock $sw
                if ($remMs -le 0) {
                    $comboError = 'combo_timeout'
                    break
                }
                $callTimeout = $remMs

                # Capture chart data and canvas
                $chartRes = Get-ChartCapture -Sess $sess -Clock $sw -Js $captureChartJsCode -RetryUsed ([ref]$retryUsed)

                if ($chartRes.err) {
                    $comboError = if ($sw.ElapsedMilliseconds -ge $comboTimeoutMs) { 'combo_timeout' } else { [string]$chartRes.err }
                    break
                }

                [void](Get-ComboRemaining -Clock $sw)
                $canvasSize = @([int]$chartRes.w, [int]$chartRes.h)
                $chartDataOk = [bool]$chartRes.chart_data_ok

                # Save PNG
                $shouldSavePng = ($SavePng -ne 0) -or (-not $chartDataOk)
                if ($shouldSavePng -and $chartRes.png -and $OutDir) {
                    $pngFile = "${baseName}.png"
                    $pngPath = Join-Path $OutDir $pngFile
                    $idx = $chartRes.png.IndexOf("base64,")
                    $b64 = if ($idx -ge 0) { $chartRes.png.Substring($idx + 7) } else { $chartRes.png }
                    $pngBytes = [Convert]::FromBase64String($b64)
                    [System.IO.File]::WriteAllBytes($pngPath, $pngBytes)
                    [void](Get-ComboRemaining -Clock $sw)
                    $pngSha256 = Get-Sha256Hex -FilePath $pngPath
                    [void](Get-ComboRemaining -Clock $sw)
                }

                # Save chart.json.gz if structured data available
                if ($chartDataOk -and $OutDir) {
                    $chartFile = "${baseName}.chart.json.gz"
                    $chartPath = Join-Path $OutDir $chartFile
                    $chartObj = @{
                        x = $chartRes.x
                        ybins = $chartRes.ybins
                        ymin = $chartRes.ymin
                        ymax = $chartRes.ymax
                        cells = $chartRes.cells
                        candles = $chartRes.candles
                        vmax = $chartRes.vmax
                        grid = $chartRes.grid
                        w = $chartRes.w
                        h = $chartRes.h
                    }
                    $chartJsonStr = ConvertTo-Json -InputObject $chartObj -Depth 12 -Compress
                    Compress-GzipData -Data $chartJsonStr -FilePath $chartPath
                    [void](Get-ComboRemaining -Clock $sw)
                    $chartSha256 = Get-Sha256Hex -FilePath $chartPath
                    [void](Get-ComboRemaining -Clock $sw)

                    $yMin = $chartRes.ymin
                    $yMax = $chartRes.ymax
                    $nX = ($chartRes.x | Measure-Object).Count
                    $nY = ($chartRes.ybins | Measure-Object).Count
                    if ($nX -gt 0) {
                        $xFirst = [string]$chartRes.x[0]
                        $xLast = [string]$chartRes.x[$nX - 1]
                    }
                    if ($chartRes.candles -and $chartRes.candles.Count -gt 0) {
                        $lastCandle = $chartRes.candles[$chartRes.candles.Count - 1]
                        if ($lastCandle -and $lastCandle.Count -ge 2) {
                            $price = [double]$lastCandle[1]
                        }
                    }
                }

                [void](Get-ComboRemaining -Clock $sw)
                $comboOk = $true
                break
            } catch {
                $isTimeout = ($_.Exception.Message -like "*combo_timeout*") -or ($sw.ElapsedMilliseconds -ge $comboTimeoutMs)
                if ($isTimeout) {
                    $comboError = 'combo_timeout'
                    break
                }
                $remMs = Get-ComboRemaining -Clock $sw
                if ($attempt -eq 0 -and -not $retryUsed -and $remMs -gt 9500) {
                    $retryUsed = $true
                    try {
                        Invoke-CdpCall -Sess $sess -Method 'Page.reload' -TimeoutMs 5000 -Clock $sw | Out-Null
                        Wait-Combo -Clock $sw -Ms 4000
                    } catch {}
                    continue
                }
                $comboError = $_.Exception.Message
                break
            }
        }

        # A failed or overdue combo publishes no staged output references.
        if ($sw.ElapsedMilliseconds -ge $comboTimeoutMs) {
            $comboOk = $false
            $comboError = 'combo_timeout'
        }
        if (-not $comboOk) {
            $pngFile = $null
            $pngSha256 = $null
            $chartFile = $null
            $chartSha256 = $null
        }
        $sw.Stop()
        $results += @{
            coin = $coin
            mode = $mode
            timeframe = $timeframe
            model = $model
            ok = $comboOk
            error = $comboError
            login_required = $loginRequired
            header = $header
            price = $price
            y_min = $yMin
            y_max = $yMax
            n_x = $nX
            n_y = $nY
            x_first = $xFirst
            x_last = $xLast
            canvas = $canvasSize
            png_file = $pngFile
            png_sha256 = $pngSha256
            chart_file = $chartFile
            chart_sha256 = $chartSha256
            chart_data_ok = $chartDataOk
            elapsed_ms = [int]$sw.ElapsedMilliseconds
        }
        # Includes per-combo bookkeeping, not just CDP work.
        if ($sw.ElapsedMilliseconds -ge $comboTimeoutMs) {
            $results[-1].ok = $false
            $results[-1].error = 'combo_timeout'
            $results[-1].png_file = $null
            $results[-1].chart_file = $null
        }
    }

    $allOk = $true
    foreach ($r in $results) {
        if (-not $r.ok) {
            $allOk = $false
            break
        }
    }
    $finalStatus = if ($allOk) { 'ok' } else { 'partial' }

    Send-JsonAndExit @{
        schema = 1
        status = $finalStatus
        chrome = @{ version = $browserVersion; port = 19301; owner_ok = $true }
        page = @{ title = $pageTitle; url = $pageUrl; tabs = $allTabs }
        logged_in = $loggedIn
        login_evidence = $loginEvidence
        login_wait_ms = $loginWaitMs
        visibility = $pageVisibility
        focus_mode = $focusMode
        warnings = $captureWarnings
        viewport = @(1400, 1200)
        browser_tz_offset_min = $tzOffset
        results = $results
    }

} catch {
    Send-JsonAndExit @{
        schema = 1
        status = 'powershell_error'
        chrome = @{ version = ''; port = 19301; owner_ok = $true }
        page = @{ title = ''; url = ''; tabs = @() }
        logged_in = 'unknown'
        visibility = 'unknown'
        warnings = @()
        viewport = @(1400, 1200)
        results = @()
        error = $_.Exception.Message
    }
} finally {
    if ($sess -and $sessConnected) {
        try {
            $unfocusPayload = @{ id = 999998; method = 'Emulation.setFocusEmulationEnabled'; params = @{ enabled = $false } } | ConvertTo-Json -Compress
            $sess.Call($unfocusPayload, 999998) | Out-Null
        } catch {}
        try {
            $clearPayload = @{ id = 999999; method = 'Emulation.clearDeviceMetricsOverride'; params = @{} } | ConvertTo-Json -Compress
            $sess.Call($clearPayload, 999999) | Out-Null
        } catch {}
        $sess.Dispose()
    }
}
