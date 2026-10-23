# -*- coding: utf-8 -*-
<#
  sync-relay.ps1 · 从 GitHub 拉中继产物到本机 relay/ 目录
  用法：
    .\sync-relay.ps1 -Repo owner/name
    .\sync-relay.ps1 -Repo owner/name -Check        # 只验新鲜度
    .\sync-relay.ps1 -Repo owner/name -ForceRaw     # 强制走 raw（调试用）

  为什么默认走 Contents API：
    2026-10-24 实测 raw.githubusercontent 的 Fastly 缓存**无视 ?t= 缓存参数**，
    刚 commit 完直接拉会拿到上一版（API 已是 1800 市场，raw 还返回 1200）。
    有保险箱令牌就用 API（5000/h），没有才回落 raw。
#>
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [string]$Branch = 'main',
  [string]$OutDir = '',
  [int]$MaxAgeMin = 90,
  [switch]$Check,
  [switch]$ForceRaw
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path $PSScriptRoot '' }
$OutDir = (Resolve-Path $OutDir).Path
$rawBase = "https://raw.githubusercontent.com/$Repo/$Branch/relay"

# 令牌（可选）：有则走 API 取真值
$script:Tok = $null
try {
    . 'C:\Users\gaotu\.secrets\Secrets.ps1'
    $script:Tok = (Get-SecretBox -Name 'github-pm-relay-pat' -Reveal)
} catch { $script:Tok = $null }
Write-Host ("[token] " + $(if ($script:Tok) { "loaded len=$($script:Tok.Length)" } else { 'none → 走 raw' }))

function _api([string]$name) {
    # 注意：$name?ref 会被 PS 当成变量名（? 在变量名里合法）→ 必须写成 ${name}
    $u = "https://api.github.com/repos/$Repo/contents/relay/${name}?ref=$Branch"
    $h = @{ 'User-Agent' = 'dsh-pm-relay'; Accept = 'application/vnd.github+json' }
    if ($script:Tok) { $h.Authorization = "Bearer $($script:Tok)" }
    $x = Invoke-RestMethod -Uri $u -Headers $h -TimeoutSec 45
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($x.content))
}

function _raw([string]$name) {
    $u = "$rawBase/$name`?t=" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $r = Invoke-WebRequest -Uri $u -TimeoutSec 30 -UseBasicParsing -Headers @{ 'Cache-Control' = 'no-cache' }
    return [string]$r.Content
}

function _get([string]$name) {
    $dst = Join-Path $OutDir $name
    $text = $null
    $via = 'raw'
    if (-not $ForceRaw -and $script:Tok) {
        try { $text = _api $name; $via = 'api' }
        catch { Write-Host "[api-fail] $name → $($_.Exception.Message)"; $text = $null }
    }
    if ($null -eq $text) { $text = _raw $name }
    $bytes = [Text.Encoding]::UTF8.GetBytes($text)
    [IO.File]::WriteAllBytes($dst, $bytes)
    return [pscustomobject]@{ name = $name; bytes = $bytes.Length; path = $dst; via = $via }
}

$meta = _get 'meta.json'
$m = Get-Content -LiteralPath $meta.path -Raw -Encoding UTF8 | ConvertFrom-Json
$age = ([datetime]::UtcNow - [datetime]::Parse($m.generated_at).ToUniversalTime()).TotalMinutes
$fresh = $age -le $MaxAgeMin

Write-Host ("[meta:{0}] generated_at={1}  age={2:N0} min  n_markets={3}  n_series={4}  fresh={5}" -f `
    $meta.via, $m.generated_at, $age, $m.n_markets, $m.n_series, $fresh)

if ($Check) {
  if (-not $fresh) { Write-Host "[WARN] 中继超过 $MaxAgeMin 分钟未更新"; exit 1 }
  Write-Host '[OK] 中继新鲜'
  exit 0
}

$mk = _get 'markets.json'
$sr = _get 'series.json'
Write-Host ("[ok] markets.json {0:N0} KB ｜ series.json {1:N0} KB ｜ via={2}" -f `
    ($mk.bytes / 1KB), ($sr.bytes / 1KB), $mk.via)

if (-not $fresh) {
  Write-Host "[WARN] 中继超过 $MaxAgeMin 分钟未更新（Actions 可能挂了），文件已落盘但请检查 workflow"
  exit 1
}
exit 0
