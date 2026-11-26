# 一键：只读刷新公开价格 -> 生成静态站 ->（可选）部署到 Cloudflare Pages
# 用法：
#   pwsh -File relay-compare/update.ps1              # 刷新 + 生成
#   pwsh -File relay-compare/update.ps1 -SkipFetch   # 只用现有快照重新生成
#   pwsh -File relay-compare/update.ps1 -Deploy      # 生成后部署（需先 wrangler login 并在 config 里填好域名）
param(
    [switch]$SkipFetch,
    [switch]$Deploy,
    [string]$Project = "relay-price",
    [string]$Python = "E:\Python312\python.exe"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $SkipFetch) {
    & $Python tools/refresh.py fetch
    if ($LASTEXITCODE -ne 0) { throw "refresh failed" }
}
& $Python tools/intel.py   # 域名资历 / 流量榜 / 收录来源，7 天内已查过的会跳过
if ($LASTEXITCODE -ne 0) { Write-Warning "intel 更新失败，沿用旧缓存" }
& $Python -m unittest tests.test_pricing tests.test_scoring
if ($LASTEXITCODE -ne 0) { throw "tests failed" }
& $Python tools/build.py
if ($LASTEXITCODE -ne 0) { throw "build failed" }

if ($Deploy) {
    $cfg = Get-Content config.json -Raw -Encoding utf8 | ConvertFrom-Json
    if ($cfg.base_url -match "example\.com") { throw "config.json 的 base_url 仍是占位域名，先改成正式域名再部署" }
    npx --yes wrangler pages deploy dist --project-name $Project --branch main --commit-dirty=true
    if ($LASTEXITCODE -ne 0) { throw "deploy failed" }
}
