<#
  quote_server.ps1 —— 本地摘录服务（带 AI 简短回应）
  在 calibre 阅读器右侧「查找」面板里加载录入表单；写下想法后可以让 DeepSeek 回一句（默认 50 字以内），
  一起写进 Markdown 笔记。只监听 127.0.0.1，不对外网开放。

  DeepSeek Key 的读取顺序：
    1) 环境变量 DEEPSEEK_API_KEY
    2) 本目录 quote_settings.json 里的 deepseek_api_key
    3) %USERPROFILE%\.dsh\deepseek_api_key.txt（纯文本一行）

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File quote_server.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File quote_server.ps1 -Test
#>
param(
    [int]$Port = 8765,
    [string]$NotePath = '',
    [string]$SettingsPath = '',
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
$script:Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SettingsPath) { $SettingsPath = Join-Path $script:Root 'quote_settings.json' }

function Get-DefaultNotePath {
    $docs = [Environment]::GetFolderPath('MyDocuments')
    if (-not $docs) { $docs = Join-Path $env:USERPROFILE 'Documents' }
    return (Join-Path $docs '摘录笔记.md')
}

function Get-Settings {
    $s = @{
        note_path = (Get-DefaultNotePath)
        last_book = ''
        last_loc = ''
        ai_base_url = 'https://api.deepseek.com'
        ai_model = 'deepseek-chat'
        ai_max_chars = 50
        ai_timeout = 45
        deepseek_api_key = ''
    }
    if (Test-Path -LiteralPath $SettingsPath) {
        try {
            $raw = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($raw.note_path) { $s.note_path = [string]$raw.note_path }
            if ($raw.last_book) { $s.last_book = [string]$raw.last_book }
            if ($raw.last_loc) { $s.last_loc = [string]$raw.last_loc }
            if ($raw.ai_base_url) { $s.ai_base_url = [string]$raw.ai_base_url }
            if ($raw.ai_model) { $s.ai_model = [string]$raw.ai_model }
            if ($raw.ai_max_chars) { $s.ai_max_chars = [int]$raw.ai_max_chars }
            if ($raw.ai_timeout) { $s.ai_timeout = [int]$raw.ai_timeout }
            if ($raw.deepseek_api_key) { $s.deepseek_api_key = [string]$raw.deepseek_api_key }
            if (-not $raw.last_book -and $raw.last_source) {
                $parts = ([string]$raw.last_source) -split ' · ', 2
                $s.last_book = $parts[0]
                if ($parts.Count -gt 1) { $s.last_loc = $parts[1] }
            }
        } catch { }
    }
    if ($NotePath) { $s.note_path = $NotePath }
    return $s
}

function Save-Settings($s) {
    try {
        $obj = [pscustomobject]@{
            note_path = $s.note_path
            last_book = $s.last_book
            last_loc = $s.last_loc
            ai_base_url = $s.ai_base_url
            ai_model = $s.ai_model
            ai_max_chars = $s.ai_max_chars
            ai_timeout = $s.ai_timeout
            deepseek_api_key = $s.deepseek_api_key
        }
        $json = $obj | ConvertTo-Json
        [System.IO.File]::WriteAllText($SettingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Get-ApiKey($settings) {
    if ($env:DEEPSEEK_API_KEY) { return $env:DEEPSEEK_API_KEY }
    if ($settings.deepseek_api_key) { return $settings.deepseek_api_key }
    $alt = Join-Path $env:USERPROFILE '.dsh\deepseek_api_key.txt'
    if (Test-Path -LiteralPath $alt) {
        try { return ((Get-Content -LiteralPath $alt -Raw -Encoding UTF8).Trim()) } catch { }
    }
    return ''
}

function Get-ReadingNow {
    $vp = Join-Path $env:APPDATA 'calibre\viewer-webengine.json'
    if (-not (Test-Path -LiteralPath $vp)) { return $null }
    try {
        $j = [System.IO.File]::ReadAllText($vp, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $items = @($j.session_data.standalone_recently_opened)
        if ($items.Count -eq 0) { return $null }
        $top = $items[0]
        if (-not $top.title) { return $null }
        $authors = ''
        if ($top.authors) { $authors = (@($top.authors) -join '、') }
        return [pscustomobject]@{
            Title   = [string]$top.title
            Authors = $authors
            Path    = [string]$top.pathtoebook
            When    = [string]$top.timestamp
        }
    } catch {
        return $null
    }
}

function Get-KnownBooks([string]$notePath, [int]$limit = 15) {
    $books = New-Object System.Collections.ArrayList
    try {
        if (Test-Path -LiteralPath $notePath) {
            foreach ($line in [System.IO.File]::ReadLines($notePath)) {
                if ($line -match '^\*\*出处\*\*：(.*)$') {
                    $book = ($Matches[1].Trim() -split ' · ', 2)[0].Trim()
                    if ($book -and -not $books.Contains($book)) { [void]$books.Add($book) }
                }
            }
        }
    } catch { }
    $arr = @($books)
    if ($arr.Count -gt $limit) { $arr = $arr[($arr.Count - $limit)..($arr.Count - 1)] }
    return $arr
}

function HtmlEncode([string]$t) {
    if ($null -eq $t) { return '' }
    return $t.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;').Replace('"', '&quot;')
}

function UrlDecode([string]$s) {
    if ($null -eq $s) { return '' }
    return [System.Uri]::UnescapeDataString($s.Replace('+', ' '))
}

function Parse-Query([string]$q) {
    $d = @{}
    if (-not $q) { return $d }
    foreach ($pair in $q.Split('&')) {
        if (-not $pair) { continue }
        $kv = $pair.Split('=', 2)
        $k = UrlDecode $kv[0]
        $v = ''
        if ($kv.Count -gt 1) { $v = UrlDecode $kv[1] }
        $d[$k] = $v
    }
    return $d
}

function Format-Source([string]$book, [string]$loc, [string]$fallback) {
    $b = $book.Trim()
    $l = $loc.Trim()
    if ($b -and $l) { return "$b · $l" }
    if ($b) { return $b }
    return $fallback.Trim()
}

function Format-Entry([string]$quote, [string]$source, [string]$thoughts, [string]$aiReply, [int]$aiMax) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('---')
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('## ' + (Get-Date -Format 'yyyy-MM-dd HH:mm'))
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('**原文**')
    [void]$sb.AppendLine('')
    foreach ($line in ($quote -split "`r?`n")) {
        [void]$sb.AppendLine('> ' + $line)
    }
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('**出处**：' + $source)
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('**想法**')
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine($thoughts)
    if ($aiReply -and $aiReply.Trim()) {
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine("**AI 回应**（${aiMax} 字以内）")
        [void]$sb.AppendLine('')
        [void]$sb.AppendLine($aiReply.Trim())
    }
    [void]$sb.AppendLine('')
    return $sb.ToString()
}

function Add-Entry([string]$path, [string]$text) {
    $dir = Split-Path -Parent $path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $enc = New-Object System.Text.UTF8Encoding($false)
    if (-not (Test-Path -LiteralPath $path)) {
        [System.IO.File]::WriteAllText($path, "# 摘录笔记`r`n", $enc)
    }
    [System.IO.File]::AppendAllText($path, $text, $enc)
    return (Resolve-Path -LiteralPath $path).Path
}

function Invoke-AI($settings, [string]$quote, [string]$source, [string]$thoughts, [int]$maxChars) {
    $key = Get-ApiKey $settings
    if (-not $key) { throw '还没有配置 DeepSeek API Key：打开 http://127.0.0.1:' + $port + '/setup 填一下，或改 quote_settings.json 的 deepseek_api_key 后重启服务' }
    $n = $maxChars
    if ($n -lt 10) { $n = 10 }
    if ($n -gt 400) { $n = 400 }
    $system = "你是一位严格的读书同伴。用户正在读一本书，写下了一段摘录和自己的想法。你的任务是只针对「想法」给出简短回应：" +
        "如果想法有事实错误、明显偏颇、逻辑跳跃或过于笼统，直接指出并给出更准确的说法；如果想法站得住，就补一个它没考虑到的角度。" +
        "不要复述原文，不要客套，不要分点，不要说「你的想法很好」。用中文，控制在 $n 个字以内，直接给结论。"
    $user = "出处：$source`n原文：$quote`n我的想法：$thoughts"
    $body = @{
        model = $settings.ai_model
        messages = @(
            @{ role = 'system'; content = $system },
            @{ role = 'user'; content = $user }
        )
        temperature = 0.7
        max_tokens = 400
        stream = $false
    } | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $headers = @{ Authorization = "Bearer $key"; 'Content-Type' = 'application/json' }
    $uri = ($settings.ai_base_url.TrimEnd('/')) + '/chat/completions'
    try {
        $proto = [System.Net.SecurityProtocolType]::Tls12
        try { $proto = $proto -bor [System.Net.SecurityProtocolType]::Tls13 } catch { }
        [System.Net.ServicePointManager]::SecurityProtocol = $proto
    } catch { }
    try {
        $web = Invoke-WebRequest -Uri $uri -Method Post -Headers $headers -Body $bytes -TimeoutSec ([int]$settings.ai_timeout) -UseBasicParsing
        $raw = $web.RawContentStream.ToArray()
        $jsonText = [System.Text.Encoding]::UTF8.GetString($raw)
        $resp = $jsonText | ConvertFrom-Json
    } catch {
        $msg = $_.Exception.Message
        try {
            $res = $_.Exception.Response
            if ($res) {
                $sr = New-Object System.IO.StreamReader($res.GetResponseStream())
                $detail = $sr.ReadToEnd()
                if ($detail) { $msg = 'HTTP ' + [int]$res.StatusCode + '：' + $detail }
            }
        } catch { }
        throw $msg
    }
    $text = $resp.choices[0].message.content
    if (-not $text) { throw 'AI 返回了空内容' }
    $text = $text.Trim()
    if ($text.Length -gt ($n + 10)) { $text = $text.Substring(0, $n) + '…' }
    return $text
}

$script:Css = @'
*{box-sizing:border-box}
body{margin:0;padding:10px 12px 18px;font:13px/1.5 "Segoe UI","Microsoft YaHei",sans-serif;color:#222}
h1{font-size:14px;margin:0 0 10px;font-weight:600}
label{display:block;font-size:12px;color:#555;margin:10px 0 3px}
textarea,input,select{width:100%;font:inherit;color:inherit;border:1px solid #c8c8c8;border-radius:4px;padding:6px;background:#fff}
textarea{resize:vertical}
.q{height:140px}.t{height:96px}
.two{display:flex;gap:8px}
.two > div{flex:1}
.row{display:flex;gap:8px;margin-top:14px}
.row.tight{margin-top:10px}
button{font:inherit;padding:8px 10px;border-radius:4px;border:1px solid #c8c8c8;background:#f6f6f6;cursor:pointer}
button.primary{background:#2a6fdb;border-color:#2a6fdb;color:#fff;flex:1.4}
button.ai{background:#8a5cf5;border-color:#8a5cf5;color:#fff;flex:1}
button.ghost{flex:0 0 auto}
.ai-block{margin-top:12px;border:1px solid #d8cffa;border-radius:6px;padding:8px 10px;background:#faf8ff}
.ai-block .hint{font-size:11px;color:#7a6bb5;margin-bottom:4px}
.ai-text{font-size:13px;white-space:pre-wrap}
.warn{margin-top:10px;font-size:12px;color:#a35a00;background:#fff6e6;border:1px solid #f0d9ae;border-radius:4px;padding:6px 8px}
.path{margin-top:10px;font-size:11px;color:#888;word-break:break-all}
.ok{font-size:14px;padding:14px 0}
.muted{color:#888;font-size:12px}
.chk{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:12px;color:#444}
.chk input{width:auto}
'@

$script:FormPage = @'
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摘录到笔记</title><style>__CSS__</style></head><body>
<h1>摘录到笔记</h1>
<form method="post" action="/save">
  <label for="quote">原文</label>
  <textarea id="quote" name="quote" class="q">__QUOTE__</textarea>
  <div class="two">
    <div>
      <label for="book">书名</label>
      <input id="book" name="book" list="books" value="__BOOK__" placeholder="文学回忆录 上">
      <datalist id="books">__BOOKS__</datalist>
    </div>
    <div>
      <label for="loc">位置</label>
      <input id="loc" name="loc" value="__LOC__" placeholder="p.23 / 第三讲">
    </div>
  </div>
  <div class="path">__NOW__</div>
  <label for="thoughts">想法</label>
  <textarea id="thoughts" name="thoughts" class="t" placeholder="当时想到什么…">__THOUGHTS__</textarea>
  <div class="row tight">
    <button type="submit" formaction="/save" class="primary">保存并追加</button>
    <button type="submit" formaction="/discuss" class="ai">让 AI 回一句</button>
  </div>
  <div class="row tight">
    <div style="flex:0 0 90px">
      <label for="max_chars">字数上限</label>
      <input id="max_chars" name="max_chars" value="__MAXCHARS__">
    </div>
    <div style="flex:0 0 auto;align-self:flex-end">
      <button type="button" class="ghost" onclick="document.getElementById('thoughts').value=''">清空想法</button>
    </div>
  </div>
  __AIBLOCK__
  __WARN__
</form>
<div class="path">写入：__PATH__</div>
<script>
document.getElementById('thoughts').addEventListener('keydown', function(e){
  if (e.key==='Enter' && (e.ctrlKey||e.metaKey)) { e.preventDefault(); this.form.submit(); }
});
</script>
</body></html>
'@

$script:SavedPage = @'
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>已保存</title><style>__CSS__</style></head><body>
<h1>已保存</h1>
<div class="ok">这条摘录已经追加到笔记文件。</div>
__AIBLOCK__
<div class="path">__PATH__</div>
<div class="muted" style="margin-top:14px">选中下一段文字，右侧面板会重新载入录入表单。</div>
</body></html>
'@

$script:ErrorPage = @'
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>出错了</title><style>__CSS__</style></head><body>
<h1>出错了</h1>
<pre style="white-space:pre-wrap;font-size:12px">__ERROR__</pre>
<div class="muted" style="margin-top:12px">回到上一页可继续编辑。</div>
</body></html>
'@

$script:SetupPage = @'
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摘录服务设置</title><style>__CSS__</style></head><body>
<h1>摘录服务设置</h1>
__SAVED__
<form method="post" action="/setup">
  <label for="key">DeepSeek API Key</label>
  <input id="key" name="deepseek_api_key" value="__KEY__" placeholder="sk-...">
  <label for="note">笔记文件</label>
  <input id="note" name="note_path" value="__NOTE__">
  <label for="chars">AI 回应字数上限</label>
  <input id="chars" name="ai_max_chars" value="__CHARS__">
  <div class="row">
    <button type="submit" class="primary">保存设置</button>
    <button type="button" onclick="location.href='/quote'">回到录入表单</button>
  </div>
</form>
<div class="path">Key 只存在本机的 quote_settings.json 里，不会上传到别处（除了你自己配置的 API 端点）。</div>
</body></html>
'@

function Render([string]$tpl, $map) {    $html = $tpl.Replace('__CSS__', $script:Css)
    foreach ($k in $map.Keys) { $html = $html.Replace($k, [string]$map[$k]) }
    return $html
}

function Send-Response($stream, [string]$html) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($html)
    $head = "HTTP/1.1 200 OK`r`nContent-Type: text/html; charset=utf-8`r`nContent-Length: $($bytes.Length)`r`nCache-Control: no-store`r`nConnection: close`r`n`r`n"
    $hb = [System.Text.Encoding]::ASCII.GetBytes($head)
    $stream.Write($hb, 0, $hb.Length)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
}

function Build-AiBlock([string]$reply, [int]$maxChars) {
    if (-not $reply -or -not $reply.Trim()) { return '' }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('<div class="ai-block">')
    [void]$sb.AppendLine('<div class="hint">AI 回应 · 上限 ' + $maxChars + ' 字（可直接改，也可取消勾选不写入）</div>')
    [void]$sb.AppendLine('<div class="ai-text">' + (HtmlEncode $reply.Trim()) + '</div>')
    [void]$sb.AppendLine('<label class="chk"><input type="checkbox" name="include_ai" value="1" checked> 把这段 AI 回应一起写进笔记</label>')
    [void]$sb.AppendLine('<input type="hidden" name="ai_reply" value="' + (HtmlEncode $reply.Trim()) + '">')
    [void]$sb.AppendLine('</div>')
    return $sb.ToString()
}

function Handle-Request($client) {
    $stream = $client.GetStream()
    $client.ReceiveTimeout = 15000
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
    $requestLine = $reader.ReadLine()
    if (-not $requestLine) { return }
    $contentLength = 0
    while ($true) {
        $line = $reader.ReadLine()
        if ($null -eq $line -or $line -eq '') { break }
        if ($line -match '^(?i)content-length:\s*(\d+)') { $contentLength = [int]$Matches[1] }
    }
    $parts = $requestLine.Split(' ')
    $method = $parts[0]
    $target = $parts[1]
    $body = ''
    if ($contentLength -gt 0) {
        $buf = New-Object char[] $contentLength
        $read = 0
        while ($read -lt $contentLength) {
            $n = $reader.Read($buf, $read, $contentLength - $read)
            if ($n -le 0) { break }
            $read += $n
        }
        if ($read -gt 0) { $body = -join $buf[0..($read - 1)] }
    }
    $settings = Get-Settings
    $path = $target
    $query = ''
    $qi = $target.IndexOf('?')
    if ($qi -ge 0) { $path = $target.Substring(0, $qi); $query = $target.Substring($qi + 1) }

    $f = @{}
    if ($method -eq 'POST') { $f = Parse-Query $body }

    $quote = ''; if ($f.ContainsKey('quote')) { $quote = $f['quote'] }
    $book = ''; if ($f.ContainsKey('book')) { $book = $f['book'] }
    $loc = ''; if ($f.ContainsKey('loc')) { $loc = $f['loc'] }
    $thoughts = ''; if ($f.ContainsKey('thoughts')) { $thoughts = $f['thoughts'] }
    $maxChars = [int]$settings.ai_max_chars
    if ($f.ContainsKey('max_chars') -and $f['max_chars']) {
        $tmp = 0
        if ([int]::TryParse($f['max_chars'], [ref]$tmp) -and $tmp -gt 0) { $maxChars = $tmp }
    }
    $aiReply = ''
    if ($f.ContainsKey('ai_reply')) { $aiReply = $f['ai_reply'] }

    if ($path -eq '/save' -and $method -eq 'POST') {
        $includeAi = $f.ContainsKey('include_ai')
        if (-not $includeAi) { $aiReply = '' }
        if ($quote.Trim() -eq '') {
            Send-Response $stream (Render $script:ErrorPage @{ '__ERROR__' = (HtmlEncode '原文是空的，没有写入。') })
            return
        }
        try {
            $source = Format-Source $book $loc ''
            $entry = Format-Entry $quote $source $thoughts $aiReply $maxChars
            $written = Add-Entry $settings.note_path $entry
            $settings.last_book = $book
            $settings.last_loc = $loc
            $settings.ai_max_chars = $maxChars
            Save-Settings $settings
            Send-Response $stream (Render $script:SavedPage @{
                '__PATH__' = (HtmlEncode $written)
                '__AIBLOCK__' = (Build-AiBlock $aiReply $maxChars)
            })
        } catch {
            Send-Response $stream (Render $script:ErrorPage @{ '__ERROR__' = (HtmlEncode ($_.Exception.Message)) })
        }
        return
    }

    if ($path -eq '/discuss' -and $method -eq 'POST') {
        $aiReply = ''
        $warn = ''
        $source = Format-Source $book $loc ''
        if ($thoughts.Trim() -eq '') {
            $warn = '<div class="warn">先写一句想法，AI 才有东西可回。</div>'
        } elseif (-not (Get-ApiKey $settings)) {
            $warn = '<div class="warn">还没有配置 DeepSeek API Key：<a href="/setup">点这里填</a>（或改 quote_settings.json 的 deepseek_api_key 后重启服务）。</div>'
        } else {
            try {
                $aiReply = Invoke-AI $settings $quote $source $thoughts $maxChars
            } catch {
                $warn = '<div class="warn">AI 调用失败：' + (HtmlEncode $_.Exception.Message) + '</div>'
            }
        }
        $opts = ''
        foreach ($b in (Get-KnownBooks $settings.note_path)) {
            $opts += '<option value="' + (HtmlEncode $b) + '">'
        }
        Send-Response $stream (Render $script:FormPage @{
            '__QUOTE__' = (HtmlEncode $quote)
            '__BOOK__' = (HtmlEncode $book)
            '__LOC__' = (HtmlEncode $loc)
            '__THOUGHTS__' = (HtmlEncode $thoughts)
            '__MAXCHARS__' = (HtmlEncode ([string]$maxChars))
            '__BOOKS__' = $opts
            '__NOW__' = ''
            '__AIBLOCK__' = (Build-AiBlock $aiReply $maxChars)
            '__WARN__' = $warn
            '__PATH__' = (HtmlEncode $settings.note_path)
        })
        return
    }

    if ($path -eq '/setup') {
        $saved = ''
        if ($method -eq 'POST' -and $f.Keys.Count -gt 0) {
            if ($f.ContainsKey('deepseek_api_key')) { $settings.deepseek_api_key = $f['deepseek_api_key'].Trim() }
            if ($f.ContainsKey('note_path') -and $f['note_path'].Trim()) { $settings.note_path = $f['note_path'].Trim() }
            if ($f.ContainsKey('ai_max_chars') -and $f['ai_max_chars']) {
                $tmp = 0
                if ([int]::TryParse($f['ai_max_chars'], [ref]$tmp) -and $tmp -gt 0) { $settings.ai_max_chars = $tmp }
            }
            Save-Settings $settings
            $saved = '<div class="warn" style="background:#eaf7ee;border-color:#bfe3c8;color:#1d6b32">已保存。</div>'
        }
        Send-Response $stream (Render $script:SetupPage @{
            '__SAVED__' = $saved
            '__KEY__' = (HtmlEncode $settings.deepseek_api_key)
            '__NOTE__' = (HtmlEncode $settings.note_path)
            '__CHARS__' = (HtmlEncode ([string]$settings.ai_max_chars))
        })
        return
    }

    $q = Parse-Query $query
    $text = ''
    if ($q.ContainsKey('text')) { $text = $q['text'] }
    elseif ($q.ContainsKey('word')) { $text = $q['word'] }
    $bookValue = $settings.last_book
    if ($q.ContainsKey('book')) { $bookValue = $q['book'] }
    $nowText = ''
    $now = Get-ReadingNow
    if ($now) {
        if (-not $bookValue -or -not $q.ContainsKey('book')) { $bookValue = $now.Title }
        $nowText = '阅读器里正在读：' + $now.Title
        if ($now.Authors) { $nowText += ' · ' + $now.Authors }
        $nowText = HtmlEncode $nowText
    }
    $locValue = $settings.last_loc
    if ($q.ContainsKey('loc')) { $locValue = $q['loc'] }
    $opts = ''
    foreach ($b in (Get-KnownBooks $settings.note_path)) {
        $opts += '<option value="' + (HtmlEncode $b) + '">'
    }
    $warn = ''
    if (-not (Get-ApiKey $settings)) {
        $warn = '<div class="warn">AI 回应还没启用：<a href="/setup">点这里填 DeepSeek API Key</a>，填完刷新本页。</div>'
    }
    Send-Response $stream (Render $script:FormPage @{
        '__QUOTE__' = (HtmlEncode $text)
        '__BOOK__' = (HtmlEncode $bookValue)
        '__LOC__' = (HtmlEncode $locValue)
        '__THOUGHTS__' = ''
        '__MAXCHARS__' = (HtmlEncode ([string]$settings.ai_max_chars))
        '__BOOKS__' = $opts
        '__NOW__' = $nowText
        '__AIBLOCK__' = ''
        '__WARN__' = $warn
        '__PATH__' = (HtmlEncode $settings.note_path)
    })
}

if ($Test) {
    $s = Get-Settings
    $now = Get-ReadingNow
    if ($now) { Write-Output ('READING=' + $now.Title + ' / ' + $now.Authors + ' / ' + $now.When) } else { Write-Output 'READING=' }
    $entry = Format-Entry '第一行原文' '《测试书》 · p.1' '测试想法' '这句是 AI 的回应。' 50
    $tmp = Join-Path $env:TEMP ('quote_selftest_' + [guid]::NewGuid().ToString('N') + '.md')
    $written = Add-Entry $tmp $entry
    Add-Entry $written (Format-Entry '第二条' '书 · p.9' '想法二' '' 50) | Out-Null
    $content = Get-Content -LiteralPath $written -Raw -Encoding UTF8
    Write-Output "TEST_OK path=$written len=$($content.Length)"
    Write-Output ('HAS_AI_SECTION=' + ($content -match '\*\*AI 回应\*\*（50 字以内）'))
    Write-Output ('SECOND_HAS_AI=' + ($content -match '想法二\r?\n\r?\n---'))
    Write-Output ('KEY_PRESENT=' + [bool](Get-ApiKey $s))
    Write-Output ('SOURCE=' + (Format-Source '文学回忆录 上' 'p.23' ''))
    Write-Output '--- note preview ---'
    Write-Output $content
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    return
}

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
try {
    $listener.Start()
} catch {
    Write-Output "START_FAIL: $($_.Exception.Message)"
    exit 1
}
$logFile = Join-Path $env:TEMP 'quote_server.log'
try {
    $s0 = Get-Settings
    "[$(Get-Date -Format 'HH:mm:ss')] listening on http://127.0.0.1:$Port/  note=$($s0.note_path)  ai_key=$([bool](Get-ApiKey $s0))" | Add-Content -LiteralPath $logFile -Encoding UTF8
} catch { }
while ($true) {
    $client = $null
    try {
        $client = $listener.AcceptTcpClient()
        Handle-Request $client
    } catch {
        try { "[$(Get-Date -Format 'HH:mm:ss')] error: $($_.Exception.Message)" | Add-Content -LiteralPath $logFile -Encoding UTF8 } catch { }
    } finally {
        if ($client) { try { $client.Close() } catch { } }
    }
}
