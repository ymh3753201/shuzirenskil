param([string]$ProjectDir = (Get-Location).Path)
$ErrorActionPreference = "Stop"
$Installer = Join-Path $PSScriptRoot "install.py"
$env:PYTHONDONTWRITEBYTECODE = "1"
$PythonExe = $null
$PythonArgs = @()
foreach ($Candidate in @("py", "python", "python3")) {
    if (-not (Get-Command $Candidate -ErrorAction SilentlyContinue)) { continue }
    $CandidateArgs = @()
    if ($Candidate -eq "py") { $CandidateArgs = @("-3") }
    & $Candidate @CandidateArgs -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $PythonExe = $Candidate; $PythonArgs = $CandidateArgs; break }
}
if (-not $PythonExe) { throw "没有可运行的 Python 3.10+。请让 Codex 先安装 Python 3.12；Windows 应用商店别名不代表已经安装。" }
& $PythonExe @PythonArgs -B $Installer $ProjectDir --with-runtime
if ($LASTEXITCODE -ne 0) {
    throw "安装程序未成功，退出码：$LASTEXITCODE。请查看上面的提示。"
}
