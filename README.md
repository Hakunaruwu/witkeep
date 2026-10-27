# witkeep

**武汉工程大学（WIT）校园网自动保活工具** —— 掉线自动重登，挂后台占 30 MB，不管它就一直在线。

> 只对武汉工程大学（WIT）的校园网认证系统做了适配。其它学校的深澜/Dr.COM 体系理论上改几个参数也能用，但没测过。

---

## 它解决什么问题

学校校园网有个很烦的地方：认证是有状态的，隔一段时间、重连一次 WiFi、电脑睡眠唤醒，都可能被踢下线。等你发现连不上网了，再手动打开网页输一遍账号密码。

这个工具就干一件事：**每 30 秒悄悄问一下网络通不通，不通就自动帮你登回去。** 正常情况下你完全感觉不到它存在。

实测被踢下线后 **1 秒内**恢复。

---

## 特性

- **占用小**：Python 单进程，内存约 30 MB，CPU 几乎为 0（实测累计 0.12 秒）
- **不掉线才不折腾**：只在真的被拦截时才登录，不会傻乎乎定时重发认证请求
- **密码不裸奔**：用 Windows DPAPI 加密存盘，绑定你这台电脑的这个用户；`config.json` 拷到别人电脑上解不开
- **不用管理员权限**：开机自启写的是当前用户的注册表 `Run` 项，不碰系统服务
- **单实例保护**：重复启动会自己退出，不会开一堆进程
- **认得出"不是本校"**：被酒店/家里的 WiFi 拦截时不动手，绝不把账号密码发给别人的网络
- **零第三方依赖**：只用 Python 标准库

---

## 环境要求

- Windows 10 / 11
- Python 3.8 及以上（[官网下载](https://www.python.org/downloads/windows/)，安装时记得勾 **Add python.exe to PATH**）

检查一下：

```bat
python --version
```

---

## 快速开始

### 1. 下载

```bat
git clone https://github.com/Hakunaruwu/witkeep.git
cd witkeep
```

没有 git 的话，点右上角 **Code → Download ZIP** 解压也行。

### 2. 录入账号密码

```bat
python witkeep.py --set-password
```

按提示输入学号和密码（输密码时屏幕不显示，正常现象）。密码当场用 DPAPI 加密后写进 `config.json`，**不会存明文，也不会发到任何地方**。

### 3. 确认能用

```bat
python witkeep.py --check
```

会打印当前网络状态。`{'kind': 'online'}` 表示在线。

如果你是移动/联通宽带（不是校园网直连），先设置运营商，否则可能登不上：

```bat
python witkeep.py --isp cmcc     :: 移动
python witkeep.py --isp cucc     :: 联通
python witkeep.py --isp ctcc     :: 电信（默认）
```

> 不知道自己是哪个？不用管，程序登录时会自动依次尝试，设不设都能通。

### 4. 开机自启（推荐）

```bat
python witkeep.py --install
```

这一步会往注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 写一条记录，以后**登录 Windows 就自动在后台跑**，没有黑窗口。

现在就想起效而不想重启，直接在前台跑一次：

```bat
python witkeep.py
```

或者去任务管理器结束掉旧的 `pythonw.exe`，再重新登录一次 Windows 让自启重新拉起。

> 这条自启是**跟人走的**：只有你登录进桌面才会启动。停在登录界面、或者开机后没登录，它不会跑。

### 5. 开机自启（不登录也跑，进阶）

如果你要的是「开机就活，不管有没有人登录」，得用系统级计划任务，需要管理员：

```powershell
# 右键「以管理员身份运行」PowerShell，然后
.\install-task.ps1
```

它会做四件事：

1. 把程序和配置搬到 `C:\ProgramData\witkeep\`（不受你误删桌面文件的影响）
2. 把密码转成**机器作用域** DPAPI 加密（`dpapi_scope: "machine"`）
3. 注册计划任务 `WITKeep`：开机 30 秒后以 **SYSTEM** 身份启动，失败 3 次内每 2 分钟重试
4. 顺手删掉旧的那条 HKCU Run，避免两套同时跑

卸载：

```powershell
.\install-task.ps1 -Uninstall
```

**代价要说清楚**：机器作用域加密意味着**本机任何账户**都能解开这个密码；相比用户作用域（只有你自己能解）安全性降低。自己一个人的电脑无所谓，多人共用的机器建议只用第 4 步的用户级自启。

---

## 命令行一览

| 命令 | 作用 |
| --- | --- |
| `python witkeep.py` | 前台常驻运行，`Ctrl+C` 退出 |
| `python witkeep.py --set-password` | 录入/修改账号密码 |
| `python witkeep.py --set-password --machine` | 同上，但用机器作用域加密（给系统任务用） |
| `python witkeep.py --migrate-machine` | 把已有的用户作用域密码就地转成机器作用域 |
| `python witkeep.py --isp ctcc` | 设置运营商（cmcc/ctcc/cucc/local） |
| `python witkeep.py --check` | 体检一次，打印当前网络状态 |
| `python witkeep.py --login` | 只尝试登录一次就退出（不想挂常驻时用） |
| `python witkeep.py --install` | 注册开机自启 |
| `python witkeep.py --uninstall` | 移除开机自启 |

---

## 怎么确认它真的在工作

打开 `witkeep.log`，正常应该长这样：

```
[2026-10-20 13:28:32] === witkeep 启动 user=2023000000 interval=30s ===
[2026-10-20 13:30:02] 检测到掉线 → 尝试登录 ip=10.21.56.65 nasId=1
[2026-10-20 13:30:03] ✅ 登录成功（isp=ctcc）
[2026-10-20 13:30:06] ✔ 复测在线
```

一直在线的时候日志是安静的（不会每 30 秒刷一行），只有状态变化时才写。日志超过 512 KB 自动转存为 `witkeep.log.1`。

---

## 工作原理

1. **探测**：GET `http://connect.rom.miui.com/generate_204`
   - 返回 `204` → 在线，睡 30 秒再看
   - 返回 `302` 且跳转到 `login.wit.edu.cn` → 被校园网拦截了，从跳转 URL 里拿到本次的 `ip` 和 `nasId`
   - 连不上 → 认为 WiFi 断了，不折腾
   - 跳转到别的门户 → 不是本校网络，不处理
2. **登录**：走门户的 REST 接口
   - `GET /api/csrf-token` 拿 token（同时建立会话）
   - `POST /api/account/login`，提交 `username / password / nasId / userIpv4 / isp`
3. **复测**：再探一次，确认真的通了才算成功

这个 204 探测点是 Android 系统的联网检测地址，极小、稳定、不产生业务流量。

---

## 常见问题

**Q：会不会把我的密码传到什么地方？**
不会。密码只存在本地 `config.json` 里，DPAPI 加密，解密密钥由 Windows 保管在你这台机器上。整个程序只和学校门户 `login.wit.edu.cn` 通信。

**Q：换了电脑 / 换了 Windows 账户，要重新弄吗？**
要。DPAPI 的加密是绑定用户的，重新跑一次 `--set-password` 即可。

**Q：提示"需要验证码"怎么办？**
说明学校那边认为登录行为可疑（通常是密码错太多次触发的）。去网页版手动登录一次，看是不是密码改了。

**Q：日志里一直显示"网络不可达"？**
那是 WiFi 没连上的意思，不是程序的问题。

**Q：开机自启了但重启后没生效？**
它注册的是"**用户登录时**启动"，不是"开机时启动"。你得登录进 Windows 桌面它才会跑。如果开机后停在锁屏/登录界面，它是不会运行的。

**Q：安全软件报毒？**
不会扫到它——它没有 exe，就是个 py 脚本，用系统自带的 Python 跑。如果杀软拦了注册表写入，手动加白即可。

**Q：想彻底删掉？**
```bat
python witkeep.py --uninstall
```
然后删掉整个文件夹就行。注册表那条 `WITKeep` 也可以手动从 `regedit` 删。

---

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `witkeep.py` | 全部代码，单文件，无第三方依赖 |
| `install-task.ps1` | 注册/卸载系统级开机任务（需管理员） |
| `config.json` | 你的账号 + 加密后的密码（**首次运行后才生成，不要上传给别人**） |
| `witkeep.log` | 运行日志 |

---

## 免责声明

- 本项目仅为本校同学日常使用提供便利，与武汉工程大学及校园网运营方无关。
- 请自行保管好账号密码，不要把带 `config.json` 的文件夹打包发给别人。
- 使用本工具产生的任何后果由使用者自行承担。

## License

[MIT](LICENSE)
