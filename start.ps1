# ============================================================
# 文件名称：start.ps1
# 生成日期：2026-10-11
# 修改日期：2026-11-20
# 版本信息：v0.261115
# 作    者：JUN
# 备    注：本程序由AI辅助完成。
#           文件功能：执行 start 对应的启动、构建或运维任务。
#           文件用途：供TAAS的 taas_api/start.ps1 模块使用。
#           注意事项：运行前检查依赖与配置；涉及数据或服务变更时先备份并验证。
# ============================================================
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "backend"
$python = Join-Path $root "..\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }
Push-Location (Join-Path $root "backend")
try { & $python -m uvicorn app.taas_app:app --host 0.0.0.0 --port 8100 } finally { Pop-Location }
