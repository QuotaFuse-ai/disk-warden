# Package acceptance drills (T15).
#
# Runs the frozen DiskWarden.exe itself in a throw-away sandbox and checks
# scan -> plan -> migrate -> verify -> rollback, the two force-kill crash
# points with recovery, log rotation, the startup item and the tray
# self-test.  Every step writes a JSON report under the sandbox; the script
# prints a table and exits non-zero when any step fails.
#
# Nothing here touches the real %APPDATA% or the real data root: the drills
# redirect APPDATA/LOCALAPPDATA/USERPROFILE into the sandbox, and the tray
# step runs with DISKWARDEN_DATA_ROOT set to a sandbox folder.
#
# Windows PowerShell 5.1 compatible (no ternary, no ??).

param(
    [string]$App = (Join-Path $PSScriptRoot '..\dist\DiskWarden\DiskWarden.exe'),
    [string]$Sandbox = (Join-Path $env:SystemDrive 'DiskWardenSandbox\t15'),
    [switch]$SkipTray
)

$ErrorActionPreference = 'Stop'

$App = [System.IO.Path]::GetFullPath($App)
$Sandbox = [System.IO.Path]::GetFullPath($Sandbox)
if (-not (Test-Path $App)) {
    throw "packaged executable not found: $App"
}
New-Item -ItemType Directory -Force -Path $Sandbox | Out-Null

$results = New-Object System.Collections.ArrayList

function Add-Result([string]$Step, [bool]$Ok, [string]$Detail) {
    [void]$results.Add([pscustomobject]@{ Step = $Step; Ok = $Ok; Detail = $Detail })
    $tag = 'FAIL'
    if ($Ok) { $tag = 'ok' }
    Write-Host ("[{0}] {1} - {2}" -f $tag, $Step, $Detail)
}

function Read-Report([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Wait-Marker([string]$Path, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $Path) {
            try { return (Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { }
        }
        Start-Sleep -Milliseconds 200
    }
    return $null
}

function Invoke-Drill([string]$Scenario, [string]$Dir, [string[]]$Extra) {
    $arguments = @('--drill', $Scenario, '--sandbox', $Dir)
    if ($Extra) { $arguments += $Extra }
    $proc = Start-Process -FilePath $App -ArgumentList $arguments -PassThru
    if (-not $proc.WaitForExit(300000)) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        return $null
    }
    return (Read-Report (Join-Path $Dir ("drill-report-{0}.json" -f $Scenario)))
}

function Invoke-Crash([string]$Point) {
    $dir = Join-Path $Sandbox ("crash-" + $Point)
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $markerPath = Join-Path $dir 'crash-marker.json'

    $proc = Start-Process -FilePath $App -ArgumentList @(
        '--drill', 'crash-prepare', '--crash-point', $Point, '--sandbox', $dir
    ) -PassThru
    $marker = Wait-Marker $markerPath 240
    if (-not $marker) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Add-Result ("crash {0}: freeze marker" -f $Point) $false 'marker was not written'
        return
    }
    try {
        Stop-Process -Id ([int]$marker.pid) -Force
    } catch {
        Add-Result ("crash {0}: force kill" -f $Point) $false $_.Exception.Message
        return
    }
    if (-not $proc.WaitForExit(60000)) {
        Add-Result ("crash {0}: force kill" -f $Point) $false 'process survived taskkill'
        return
    }
    $left = Get-Process -Id ([int]$marker.pid) -ErrorAction SilentlyContinue
    if ($left) {
        Add-Result ("crash {0}: force kill" -f $Point) $false 'process still listed'
        return
    }
    Add-Result ("crash {0}: force kill" -f $Point) $true ("pid {0}, exit {1}" -f $marker.pid, $proc.ExitCode)

    $report = Invoke-Drill 'crash-recover' $dir $null
    if (-not $report) {
        Add-Result ("crash {0}: recovery" -f $Point) $false 'no report'
        return
    }
    Add-Result ("crash {0}: recovery" -f $Point) ([bool]$report.ok) (Join-Path $dir 'drill-report-crash-recover.json')
}

# -- plain drills --------------------------------------------------------

foreach ($scenario in @('migrate-rollback', 'settings', 'autostart')) {
    $dir = Join-Path $Sandbox $scenario
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $report = Invoke-Drill $scenario $dir $null
    if (-not $report) {
        Add-Result $scenario $false 'no report'
    } else {
        Add-Result $scenario ([bool]$report.ok) (Join-Path $dir ("drill-report-{0}.json" -f $scenario))
        if (-not $report.ok) { Write-Host ("       error: {0}" -f $report.error) }
    }
}

# -- force-kill crash points --------------------------------------------

Invoke-Crash 'before-swap'
Invoke-Crash 'after-rename'

# -- tray self-test ------------------------------------------------------

if (-not $SkipTray) {
    $trayRoot = Join-Path $Sandbox 'tray'
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $trayRoot
    $env:DISKWARDEN_DATA_ROOT = $trayRoot
    $env:DISKWARDEN_TRAY_SELFTEST_MS = '1200'
    try {
        $proc = Start-Process -FilePath $App -ArgumentList @('--hidden') -PassThru
        $exited = $proc.WaitForExit(240000)
    } finally {
        Remove-Item Env:DISKWARDEN_DATA_ROOT -ErrorAction SilentlyContinue
        Remove-Item Env:DISKWARDEN_TRAY_SELFTEST_MS -ErrorAction SilentlyContinue
    }
    if (-not $exited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Add-Result 'tray self-test' $false 'window did not exit within 240s'
    } else {
        $appLog = Join-Path $trayRoot 'logs\app.log'
        $text = ''
        if (Test-Path $appLog) { $text = Get-Content $appLog -Raw -Encoding UTF8 }
        $steps = @('step: show', 'step: hide', 'step: quit')
        $missing = @()
        foreach ($step in $steps) {
            if ($text -notmatch [regex]::Escape($step)) { $missing += $step }
        }
        # Only this package counts: an older install elsewhere on the machine
        # is not a residual process of this test.
        $leftover = @(
            Get-CimInstance Win32_Process -Filter "Name='DiskWarden.exe'" |
                Where-Object { $_.ExecutablePath -eq $App }
        )
        $ok = ($proc.ExitCode -eq 0) -and ($missing.Count -eq 0) -and ($leftover.Count -eq 0)
        $detail = "exit {0}" -f $proc.ExitCode
        if ($missing.Count -gt 0) { $detail += "; missing log steps: " + ($missing -join ', ') }
        if ($leftover) { $detail += '; residual DiskWarden process' }
        Add-Result 'tray self-test' $ok $detail
    }
}

# -- summary -------------------------------------------------------------

Write-Host ''
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { -not $_.Ok })
if ($failed.Count -gt 0) {
    Write-Host ("{0} step(s) failed; reports are under {1}" -f $failed.Count, $Sandbox)
    exit 1
}
Write-Host ("all {0} steps passed; reports are under {1}" -f $results.Count, $Sandbox)
exit 0
