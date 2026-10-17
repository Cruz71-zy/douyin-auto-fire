param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repository
)
$ErrorActionPreference = 'Stop'
function Assert-Command([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$manifest = Get-Content -LiteralPath (Join-Path $sourceRoot 'DEPLOY_FILES.json') -Raw | ConvertFrom-Json
gh auth status
Assert-Command '请先执行 gh auth login，再重新运行部署脚本。'
$identity = gh api user | ConvertFrom-Json
Assert-Command '读取 GitHub 本人账号失败。'
if ($Repository.Split('/')[0] -ine $identity.login) { throw '只允许部署到当前登录用户本人名下仓库。' }
$repoInfo = gh repo view $Repository --json nameWithOwner,defaultBranchRef | ConvertFrom-Json
Assert-Command '仓库不存在或无权限。请先在 GitHub 网页 Fork 原项目。'
if (-not $repoInfo.defaultBranchRef.name) { throw '仓库缺少默认分支。' }
# Disable live sending before any new workflow is pushed.
gh variable set ENABLE_REAL_SEND --body false --repo $Repository
Assert-Command '设置正式发送关闭开关失败；未推送代码。'
$checkoutRoot = Join-Path $sourceRoot ('deploy-checkout-' + [Guid]::NewGuid().ToString('N'))
git clone --single-branch --branch $repoInfo.defaultBranchRef.name ('https://github.com/' + $Repository + '.git') $checkoutRoot
Assert-Command '克隆失败；请检查 Git 登录和网络。'
foreach ($relativePath in $manifest) {
    $sourceFile = [IO.Path]::GetFullPath((Join-Path $sourceRoot $relativePath))
    $targetFile = [IO.Path]::GetFullPath((Join-Path $checkoutRoot $relativePath))
    if (-not $sourceFile.StartsWith($sourceRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        -not $targetFile.StartsWith($checkoutRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw '清单路径越界，停止。'
    }
    New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($targetFile)) | Out-Null
    Copy-Item -LiteralPath $sourceFile -Destination $targetFile -Force
}
Push-Location $checkoutRoot
try {
    foreach ($obsolete in @('.github/workflows/docker.yml', '.github/workflows/sync-cnb.yml')) {
        $target = [IO.Path]::GetFullPath((Join-Path $checkoutRoot $obsolete))
        if (-not $target.StartsWith($checkoutRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw '路径越界。' }
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target }
    }
    # Fresh checkout only: generated credentials and local files are never copied.
    git add --all
    Assert-Command '暂存失败。'
    $diff = git diff --cached --name-only
    if (-not $diff) { Write-Host '远程仓库代码已经一致；正式发送保持关闭。'; return }
    $mail = [string]$identity.id + '+' + $identity.login + '@users.noreply.github.com'
    git -c ('user.name=' + $identity.login) -c ('user.email=' + $mail) commit -m 'Add conservative daily Douyin Actions runner'
    Assert-Command '提交失败。'
    git push origin ('HEAD:' + $repoInfo.defaultBranchRef.name)
    Assert-Command '推送失败，可能存在分支保护；本地提交已保留，请勿强制推送。'
    Write-Host ('部署成功：https://github.com/' + $Repository + '/actions；默认 Dry Run，接下来按中文教程配置 Secrets。')
} finally { Pop-Location }
