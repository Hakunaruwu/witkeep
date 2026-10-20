# -*- coding: utf-8 -*-
"""witkeep —— 武汉工程大学校园网（WIT）自动保活 / 掉线重登守护进程。

单进程常驻，每 N 秒轻量探测一次；一旦发现被校园网拦截，就用保存的账号密码
自动重新认证。密码用 Windows DPAPI 加密存放（只能在本机本用户下解出）。

用法：
  python witkeep.py --set-password          # 首次：录入账号密码（DPAPI 加密存盘）
  python witkeep.py --isp ctcc              # 设置运营商 cmcc/ctcc/cucc/local
  python witkeep.py --check                 # 单次体检，打印当前状态
  python witkeep.py                         # 前台常驻（Ctrl+C 退出）
  python witkeep.py --install               # 注册开机自启（写 HKCU Run，免管理员）
  python witkeep.py --uninstall             # 移除开机自启
"""
import argparse, base64, ctypes, http.cookiejar, json, os, re, socket, ssl, subprocess, sys, time
import urllib.request, urllib.error, urllib.parse
from ctypes import wintypes

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "config.json")
LOG = os.path.join(BASE, "witkeep.log")
PORTAL_API = "https://login.wit.edu.cn/api"
CHECK_URL = "http://connect.rom.miui.com/generate_204"
CHECK_URL2 = "http://www.msftconnecttest.com/connecttest.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MUTEX_NAME = "Global\\witkeep_singleton"

try:  # 控制台默认 GBK，中文会炸；pythonw 下 stdout 为 None
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 日志
def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512 * 1024:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout and not sys.stdout.closed:
        try:
            print(line, flush=True)
        except Exception:
            pass

# ---------------------------------------------------------------- DPAPI
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

def _mkblob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

def dpapi_protect(data):
    src, _k = _mkblob(data)
    out = _BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)

def dpapi_unprotect(blob):
    src, _k = _mkblob(blob)
    out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)

# ---------------------------------------------------------------- 配置
DEFAULTS = {
    "username": "",
    "password_dpapi": "",
    "isp": "ctcc",
    "interval": 30,
    "portal_api": PORTAL_API,
}

def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONF):
        try:
            with open(CONF, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"配置读取失败，使用默认值: {e!r}")
    return cfg

def save_config(cfg):
    with open(CONF, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def get_password(cfg):
    blob = cfg.get("password_dpapi") or ""
    if not blob:
        return None
    try:
        return dpapi_unprotect(base64.b64decode(blob)).decode("utf-8")
    except Exception as e:
        log(f"密码解密失败（换机器/换用户会这样）: {e!r}")
        return None

# ---------------------------------------------------------------- HTTP
def make_opener():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2   # 门户只认 TLS1.2
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")          # 门户 RSA+SHA1 证书，SECLEVEL=2 会被拒
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),           # 系统代理会劫走内网域名，必须绕开
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(cj),
    )
    op.addheaders = [("User-Agent", UA),
                     ("Referer", "https://login.wit.edu.cn/tpl/wit/login_account.html")]
    return op, cj

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

_PROBE_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def http_get(url, opener=None, timeout=8):
    op = opener or _PROBE_OP
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:
        return None, {}, repr(e).encode()

def http_post(url, fields, opener, token=None, timeout=12):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    req.add_header("X-Requested-With", "XMLHttpRequest")
    if token:
        req.add_header("X-CSRF-Token", token)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, repr(e)

# ---------------------------------------------------------------- 探测
def probe():
    """返回 dict(kind=online|portal|other_portal|down, ip, nasid, location)"""
    for url in (CHECK_URL, CHECK_URL2):
        st, hd, body = http_get(url, timeout=6)
        if st == 204:
            return {"kind": "online"}
        if st in (200,) and b"Microsoft Connect Test" in (body or b""):
            return {"kind": "online"}
        if st is None:
            continue  # 换一个探测点再试
        loc = ""
        for k, v in hd.items():
            if k.lower() == "location":
                loc = v
        if st in (301, 302, 303, 307, 308):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
            ip = (q.get("ip") or q.get("wlanuserip") or [""])[0]
            nas = (q.get("nasId") or [""])[0]
            if "wit.edu.cn" in loc or "/api/r/" in loc:
                return {"kind": "portal", "ip": ip or local_ip(), "nasid": nas or "1", "location": loc}
            return {"kind": "other_portal", "location": loc}
        return {"kind": "unknown", "status": st}
    return {"kind": "down"}

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("10.5.100.42", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""

def local_mac():
    """取默认路由网卡的 MAC（门户不强制要求，拿不到就空）"""
    try:
        import subprocess
        out = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True, timeout=5)
        txt = out.stdout.decode("gbk", "replace")
        for line in txt.splitlines():
            m = re.search(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", line)
            if m and "00-00-00" not in m.group(1):
                return m.group(1).replace("-", ":").lower()
    except Exception:
        pass
    return ""

# ---------------------------------------------------------------- 登录
def query_status(op, username, ip, nasid):
    q = urllib.parse.urlencode({"username": username, "ip": ip, "userIpv4": ip,
                                "nasId": nasid, "switchip": "", "userMac": ""})
    st, _hd, body = http_get(f"{PORTAL_API}/account/status?{q}", opener=op, timeout=10)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return {}

def get_csrf(op):
    st, _hd, body = http_get(f"{PORTAL_API}/csrf-token", opener=op, timeout=10)
    try:
        return json.loads(body.decode("utf-8", "replace")).get("csrf_token", "")
    except Exception:
        return ""

def do_login(cfg, ip, nasid, password):
    """返回 (ok: bool, msg: str)"""
    op, _cj = make_opener()
    token = get_csrf(op)
    if not token:
        return False, "拿不到 csrf-token（网络或证书层问题）"

    # 优先用上一次成功过的运营商；status 里能读到就跟随
    isp = cfg.get("isp") or ""
    st_info = query_status(op, cfg["username"], ip, nasid)
    if isinstance(st_info, dict) and st_info.get("isp"):
        isp = st_info["isp"]

    attempts = []
    for cand in ([isp] if isp else []) + ["", "local"]:
        if cand in attempts:
            continue
        attempts.append(cand)
        fields = {"username": cfg["username"], "password": password,
                  "nasId": nasid, "userIpv4": ip, "switchip": "",
                  "userMac": "", "isp": cand}
        st, txt = http_post(f"{PORTAL_API}/account/login", fields, op, token)
        try:
            js = json.loads(txt)
        except Exception:
            return False, f"登录响应无法解析: {txt[:200]}"
        code = js.get("code")
        msg = js.get("msg") or ""
        if code == 0:
            if cand:
                cfg["isp"] = cand
            return True, f"登录成功（isp={cand or '默认'}）"
        if any(k in msg for k in ("已在线", "已经在线", "重复登录")):
            return True, f"服务器报告已在线（isp={cand or '默认'}）"
        if code == 2:
            return False, f"需要验证码，自动登录无法继续：{msg}"
        if any(k in msg for k in ("运营商", "isp", "ISP")):
            continue  # 换一个运营商参数再试
        return False, f"登录失败 code={code} msg={msg}"
    return False, f"登录失败，已尝试运营商参数 {attempts}"

# ---------------------------------------------------------------- 主循环
def tick(cfg, password, state):
    r = probe()
    kind = r["kind"]
    if kind == "online":
        if state.get("was_offline"):
            log("✔ 已恢复在线")
            state["was_offline"] = False
            state["fail"] = 0
        return cfg.get("interval", 30)

    if kind == "down":
        log("网络不可达（可能 WiFi 断开），稍后再试")
        state["was_offline"] = True
        return max(cfg.get("interval", 30), 60)

    if kind != "portal":
        log(f"被其他网络拦截({r.get('location') or r.get('status')})，不处理")
        return max(cfg.get("interval", 30), 120)

    ip = r.get("ip") or local_ip()
    nasid = r.get("nasid") or "1"
    if not password:
        log("检测到掉线，但没有保存密码，无法自动登录（先跑 --set-password）")
        return max(cfg.get("interval", 30), 120)

    log(f"检测到掉线 → 尝试登录 ip={ip} nasId={nasid}")
    state["was_offline"] = True
    ok, msg = do_login(cfg, ip, nasid, password)
    if ok:
        log("✅ " + msg)
        save_config(cfg)
        state["fail"] = 0
        time.sleep(3)
        if probe()["kind"] == "online":
            log("✔ 复测在线")
            state["was_offline"] = False
            return cfg.get("interval", 30)
        log("⚠ 登录接口返回成功，但复测仍未在线")
        return 15
    state["fail"] = state.get("fail", 0) + 1
    log("❌ " + msg)
    if "验证码" in msg:
        return 600
    backoff = min(30 * (2 ** (state["fail"] - 1)), 600)
    return backoff

def single_instance():
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    return True

def run_daemon(cfg):
    if not single_instance():
        log("已有实例在运行，退出")
        return 1
    password = get_password(cfg)
    if not password:
        log("未配置密码，请先运行: python witkeep.py --set-password")
        return 1
    log(f"=== witkeep 启动 user={cfg['username']} interval={cfg.get('interval', 30)}s ===")
    state = {}
    while True:
        try:
            wait = tick(cfg, password, state)
        except KeyboardInterrupt:
            log("收到中断，退出")
            return 0
        except Exception as e:
            log(f"循环异常（已忽略继续）: {e!r}")
            wait = 60
        time.sleep(max(5, int(wait)))

# ---------------------------------------------------------------- CLI
def cmd_set_password(cfg):
    print("首次配置：账号密码只在本机加密保存（Windows DPAPI，绑定当前用户）。")
    u = input(f"账号（默认 {cfg['username'] or '无'}）: ").strip() or cfg["username"]
    import getpass
    p = getpass.getpass("密码: ")
    if not u or not p:
        print("账号或密码为空，已取消")
        return 1
    cfg["username"] = u
    cfg["password_dpapi"] = base64.b64encode(dpapi_protect(p.encode("utf-8"))).decode()
    save_config(cfg)
    print(f"已保存到 {CONF}")
    print("可以运行 --check 验证一下")
    return 0

def cmd_login(cfg):
    """一次性登录：只试一次，适合不想挂常驻进程的场景"""
    r = probe()
    print(f"探测结果: {r}")
    if r["kind"] == "online":
        print("当前已在线，无需登录")
        return 0
    if r["kind"] != "portal":
        print("当前不在校园网门户的拦截状态，无法（也不需要）登录")
        return 1
    pw = get_password(cfg)
    if not pw:
        print("未配置密码，先运行: python witkeep.py --set-password")
        return 1
    ip = r.get("ip") or local_ip()
    nasid = r.get("nasid") or "1"
    print(f"尝试登录 ip={ip} nasId={nasid} ...")
    ok, msg = do_login(cfg, ip, nasid, pw)
    print(("✅ " if ok else "❌ ") + msg)
    if ok:
        save_config(cfg)
    return 0 if ok else 1

def cmd_check(cfg):
    r = probe()
    print(f"探测结果: {r}")
    if r["kind"] == "portal":
        op, _ = make_opener()
        info = query_status(op, cfg["username"], r.get("ip") or local_ip(), r.get("nasid") or "1")
        print("status 接口:", json.dumps(info, ensure_ascii=False)[:300])
    pw = get_password(cfg)
    print("凭据:", "已配置" if pw else "未配置")
    return 0

def pick_pythonw():
    """优先用系统 Python（不依赖 DSH 运行时），找不到再退回当前解释器"""
    import shutil
    cands = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    p = shutil.which("pythonw")
    if p:
        return p
    return sys.executable.replace("python.exe", "pythonw.exe")

def cmd_install(cfg):
    import winreg
    py = pick_pythonw()
    script = os.path.abspath(__file__)
    val = f'"{py}" "{script}" --daemon'
    keypath = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keypath, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "WITKeep", 0, winreg.REG_SZ, val)
    print("已注册开机自启（HKCU Run，无需管理员权限）")
    print(f"  命令: {val}")
    print("  说明: 登录 Windows 后自动后台启动，无窗口")
    return 0

def cmd_uninstall(cfg):
    import winreg
    keypath = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, keypath, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "WITKeep")
        print("已移除开机自启")
    except FileNotFoundError:
        print("当前没有注册开机自启")
    return 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-password", action="store_true")
    ap.add_argument("--isp", choices=["cmcc", "ctcc", "cucc", "local"],
                    help="运营商：cmcc=移动 ctcc=电信 cucc=联通 local=校园网")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--login", action="store_true", help="只尝试登录一次就退出")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--daemon", action="store_true", help="（开机自启用，不要手动执行）")
    a = ap.parse_args()
    cfg = load_config()
    if a.isp:
        cfg["isp"] = a.isp
        save_config(cfg)
        print(f"已设置运营商: {a.isp}")
        return 0
    if a.set_password:
        return cmd_set_password(cfg)
    if a.check:
        return cmd_check(cfg)
    if a.login:
        return cmd_login(cfg)
    if a.install:
        return cmd_install(cfg)
    if a.uninstall:
        return cmd_uninstall(cfg)
    return run_daemon(cfg)

if __name__ == "__main__":
    sys.exit(main() or 0)
