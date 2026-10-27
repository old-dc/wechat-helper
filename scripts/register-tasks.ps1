# 注册 Windows 计划任务 wechat-watch：登录后后台常驻监听微信新消息。
# 用法（管理员 PowerShell）：  powershell -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
# 卸载：  Unregister-ScheduledTask -TaskName wechat-watch -Confirm:$false
#
# 注意：watch 需要微信登录且窗口在前台；作为登录任务更适合专用机器/空闲时段。

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$bat  = Join-Path $root 'scripts\watch.bat'

if (-not (Test-Path $bat)) { throw "找不到 $bat" }

$action  = New-ScheduledTaskAction -Execute $bat
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'wechat-watch' -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host '已注册计划任务 wechat-watch（登录后运行）。'
Write-Host '立即测试： Start-ScheduledTask -TaskName wechat-watch'
