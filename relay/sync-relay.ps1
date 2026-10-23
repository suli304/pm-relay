# -*- coding: utf-8 -*-
<#
  sync-relay.ps1 · 从 GitHub 拉中继产物到本机 relay/ 目录
  用法：
    .\sync-relay.ps1 -Repo owner/name                 # 拉 meta/markets/series
    .\sync-relay.ps1 -Repo owner/name -Branch main -Check
  说明：
    只读 raw.githubusercontent.com（公网，无需 token）。
    拉了 meta.json 之后按里面的 n_markets 判断是否新鲜，超过 -MaxAgeMin 分钟就报警但仍落盘。
#>
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [string]$Branch = 'main',
  [string]$OutDir = '',
  [int]$MaxAgeMin = 90,
  [switch]$Check
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path $PSScriptRoot '' }
$OutDir = (Resolve-Path $OutDir).Path
$base = "https://raw.githubusercontent.com/$Repo/$Branch/relay"

function _get([string]$name) {
  $u = "$base/$name"
  $dst = Join-Path $OutDir $name
  try {
    $r = Invoke-WebRequest -Uri $u -TimeoutSec 30 -UseBasicParsing
  } catch {
    throw "拉取失败 $u ：$($_.Exception.Message)"
  }
  $bytes = [Text.Encoding]::UTF8.GetBytes($r.Content)
  [IO.File]::WriteAllBytes($dst, $bytes)
  return [pscustomobject]@{ name = $name; bytes = $bytes.Length; path = $dst }
}

$meta = _get 'meta.json'
$m = Get-Content -LiteralPath $meta.path -Raw -Encoding UTF8 | ConvertFrom-Json
$age = ([datetime]::UtcNow - [datetime]::Parse($m.generated_at).ToUniversalTime()).TotalMinutes
$fresh = $age -le $MaxAgeMin

Write-Host ("[meta] generated_at={0}  age={1:N0} min  n_markets={2}  n_series={3}  fresh={4}" -f `
    $m.generated_at, $age, $m.n_markets, $m.n_series, $fresh)

if ($Check) {
  if (-not $fresh) { Write-Host "[WARN] 中继超过 $MaxAgeMin 分钟未更新"; exit 1 }
  Write-Host '[OK] 中继新鲜'
  exit 0
}

$mk = _get 'markets.json'
$sr = _get 'series.json'
Write-Host ("[ok] markets.json {0:N0} KB ｜ series.json {1:N0} KB" -f ($mk.bytes / 1KB), ($sr.bytes / 1KB))

if (-not $fresh) {
  Write-Host "[WARN] 中继超过 $MaxAgeMin 分钟未更新（Actions 可能挂了），文件已落盘但请检查 workflow"
  exit 1
}
exit 0
