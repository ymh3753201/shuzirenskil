param([string]$ProjectDir = (Get-Location).Path)
$ErrorActionPreference = "Stop"
$Installer = Join-Path $PSScriptRoot "install.py"
$env:PYTHONDONTWRITEBYTECODE = "1"
if (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 -B $Installer $ProjectDir
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python -B $Installer $ProjectDir
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -B $Installer $ProjectDir
} else {
    throw "请先安装 Python 3.10 或更高版本，再运行安装程序。"
}
if ($LASTEXITCODE -ne 0) {
    throw "安装程序未成功，退出码：$LASTEXITCODE。请查看上面的提示。"
}
