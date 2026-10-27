#Requires -RunAsAdministrator
# witkeep 系统级计划任务安装脚本
# ---------------------------------------------------------------------------
# 把 witkeep 装成「开机即启动、不需要用户登录」的计划任务（SYSTEM 身份）。
# 与 HKCU\...\Run 的区别：本脚本注册的任务在登录界面就会出现并在后台运行。
#
# 用法（管理员 PowerShell）：
#   .\install-task.ps1              安装
#   .\install-task.ps1 -Uninstall   卸载
# ---------------------------------------------------------------------------
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
try { Start-Transcript -Path (Join-Path $env:TEMP 'witkeep_install.log') -Force | Out-Null } catch {}
$TaskName = 'WITKeep'
$Dst      = 'C:\ProgramData\witkeep'
$Src      = $PSScriptRoot

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

# ---------------------------------------------------------------- 卸载
if ($Uninstall) {
    Say "== 卸载 ==" Cyan
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Say "  已删除计划任务 $TaskName" Green
    } else {
        Say "  没有找到计划任务 $TaskName"
    }
    Get-Process pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like 'pythonw*' } | Out-Null
    Say "  数据目录保留在 $Dst（如需彻底删除请手动移除）"
    exit 0
}

Say "== 1/6 准备目录 ==" Cyan
New-Item -ItemType Directory -Force -Path $Dst | Out-Null
Copy-Item (Join-Path $Src 'witkeep.py') (Join-Path $Dst 'witkeep.py') -Force
Say "  脚本已复制到 $Dst\witkeep.py"

if (-not (Test-Path (Join-Path $Dst 'config.json'))) {
    if (Test-Path (Join-Path $Src 'config.json')) {
        Copy-Item (Join-Path $Src 'config.json') (Join-Path $Dst 'config.json') -Force
        Say "  已从 $Src\config.json 复制配置"
    }
}

# ---------------------------------------------------------------- pythonw
Say "== 2/6 定位 pythonw.exe ==" Cyan
$cands = @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\pythonw.exe" -ErrorAction SilentlyContinue |
           Sort-Object FullName -Descending)
$pyw = if ($cands.Count) { $cands[0].FullName } else { (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
if (-not $pyw) { throw "找不到 pythonw.exe，请先安装 Python" }
Say "  $pyw"

# ---------------------------------------------------------------- 密码转机器作用域
Say "== 3/6 密码转 machine 作用域 ==" Cyan
$py = $pyw -replace 'pythonw\.exe$', 'python.exe'
$cfgPath = Join-Path $Dst 'config.json'
if (-not (Test-Path $cfgPath)) {
    Say "  ! 缺少 config.json，请先在本目录运行： python witkeep.py --set-password --machine" Red
    throw "配置缺失"
}
$scope = (Get-Content $cfgPath -Raw | ConvertFrom-Json).dpapi_scope
if ($scope -eq 'machine') {
    Say "  已经是 machine 作用域，跳过"
} else {
    Push-Location $Dst
    try { & $py witkeep.py --migrate-machine } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "转换失败" }
}

# 配置只给 SYSTEM 和管理员读
& icacls $cfgPath /inheritance:r /grant:r 'SYSTEM:(F)' 'Administrators:(F)' | Out-Null
Say "  config.json 权限已收紧为 SYSTEM + Administrators"

# ---------------------------------------------------------------- 注册任务
Say "== 4/6 注册计划任务 ==" Cyan
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>WIT campus network keeper - auto re-login after disconnect</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT2M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$pyw</Command>
      <Arguments>"$Dst\witkeep.py" --daemon</Arguments>
      <WorkingDirectory>$Dst</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
$tmp = Join-Path $env:TEMP 'witkeep_task.xml'
[System.IO.File]::WriteAllText($tmp, $xml, [System.Text.Encoding]::Unicode)
schtasks /Create /TN $TaskName /XML $tmp /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks 注册失败" }
Say "  已注册：触发器=开机后 30s，身份=SYSTEM，失败 3 次内每 2 分钟重试"

# ---------------------------------------------------------------- 清掉旧的 HKCU 自启
Say "== 5/6 移除旧的 HKCU Run 自启 ==" Cyan
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ((Get-ItemProperty $runKey -ErrorAction SilentlyContinue).PSObject.Properties.Name -contains 'WITKeep') {
    Remove-ItemProperty -Path $runKey -Name 'WITKeep'
    Say "  已删除 HKCU Run\WITKeep（避免与系统任务重复）"
} else {
    Say "  没有旧条目"
}

# ---------------------------------------------------------------- 立刻跑一次
Say "== 6/6 立即启动并验证 ==" Cyan
schtasks /Run /TN $TaskName | Out-Null
Start-Sleep -Seconds 10
$p = Get-Process pythonw -ErrorAction SilentlyContinue
if ($p) {
    foreach ($x in $p) {
        Say ("  进程 PID={0} 内存={1}MB 会话={2}" -f $x.Id, [math]::Round($x.WorkingSet64/1MB,1), $x.SessionId) Green
    }
} else {
    Say "  ! 没有检测到 pythonw 进程" Red
}
Say ""
Say "---- 日志最后 8 行 ----" Cyan
$log = Join-Path $Dst 'witkeep.log'
if (Test-Path $log) { Get-Content $log -Encoding UTF8 -Tail 8 } else { Say "  日志还没生成" Yellow }
Say ""
Say "安装完成。任务名: $TaskName    数据目录: $Dst" Green
