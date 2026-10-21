param(
    [string]$ConfigFile,
    [string]$OutDir,
    [int]$Port = 19301,
    [int]$SavePng = 1,
    [switch]$DryRun
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
    CancellationTokenSource ct = new CancellationTokenSource(180000);
    string ep = "";
    public void Open(string endpoint) {
        ep = endpoint;
        Connect();
    }
    private void Connect() {
        if (ws != null) {
            try { ws.Dispose(); } catch {}
        }
        ws = new ClientWebSocket();
        ws.ConnectAsync(new Uri(ep), ct.Token).GetAwaiter().GetResult();
    }
    public void EnsureOpen() {
        if (ws == null || ws.State != WebSocketState.Open) {
            Connect();
        }
    }
    public string Call(string payload, int id, int timeoutMs = 60000) {
        EnsureOpen();
        using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct.Token)) {
            if (timeoutMs > 0) {
                cts.CancelAfter(timeoutMs);
            }
            try {
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
        try { ws.Dispose(); } catch {}
        try { ct.Dispose(); } catch {}
    }
}
'@
}

$reqIdCounter = 0

function Invoke-CdpCall {
    param($Sess, [string]$Method, $Params = @{}, [int]$TimeoutMs = 60000)
    $script:reqIdCounter++
    $id = $script:reqIdCounter
    $payload = @{
        id = $id
        method = $Method
        params = $Params
    } | ConvertTo-Json -Depth 15 -Compress
    $raw = $Sess.Call($payload, $id, $TimeoutMs)
    return ($raw | ConvertFrom-Json)
}

function Invoke-CdpEval {
    param($Sess, [string]$Expression, [switch]$AwaitPromise, [int]$TimeoutMs = 60000)
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
    $raw = $Sess.Call($payload, $id, $TimeoutMs)
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

function Compress-GzipData {
    param([string]$Data, [string]$FilePath)
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
            return JSON.stringify({ state: 'false', evidence: 'login_leaf', waited_ms: Date.now() - start });
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
        if (hasUserButton) {
            userMenuConsecutive++;
            if (userMenuConsecutive >= 2) {
                return JSON.stringify({ state: 'true', evidence: 'user_menu_button', waited_ms: Date.now() - start });
            }
        } else {
            userMenuConsecutive = 0;
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
        $comboDeadline = [System.DateTime]::UtcNow.AddMilliseconds($comboTimeoutMs)
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

        # Single retry loop (max 1 retry)
        for ($attempt = 0; $attempt -lt 2; $attempt++) {
            try {
                $remMs = [int]($comboDeadline - [System.DateTime]::UtcNow).TotalMilliseconds
                if ($remMs -le 0 -or $sw.ElapsedMilliseconds -ge $comboTimeoutMs) {
                    $comboError = 'combo_timeout'
                    break
                }
                $callTimeout = [Math]::Max(1000, [Math]::Min($remMs, $comboTimeoutMs))

                $switchExpr = "($driverJsCode)('$coin', $model, '$timeframe', '$mode')"
                $switchResJson = Invoke-CdpEval -Sess $sess -Expression $switchExpr -AwaitPromise -TimeoutMs $callTimeout
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
                    } -TimeoutMs 5000 | Out-Null
                    Invoke-CdpCall -Sess $sess -Method 'Input.dispatchKeyEvent' -Params @{
                        type = 'keyUp'
                        key = 'Escape'
                        code = 'Escape'
                        windowsVirtualKeyCode = 27
                        nativeVirtualKeyCode = 27
                    } -TimeoutMs 5000 | Out-Null

                    # Read back to verify dialog is closed
                    for ($k = 0; $k -lt 10; $k++) {
                        Start-Sleep -Milliseconds 100
                        $dlgOpen = Invoke-CdpEval -Sess $sess -Expression 'Boolean([...document.querySelectorAll("[role=dialog],.MuiModal-root")].find(d => /Log in to unlock/i.test(d.innerText || "")))' -TimeoutMs 2000
                        if (-not $dlgOpen) { break }
                    }
                    break
                }
                if ($switchRes.err) {
                    $remMs = [int]($comboDeadline - [System.DateTime]::UtcNow).TotalMilliseconds
                    if ($attempt -eq 0 -and $remMs -gt 6000) {
                        Invoke-CdpCall -Sess $sess -Method 'Page.reload' -TimeoutMs 5000 | Out-Null
                        Start-Sleep -Seconds 4
                        continue
                    }
                    $comboError = if ($sw.ElapsedMilliseconds -ge $comboTimeoutMs) { 'combo_timeout' } else { [string]$switchRes.err }
                    break
                }

                $header = [string]$switchRes.head
                $remMs = [int]($comboDeadline - [System.DateTime]::UtcNow).TotalMilliseconds
                if ($remMs -le 4000) {
                    $comboError = 'combo_timeout'
                    break
                }
                Start-Sleep -Seconds 4

                $remMs = [int]($comboDeadline - [System.DateTime]::UtcNow).TotalMilliseconds
                if ($remMs -le 0) {
                    $comboError = 'combo_timeout'
                    break
                }
                $callTimeout = [Math]::Max(1000, [Math]::Min($remMs, $comboTimeoutMs))

                # Capture chart data and canvas
                $chartResJson = Invoke-CdpEval -Sess $sess -Expression $captureChartJsCode -TimeoutMs $callTimeout
                $chartRes = $chartResJson | ConvertFrom-Json

                if ($chartRes.err) {
                    $comboError = if ($sw.ElapsedMilliseconds -ge $comboTimeoutMs) { 'combo_timeout' } else { [string]$chartRes.err }
                    break
                }

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
                    $pngSha256 = Get-Sha256Hex -FilePath $pngPath
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
                    $chartSha256 = Get-Sha256Hex -FilePath $chartPath

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

                $comboOk = $true
                break
            } catch {
                $isTimeout = ($_.Exception.Message -like "*combo_timeout*") -or ($sw.ElapsedMilliseconds -ge $comboTimeoutMs)
                if ($isTimeout) {
                    $comboError = 'combo_timeout'
                    break
                }
                $remMs = [int]($comboDeadline - [System.DateTime]::UtcNow).TotalMilliseconds
                if ($attempt -eq 0 -and $remMs -gt 6000) {
                    try {
                        Invoke-CdpCall -Sess $sess -Method 'Page.reload' -TimeoutMs 5000 | Out-Null
                        Start-Sleep -Seconds 4
                    } catch {}
                    continue
                }
                $comboError = $_.Exception.Message
                break
            }
        }

        # Safe cleanup for next combo
        if ($comboError -eq 'combo_timeout' -or (-not $comboOk)) {
            try {
                if ($sess) { $sess.EnsureOpen() }
            } catch {}
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
