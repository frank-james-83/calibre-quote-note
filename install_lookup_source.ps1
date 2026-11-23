<#
  安装/卸载「摘录到笔记」查词源
  作用：往 calibre 阅读器的配置里加一条搜索源，指向本机的摘录服务。
  必须在 calibre 阅读器完全关闭时运行（否则阅读器退出时会覆写配置）。

  用法：右键 → 使用 PowerShell 运行；或在 PowerShell 里执行
    powershell -NoProfile -ExecutionPolicy Bypass -File install_lookup_source.ps1
  卸载：
    powershell -NoProfile -ExecutionPolicy Bypass -File install_lookup_source.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [int]$Port = 8765,
    [string]$Name = '摘录到笔记'
)

$ErrorActionPreference = 'Stop'

$running = Get-Process ebook-viewer -ErrorAction SilentlyContinue
if ($running) {
    Write-Host '请先完全退出 calibre 阅读器（ebook-viewer 窗口全部关闭）再运行本脚本。' -ForegroundColor Yellow
    exit 1
}

$prefs = Join-Path $env:APPDATA 'calibre\viewer-webengine.json'
if (-not (Test-Path -LiteralPath $prefs)) {
    Write-Host "找不到 calibre 阅读器配置文件：$prefs" -ForegroundColor Yellow
    Write-Host '请先打开一次 calibre 阅读器，然后关闭，再运行本脚本。'
    exit 1
}

$backup = "$prefs.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
Copy-Item -LiteralPath $prefs -Destination $backup -Force
Write-Host "已备份原配置：$backup"

$json = Get-Content -LiteralPath $prefs -Raw -Encoding UTF8 | ConvertFrom-Json
$url = "http://127.0.0.1:$Port/quote?text={word}"

$current = @()
if ($json.PSObject.Properties.Name -contains 'lookup_locations' -and $json.lookup_locations) {
    $current = @($json.lookup_locations)
}
$kept = @($current | Where-Object { $_.name -ne $Name })

if ($Uninstall) {
    $next = $kept
} else {
    $next = $kept + @([pscustomobject]@{ name = $Name; url = $url })
}

if ($next.Count -eq 0) {
    $json.PSObject.Properties.Remove('lookup_locations')
} else {
    if ($json.PSObject.Properties.Name -contains 'lookup_locations') {
        $json.lookup_locations = $next
    } else {
        $json | Add-Member -NotePropertyName 'lookup_locations' -NotePropertyValue $next -Force
    }
    if (-not $Uninstall) {
        if ($json.PSObject.Properties.Name -contains 'lookup_location') {
            $json.lookup_location = $Name
        } else {
            $json | Add-Member -NotePropertyName 'lookup_location' -NotePropertyValue $Name -Force
        }
    }
}

$out = $json | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($prefs, $out, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
if ($Uninstall) {
    Write-Host '已移除「摘录到笔记」搜索源。' -ForegroundColor Green
} else {
    Write-Host "已加入搜索源「$Name」 → $url" -ForegroundColor Green
    Write-Host '下次打开阅读器，选中文字 → 按 L（或点「查找」）→ 在「在以下位置查找」里选「摘录到笔记」。'
}
Write-Host "当前查词源列表：$((@($next) | ForEach-Object { $_.name }) -join ' / ')"
