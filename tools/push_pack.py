#!/usr/bin/env python3
"""推资源包（Mac / Windows 共用，只此一份；11-06 维护者拍板、维护者「玩家流程做在共用后端、两端都验」）：
把 <资源包目录> 增量推到已装好的 App，只传机上没有 / 内容变了的文件，App 下次启动时自己解开（S2A_PACK_APPLY_ONLY=1 只解包不跑游戏）。
原先 Mac 是 tools/push_pack.sh（bash + devicectl）、Windows 是 pack/orch/push_win.py（s2a-devpush），同一套契约各写一遍，
残留批覆盖、.pushed 记账这类坑两边各踩一次；现在两边都调本模块，tools/push_pack.sh 只剩 dev_run 的回调外壳。
放在 tools/ 而不是 pack/jobs/：调度把 tools/ 整份单独拷到 gate-host 跑 dev_run（tools/sched.sh git archive tools），tools 树要自成一体（11-06 列车 sched_test / ssh_argv_test 退回）。

口径（手机端 runtime/src/pack_apply.c 不变）：
  ① 机上已有什么按 <文档根>/资源包/index.applied.tsv 认（文档根缺省 Documents，多款容器每款 Documents/games/<游戏号>）（App 解包成功时写；老 App 没有它才退回 index.tsv）
  ② .incoming 里本工具推的包（<时刻>_<三位序号>.tar）逐个核完整性（s2a-devpush tar-ok：512 整倍、末尾 1024 字节全零）：
     完整的留着（推完、还没起 App 解的批：出包重叠早推的、上次推完没起 App 的），其里的文件按 .pushed 算机上已有；
     不完整或核不了（Mac 没有 s2a-devpush / UDID）的用同名空 tar（1024 字节全零）盖掉，不信 .pushed、按已应用索引补推（10-26 截短残批）
  ③ 没有待解批 = 账已由已应用索引接管，.pushed 清空（10-20 换回旧包时 .pushed 旧账把被新包删掉的文件判成「机上已有」）
  ④ 上次收推送撞到截短包（unpack.log 末行）：不信 .pushed，也不退回 index.tsv（它是上次推的目标，文件没落地）
  ⑤ 推前抽查机上实况：直连库顶层表 + b-<链>/lib.idx 全核、同 sha 小文件按 sha 序取 5 个拉回比 sha256；
     任何一个不符：拉机上实际清单（路径 + 大小）核账，只留在且大小对得上的行；清单拉不回才整包重推（10-25 马车8、11-02 测试设备1 奥德赛）
  ⑥ ≤1 GB 一批（单个超 1 GB 的文件自成一批）；每批 s2a-devpush put-tar 边读边打 pax tar 直接写进 .incoming（本机盘不落临时 tar）；
     Mac 上没工具 / 不知道 UDID / 这批流式写失败 → 这批打 tar + devicectl copy（同名覆盖半截包）
  ⑦ 每推完一批把「路径 \\t sha」追加进 .pushed 并推上机（中断续推 / 出包重叠早推后补差量）；最后推 index.tsv 与 manifest.json
  ⑧ 推之前核剩余空间：待推量 + 最大一批 + 每文件 4 KB + S2A_PUSH_SPARE_GB（缺省 1）GB（need_bytes）；读不到照推

设备操作两种（同一个 Dev 接口）：
  Devpush：s2a-devpush 走 usbmuxd + house_arrest + AFC（Windows 只有它；Mac 上手机连 usbmuxd 时也能用）
  Devicectl：Mac 的 xcrun devicectl（S2A_DEVICECTL 可换假的）；put-tar / tar-ok 有 s2a-devpush + UDID 时借它

用法：
  python3 tools/push_pack.py run   <设备> <包名> <资源包目录> [--full] [--udid U] [--devpush P] [--work 目录] [--apply]   一条龙（启动器 / Windows / 手动装机）；
      --apply：推完起一次 App 只解包并等解完（.applied = 本包且解包日志本次新增完成行）
  各子命令都收 --docs <设备侧文档根>（缺省 Documents；多款容器每款 Documents/games/<游戏号>，资源包 / 解包日志都在它下面）
      与 --launch-ctx <值>（多款容器：起 App 解包时带 S2A_LAUNCH_CTX=<值>，如 @bundle/Games/<游戏号>/launch.ctx）
  python3 tools/push_pack.py plan  <设备> <包名> <资源包目录> --work O [--full] [--udid U]   算待推批次（dev_run 用：之后它按推送量放大看门、腾空间）
  python3 tools/push_pack.py free  <设备> <包名> <资源包目录> --work O [--udid U]           机上可写字节（读不到不打印）
  python3 tools/push_pack.py send  <设备> <包名> <资源包目录> --work O [--udid U] [--devpush P]   推 plan 算好的批次 + 索引 + 清单
  plan / send 把机读结果写进 <O>/push.state（PLAN 批数 文件数 字节数 / SENT 批数 / FAIL 原因），日志行打到标准输出（不带时刻，调用方自己加）
  <设备>：Mac 是 devicectl 的设备标识或名字；Windows（或 --via devpush）是手机 UDID
退出码：0 成功；1 失败（标准输出末行【红】说明）；2 用法"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

R = Path(__file__).resolve().parents[1]
LIM = 1 << 30
IMG = re.compile(r"(^|.*/)romfs\.img$")   # 单文件镜像：直接写到最终路径，不进 tar、不用首启解开
DOCS = "Documents"   # 设备侧文档根（单款 App）；多款容器每款 Documents/games/<游戏号>（pack/ship_ext.py 容器布局），设备对象的 docs 属性


def rp(dev): return f"{dev.docs}/资源包"
def inc(dev): return rp(dev) + "/.incoming"
def unpack_log(dev): return f"{dev.docs}/unpack.log"   # runtime/src/pack_apply.c：<文档根>/unpack.log
STALE = re.compile(r"\d+_\d{3}\.tar")
EXE = "s2a-devpush.exe" if os.name == "nt" else "s2a-devpush"


class Fail(Exception):
    """推包失败：reason 是给调用方 fin / 启动器看的一句话"""


class NeedsPlayer(Fail):
    """资源已传完，只差玩家在手机上打开 App 解开（电脑拉不起 App，#192）：不是传输失败，调用方按「需要玩家操作」收尾"""


def devpush_path(given=None, env=None):
    """s2a-devpush：显式给的 > S2A_DEVPUSH > 启动器发版内置（S2A_PREBUILT_TOOLS/devpush/）> 仓库编出的 build/devpush/release > PATH"""
    env = os.environ if env is None else env
    pre = env.get("S2A_PREBUILT_TOOLS")
    for p in (given, env.get("S2A_DEVPUSH"), pre and str(Path(pre) / "devpush" / EXE), str(R / "build/devpush/release" / EXE), shutil.which(EXE)):
        if p and Path(p).is_file() and (os.name == "nt" or os.access(p, os.X_OK)):
            return str(p)
    return None


class Devpush:
    """s2a-devpush（pack/devpush）：经 usbmuxd（USBMUXD_SOCKET_ADDRESS 可指别的机器）连手机，house_arrest 进 App 容器、AFC 读写"""
    kind = "devpush"
    docs, ctx = DOCS, None

    def __init__(self, exe, udid, bid, work, env=None):
        self.exe, self.udid, self.bid, self.work, self.env = exe, udid, bid, Path(work), env

    def call(self, *args):
        try:
            p = subprocess.run([self.exe, self.udid, self.bid, *map(str, args)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        except OSError as e:
            return 1, str(e)
        out = (p.stdout + p.stderr).strip()
        with open(self.work / "devpush.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {args[0]} rc={p.returncode} {out[-300:]}\n")
        return p.returncode, out

    def get(self, rel, dst, t=None):
        Path(dst).unlink(missing_ok=True)
        rc, _ = self.call("get", rel, dst)
        return rc == 0 and Path(dst).is_file()

    def put(self, src, rel):
        rc, out = self.call("put", src, rel)
        if rc:
            raise Fail(f"推 {Path(rel).name} 失败：{out.splitlines()[-1] if out else rc}")

    def ls(self, rel):
        rc, out = self.call("ls", rel)
        return out.splitlines() if rc == 0 else None

    def get_tree(self, rel, dst):   # 整个目录拉回本机；目录不在 = 成功且 0 个文件。→ (成功?, 输出)
        rc, out = self.call("get-tree", rel, dst)
        return rc == 0, out

    def rm(self, rel):   # 删机上一个文件或整个目录（已不在算成功）。→ (成功?, 输出)
        rc, out = self.call("rm", rel)
        return rc == 0, out

    put_big = put

    def tar_ok(self, rel):
        rc, out = self.call("tar-ok", rel)
        return rc == 0 and out.strip().endswith("ok")

    def put_tar(self, lst, root, rel):
        rc, out = self.call("put-tar", lst, root, rel)
        if rc:
            raise Fail(f"推 {Path(rel).name} 失败：{out.splitlines()[-1] if out else rc}")
        return "stream"

    def listing(self):   # 机上整个资源包目录的「路径 → 大小」：AFC 列表不带大小，读不到
        return None

    def free(self):   # 机上可写字节（AFC 设备信息）；工具太旧或读不到 → None，调用方照推
        rc, out = self.call("free", "x")
        return int(out.strip()) if rc == 0 and re.fullmatch(r"\d+", out.strip()) else None

    def nospc(self):
        return False

    def launch(self, env):   # 起 App（s2a-devpush launch：CoreDevice app_service，iOS 17+；旧工具没有 launch → 失败）
        rc, out = self.call("launch", *[f"{k}={v}" for k, v in env.items()])
        if rc: raise Fail(f"起 App 失败：{out.splitlines()[-1] if out else rc}")


class Devicectl:
    """Mac：xcrun devicectl（口径同 tools/dev_run.sh：一律用 devicectl 自带 --timeout，外层 timeout 只在它自己卡死时兜底；拷贝按大小定超时、挂了重试一次）。
    有 s2a-devpush + UDID 时流式写批次、核待解批完整性借它（手机只在 CoreDevice 无线隧道上、usbmuxd 看不到时那批退回 devicectl）"""
    kind = "devicectl"
    docs, ctx = DOCS, None

    def __init__(self, device, bid, work, udid=None, devpush=None, env=None):
        self.device, self.bid, self.work, self.udid, self.env = device, bid, Path(work), udid, env
        self.dc = shlex.split((env or os.environ).get("S2A_DEVICECTL") or "xcrun devicectl")
        self.dp = Devpush(devpush, udid, bid, work, env) if devpush and udid else None

    def dcp(self, t, *args, log=None):
        s0 = time.time(); margin = int((self.env or os.environ).get("S2A_DC_MARGIN", "30"))
        try:
            p = subprocess.run([*self.dc, *map(str, args), "--timeout", str(t)], capture_output=True, text=True, errors="replace", timeout=t + margin, env=self.env)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            rc, out = 124, ""
        if log:
            with open(self.work / log, "a", encoding="utf-8") as f: f.write(out)
        if rc != 0 and time.time() - s0 >= t: rc = 124
        return rc, out

    def slow(self, what, s0, rc):
        if time.time() - s0 >= 20:
            with open(self.work / "slow_devicectl.log", "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] 慢：{what} 用了 {time.time() - s0:.0f} 秒（rc={rc}）\n")

    def container(self):
        return ["--device", self.device, "--domain-type", "appDataContainer", "--domain-identifier", self.bid]

    def get(self, rel, dst, t=None):   # 调用方没给 t 时超时 120 秒、挂了重试一次
        s0 = time.time(); Path(dst).unlink(missing_ok=True)
        for _ in range(1 if t else 2):
            rc, _ = self.dcp(t or 120, "device", "copy", "from", *self.container(), "--source", rel, "--destination", dst, log="cpf.log")
            if rc != 124: break
        self.slow(f"拉 {rel}", s0, rc)
        return rc == 0 and Path(dst).is_file()

    def put(self, src, rel):   # 超时 = S2A_CP_T0（缺省 60）秒 + 每 10 MB 1 秒（实测推送约 33 MB/s，留 3 倍余量）
        s0 = time.time(); t = int((self.env or os.environ).get("S2A_CP_T0", "60")) + Path(src).stat().st_size // (10 << 20)
        for i in (1, 2):
            rc, _ = self.dcp(t, "device", "copy", "to", *self.container(), "--source", src, "--destination", rel, log="cp.log")
            if rc != 124: break
            with open(self.work / "slow_devicectl.log", "a", encoding="utf-8") as f: f.write(f"[{time.strftime('%H:%M:%S')}] 挂：推 {rel} 超 {t} 秒（第 {i} 次）\n")
        self.slow(f"推 {rel}", s0, rc)
        if rc:
            raise Fail(f"推 {Path(rel).name} 失败")

    def put_big(self, src, rel):   # 大文件优先走 s2a-devpush 流式（usbmuxd 能到时快且不落临时文件），不行退回 devicectl
        if self.dp:
            try:
                return self.dp.put(src, rel)
            except Fail as e:
                self.say(f"流式写 {Path(rel).name} 没成（{str(e)[-160:]}），改走 devicectl")
        self.put(src, rel)

    def ls(self, rel):
        rc, out = self.dcp(60, "device", "info", "files", *self.container(), "--subdirectory", rel, "--no-recurse")
        return out.splitlines() if rc == 0 else None

    def tar_ok(self, rel):   # 没有 s2a-devpush / UDID：判不了（None），调用方当不完整盖掉
        return self.dp.tar_ok(rel) if self.dp else None

    def put_tar(self, lst, root, rel):
        if self.dp:
            try:
                return self.dp.put_tar(lst, root, rel)
            except Fail as e:
                self.say(f"流式推 {Path(rel).name} 没成（{str(e)[-160:]}），这批改走 devicectl")
        tf = self.work / "push.tar"
        try:
            names = [l for l in Path(lst).read_text(encoding="utf-8").splitlines() if l]
            with tarfile.open(tf, "w", format=tarfile.PAX_FORMAT) as t:
                for n in names:
                    ti = t.gettarinfo(str(Path(root) / n), arcname=n); ti.uid = ti.gid = 0; ti.uname = ti.gname = ""
                    with open(Path(root) / n, "rb") as f: t.addfile(ti, f)
            self.put(tf, rel)
        finally:
            tf.unlink(missing_ok=True)   # 失败也删：10-29 局目录里留下 10.7 GB 的 push.tar
        return "devicectl"

    def listing(self):
        js = self.work / "r.list.json"
        rc, _ = self.dcp(300, "device", "info", "files", *self.container(), "--subdirectory", rp(self), "--json-output", js)
        if rc: return None
        try:
            return {f["relativePath"]: f["metadata"]["size"] for f in json.loads(js.read_text())["result"]["files"] if not f.get("resources", {}).get("isDirectory")}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def free(self):   # 机上可写字节（ideviceinfo AmountDataAvailable）；UDID 没给就按设备标识从 devicectl 设备列表换；读不到 None
        ud = self.udid
        if not ud:
            js = self.work / "devlist.json"
            self.dcp(60, "list", "devices", "--json-output", js)
            try:
                for d in json.loads(js.read_text())["result"]["devices"]:
                    u = d.get("hardwareProperties", {}).get("udid", "")
                    if self.device in (d.get("identifier"), u): ud = u; break
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if not ud or not shutil.which("ideviceinfo"): return None
        try:
            out = subprocess.run(["ideviceinfo", "-u", ud, "-q", "com.apple.disk_usage", "-k", "AmountDataAvailable"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        return int(out) if re.fullmatch(r"\d+", out) else None

    def launch(self, env):
        rc, out = self.dcp(120, "device", "process", "launch", "--device", self.device, "--terminate-existing", "--environment-variables", json.dumps(env), self.bid)
        if rc: raise Fail(f"起 App 失败：{(out.strip().splitlines() or [str(rc)])[-1][:200]}（手机要解锁）")

    def nospc(self):   # 刚失败的那次拷贝是不是空间满（cp.log 末尾有 POSIX 28）
        try:
            tail = (self.work / "cp.log").read_text(errors="replace").splitlines()[-40:]
        except OSError:
            return False
        return any(re.search(r"POSIX error code 28[^0-9]|NSPOSIXErrorDomain error 28[^0-9]", l + " ") for l in tail)

    say = staticmethod(lambda m: None)


def device(dev, bid, work, udid=None, devpush=None, via=None, env=None, say=print, docs=DOCS, ctx=None):
    """按平台挑设备操作：Windows（没有 devicectl）或 via=devpush → s2a-devpush（dev 就是 UDID）；Mac → devicectl（有工具 + UDID 时借 s2a-devpush 流式）"""
    env = dict(os.environ) if env is None else env
    udid = udid or env.get("S2A_PUSH_UDID") or env.get("SW_IOS_UDID") or None
    exe = devpush_path(devpush, env)
    if via == "devpush" or (via is None and os.name == "nt"):
        if not exe: raise Fail("没有 s2a-devpush（cargo build --release --manifest-path pack/devpush/Cargo.toml --target-dir build/devpush）")
        d = Devpush(exe, udid or dev, bid, work, env); d.why = f"工具 {exe}"; d.docs, d.ctx = docs, ctx; return d
    d = Devicectl(dev, bid, work, udid, exe, env); d.say = say; d.docs, d.ctx = docs, ctx
    d.why = "没有 s2a-devpush" if not exe else "不知道手机 UDID（S2A_PUSH_UDID）" if not udid else f"工具 {exe}"
    return d


def rows(text, n):   # 制表符分隔、恰 n 列的行 → {路径: 末列}
    out = {}
    for l in (text or "").splitlines():
        w = l.split("\t")
        if len(w) == n: out[w[0]] = w[-1]
    return out


def read(dev, rel, work, name):   # 机上一个文本文件拉回 <work>/<name>；拉不到 → None
    p = Path(work) / name
    return p.read_text(encoding="utf-8", errors="replace") if dev.get(rel, p) else None


def jver(text):
    try: return json.loads(text or "").get("version")
    except (ValueError, AttributeError): return None


def batches_of(pack, todo):
    """→ [[行…]…] 每批 ≤1 GB（按 index.tsv 顺序，单个超 1 GB 的文件自成一批）；行 = [路径, 大小, sha]"""
    out, cur, sz = [], [], 0
    for w in todo:
        n = int(w[1])
        if cur and sz + n > LIM: out.append(cur); cur, sz = [], 0
        cur.append(w); sz += n
    if cur: out.append(cur)
    return out


def plan(dev, pack, work, full=False, say=print):
    """算待推批次：写 <work>/push_NNN.tsv（路径 \\t 大小 \\t sha）与 <work>/pushed（续推账）；→ (批数, 文件数, 字节数)"""
    pack, work = Path(pack), Path(work)
    for f in work.glob("push_*.tsv"): f.unlink()
    if jver((pack / "manifest.json").read_text(encoding="utf-8") if (pack / "manifest.json").is_file() else "") != 2:
        say(f"【红】资源包不是 v2（{pack}/manifest.json）"); raise Fail("资源包不是 v2")
    if full:
        have, pushed, keep = {}, {}, []
        say("资源包：S2A_PUSH_FULL=1，整包推送")
    else:
        names = sorted({m for l in (dev.ls(inc(dev)) or []) for m in STALE.findall(l)})
        keep, bad = [], []
        for n in names: (keep if dev.tar_ok(f"{inc(dev)}/{n}") else bad).append(n)
        if bad:
            e = work / "empty.tar"; e.write_bytes(b"\0" * 1024)
            try:
                for n in bad:
                    try: dev.put(e, f"{inc(dev)}/{n}")
                    except Fail: say(f"盖掉机上残留推送包 {n} 失败"); raise Fail("盖掉残留推送包失败")
            finally:
                e.unlink(missing_ok=True)
            say(f"机上留着上次没解完的推送包 {len(bad)} 个（{' '.join(bad)[:80]}）：已用空包盖掉，不信 .pushed，按已应用索引补推")
        unpack = read(dev, unpack_log(dev), work, "r.unpack.pre")
        cut = bool(bad) or "截短包" in ((unpack or "").strip().splitlines() or [""])[-1]
        if cut and not bad: say(f"上次收推送撞到截短包（{(unpack or '').strip().splitlines()[-1][:60]}…）：不信 .pushed，按已应用索引补推")
        idx = None
        if jver(read(dev, f"{rp(dev)}/manifest.json", work, "r.manifest")) == 2:
            idx = read(dev, f"{rp(dev)}/index.applied.tsv", work, "r.index")
            if not idx and not cut: idx = read(dev, f"{rp(dev)}/index.tsv", work, "r.index")
        have = rows(idx, 3)
        # .pushed 只在机上还有推完整、没解开的批时才算数（推到一半那批的文件不会在里面：一批 put-tar 成功后才追加）
        pushed = rows(read(dev, f"{rp(dev)}/.pushed", work, "r.pushed"), 2) if keep and not cut else {}   # 有残批 / 上次撞截短包：不信 .pushed
        if keep: say(f"机上有推完、还没解开的推送包 {len(keep)} 个：其中 {len(pushed)} 个文件算已推，不重推")
        if have: have = spot_check(dev, pack, work, have, say)
    want = [w for w in (l.split("\t") for l in (pack / "index.tsv").read_text(encoding="utf-8").splitlines()) if len(w) == 3]
    acct = {**have, **pushed}
    todo = [w for w in want if acct.get(w[0]) != w[2]]
    direct = [w for w in todo if IMG.fullmatch(w[0])]
    (work / "direct_img.tsv").write_text("".join("\t".join(w) + "\n" for w in direct), encoding="utf-8")
    bs = batches_of(pack, [w for w in todo if not IMG.fullmatch(w[0])])
    for i, b in enumerate(bs):
        (work / f"push_{i:03d}.tsv").write_text("".join("\t".join(w) + "\n" for w in b), encoding="utf-8")
    (work / "pushed").write_text("".join(f"{r}\t{h}\n" for r, h in pushed.items()), encoding="utf-8")
    return len(bs), len(todo), sum(int(w[1]) for w in todo)


def spot_check(dev, pack, work, have, say):
    """推送前核机上实况（已应用索引是 App 写的账，不等于机上真有）→ 核过的账（可能删掉了对不上的行，或整个清空）"""
    want = {w[0]: w for w in (l.split("\t") for l in (Path(pack) / "index.tsv").read_text(encoding="utf-8").splitlines()) if len(w) == 3}
    same = [w for r, w in want.items() if have.get(r) == w[2]]
    pick = [w for w in same if w[0].startswith("shaders/direct/") and ("/" not in w[0][15:] or re.fullmatch(r"shaders/direct/b-[^/]+/lib\.idx", w[0]))]
    pick += sorted((w for w in same if int(w[1]) < 4 << 20 and w not in pick), key=lambda w: w[2])[:5]
    chk = Path(work) / "chk"; chk.mkdir(exist_ok=True); bad = []
    for i, w in enumerate(pick, 1):
        f = chk / str(i)
        if (dev.get(f"{rp(dev)}/{w[0]}", f, t=30) or dev.get(f"{rp(dev)}/{w[0]}", f, t=60)) and hashlib.sha256(f.read_bytes()).hexdigest() == w[2]:
            continue
        bad.append(w[0])
        if len(bad) == 1: say(f"资源包：机上实况与已应用索引不符：{w[0]} 拉不回或 sha256 不对")
    if not bad:
        say(f"资源包：机上实况抽查 {len(pick)} 个文件，与已应用索引一致"); return have
    lst = dev.listing()
    if lst is None:
        say(f"资源包：抽查 {len(pick)} 个、{len(bad)} 个不符，机上清单拉不回，不信已应用索引，整包重推"); return {}
    rws = [l.split("\t") for l in (Path(work) / "r.index").read_text(encoding="utf-8", errors="replace").splitlines()] if (Path(work) / "r.index").is_file() else []
    rws = [w for w in rws if len(w) == 3]
    keep = {w[0]: w[2] for w in rws if w[0] not in bad and str(lst.get(w[0], -1)) == w[1]}
    say(f"资源包：抽查 {len(pick)} 个、{len(bad)} 个不符；按机上实际清单核账：{len(keep)}/{len(rws)} 行在且大小对得上留下，其余补推")
    return keep


def need_bytes(work, nbytes, env=None):
    """机上要的可写字节 = 待推量 + 最大一批（解开时这批的 tar 和解开的文件同时在，解完一批删一批）+ 每文件 4 KB（块取整）+ S2A_PUSH_SPARE_GB（缺省 1）GB。
    实测（11-08 测试设备2）：动森 11.37 GB、10.5 万文件，峰值 12.40 GB，本式 13.95 GB；原先按待推量 × 2 要 23.8 GB，多拦了空间够的玩家"""
    spare = int((env or os.environ).get("S2A_PUSH_SPARE_GB", "1"))
    biggest = files = 0
    for f in Path(work).glob("push_*.tsv"):
        rows = [l.split("\t") for l in f.read_text(encoding="utf-8").splitlines() if l]
        biggest = max(biggest, sum(int(w[1]) for w in rows)); files += len(rows)
    return nbytes + biggest + files * 4096 + spare * (1 << 30)


def space(dev, nbytes, work, env=None):
    """→ (可写字节或 None, 要的字节)"""
    return dev.free(), need_bytes(work, nbytes, env)


def send(dev, pack, work, say=print):
    """推 plan 写好的批次 → 推了几批；每批写完才把它的文件记进 .pushed 并推上机；最后推索引与清单"""
    pack, work = Path(pack), Path(work)
    files = sorted(work.glob("push_*.tsv"))
    led = work / "pushed"; ledger = led.read_text(encoding="utf-8") if led.is_file() else ""
    nt = nb = ns = nd = done = 0; ts = int(time.time()); t0 = time.time()
    dl = work / "direct_img.tsv"
    direct = [l.split("\t") for l in dl.read_text(encoding="utf-8").splitlines() if l] if dl.is_file() else []
    if direct:   # 先把「已应用」作废：镜像写到一半时机上不能判成就绪（App 看到长度不对会提示重新传）
        e = work / "not_applied"; e.write_bytes(b"")
        try: dev.put(e, f"{rp(dev)}/.applied")
        finally: e.unlink(missing_ok=True)
    for w in direct:
        t1 = time.time()
        try:
            dev.put_big(pack / w[0], f"{rp(dev)}/{w[0]}")
        except Fail as e:
            if dev.nospc(): raise Fail(f"测试设备空间不足（推 {w[0]} 时 POSIX 28）")
            say(f"【红】传 {w[0]} 中断（{e}）；机上这份不完整，不会被当成可用，重新装机会从头重传")
            raise Fail(f"资源传输中断：{w[0]} 没传完，重新装到 iPhone 会从头再传一次")
        done += int(w[1]); nt += 1; ledger += f"{w[0]}\t{w[2]}\n"; led.write_text(ledger, encoding="utf-8"); dev.put(led, f"{rp(dev)}/.pushed")
        say(f"  直写 {w[0]}：{int(w[1]) / 1e9:.2f} GB，{time.time() - t1:.0f} 秒，{int(w[1]) / 1e6 / max(time.time() - t1, 1e-3):.1f} MB/s")
    for k, bf in enumerate(files):
        b = [l.split("\t") for l in bf.read_text(encoding="utf-8").splitlines() if l]
        lst = work / "push.lst"; lst.write_text("".join(w[0] + "\n" for w in b), encoding="utf-8")
        tn = f"{ts}_{k:03d}.tar"
        try:
            how = dev.put_tar(lst, pack, f"{inc(dev)}/{tn}")
        except Fail:
            if dev.nospc():
                say(f"【红】推 {tn} 时测试设备空间满（POSIX 28）"); raise Fail(f"测试设备空间不足（推 {tn} 时 POSIX 28）")
            say(f"推 {tn} 失败"); raise Fail(f"推 {tn} 失败")
        finally:
            lst.unlink(missing_ok=True)
        ns += how == "stream"; nd += how != "stream"
        sz = sum(int(w[1]) for w in b); done += sz; nt += len(b); nb += 1
        ledger += "".join(f"{w[0]}\t{w[2]}\n" for w in b); led.write_text(ledger, encoding="utf-8"); dev.put(led, f"{rp(dev)}/.pushed")
        if len(files) > 1:
            say(f"  批 {k + 1}/{len(files)}：{sz >> 20} MB，累计 {done / 1e9:.2f} GB，{done / 1e6 / max(time.time() - t0, 1e-3):.1f} MB/s")
    for f in ("index.tsv", "manifest.json"):
        try:
            dev.put(pack / f, f"{rp(dev)}/{f}")
        except Fail:
            if dev.nospc(): raise Fail(f"测试设备空间不足（推 {f} 时 POSIX 28）")
            say(f"推 {f} 失败"); raise Fail(f"推 {f} 失败")
    say(f"资源包：推 {nt} 个文件 {done >> 20} MB，分 {nb} 批（流式 s2a-devpush {ns} 批 / devicectl {nd} 批；{dev.why}）+ 索引 + 清单（{time.time() - t0:.0f} 秒）")
    return nb


# 等机上解包的判据（11-07 根修）：不按假定写速算总上限，按解包日志的进度行看——包数还在涨就一直等，连续 STALL_S 秒没有新进展才判失败。
#   原先「上限 = 字节 ÷ 50 MB/秒、至少 5 分钟」：11-07 测试设备1 旷野之息（17 包 48895 个文件 16483 MB）机上实测 463 秒解完（约 36 MB/秒），
#   330 秒上限先到，装机作业报「解包没在规定时间内完成」（rc=1）而 App 其实在正常解——玩家看到假失败。
#   STALL_S = 180 的依据：同一次实测 unpack.log 单包（≤1 GB）最慢 43.5 秒（第 14 包 7728 个文件 1023 MB），App 冷起到第一行进度 38 秒（含第 1 包 33.1 秒）；
#   取单包最慢的约 4 倍，比任何一包正常耗时都长，又不至于真卡住时让玩家干等十几分钟
STALL_S = 180
OPEN_BY_HAND = "在 iPhone 上打开 S2Arm，选这款游戏，保持在前台别锁屏，等它导入完就能玩"
PROGRESS = re.compile(r"进度：解开 \S+（(\d+)/(\d+)）")


def apply(dev, pack, work, nb, say=print, sleep=time.sleep, now=time.time, stall=STALL_S):
    """推完起一次 App 只解包（S2A_PACK_APPLY_ONLY=1，runtime/src/pack_apply.c）并等它解完：机上 .applied = 本包 index_sha256，且解包日志
    本次新增完成行「解开 N 个包 … .applied = <本包>」（只看 .applied 不够：同包重推时推之前就对得上，11-06 对照实测）。
    等待按进度：本次新增的进度行「进度：解开 <包>（k/n）」k 涨了就重新计时、打一行「机上解包 k/n」（装机作业据此发进度事件，pack/jobs/install.py）；
    连续 stall 秒 k 没涨 → 判失败。没推东西且 .applied 已是本包 → 不起 App。失败抛 Fail"""
    want = json.loads((Path(pack) / "manifest.json").read_text(encoding="utf-8")).get("index_sha256")
    def applied(): return (read(dev, f"{rp(dev)}/.applied", work, "r.applied") or "").strip()
    if not nb and applied() == want:
        return False
    u0 = len((read(dev, unpack_log(dev), work, "r.unpack0") or "").splitlines())
    done = re.compile(r"解开 \d+ 个包 .*\.applied = " + re.escape(want or "-"))
    say("起一次 App 解开推送包（S2A_PACK_APPLY_ONLY=1）")
    cant_open = None
    try:
        dev.launch({"S2A_PACK_APPLY_ONLY": "1", **({"S2A_LAUNCH_CTX": dev.ctx} if dev.ctx else {})})   # 容器：起哪一款由启动上下文定
    except Fail as e:   # 拉起靠电脑侧的设备服务，玩家机上常常没有（#192）；资源已在手机上，照样等玩家手动打开后解完
        cant_open = str(e); say(f"资源包已传完。{OPEN_BY_HAND}")
    seen, last = 0, now()
    while True:
        new = (read(dev, unpack_log(dev), work, "r.unpack1") or "").splitlines()[u0:]
        if applied() == want and any(done.search(l) for l in new):
            say("资源包：机上已解开（.applied = 本包）"); return True
        got = [(int(m.group(1)), int(m.group(2))) for m in map(PROGRESS.search, new) if m]
        if got and got[-1][0] > seen:
            seen, last = got[-1][0], now(); say(f"机上解包 {seen}/{got[-1][1]}")
        elif now() - last > stall:
            break
        sleep(3)
    if cant_open:
        raise NeedsPlayer(f"资源包已传完，还差最后一步：{OPEN_BY_HAND}（电脑这边打不开手机上的 App：{cant_open}）")
    raise Fail(f"资源包推进去了，但机上解包 {stall} 秒没有新进展（停在第 {seen} 包）：打开这款游戏、保持在前台等它导入完" if seen else
               f"资源包推进去了，但起 App 后 {stall} 秒机上解包没有开始：打开这款游戏、保持在前台等它导入完")


def run(dev, pack, work, full=False, say=print, env=None, on_plan=None):
    """一条龙：算 → 核空间（不够报红，读不到照推）→ 推 → (推了几批, 文件数, 字节数)"""
    nb, nt, nbytes = plan(dev, pack, work, full, say)
    if on_plan: on_plan(nb, nt, nbytes)
    if nbytes:
        fb, need = space(dev, nbytes, work, env)
        if fb is None: say("🟡 读不到测试设备剩余空间，不核、照推")
        elif fb < need:
            say(f"【红】测试设备剩余空间不足：可写 {fb >> 30} GB < 要 {need >> 30} GB（待推 {nbytes >> 30} GB + 解开时最大一批 + 余量）；先卸掉机上不用的包")
            raise Fail("测试设备空间不足")
    send(dev, pack, work, say)
    return nb, nt, nbytes


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"): s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="推资源包（Mac / Windows 共用）")
    ap.add_argument("op", choices=("run", "plan", "free", "send", "need")); ap.add_argument("device"); ap.add_argument("bundle_id"); ap.add_argument("pack")
    ap.add_argument("--work"); ap.add_argument("--full", action="store_true"); ap.add_argument("--udid"); ap.add_argument("--devpush")
    ap.add_argument("--via", choices=("devpush", "devicectl")); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--docs", default=DOCS); ap.add_argument("--launch-ctx")   # 多款容器：本款设备侧文档根 Documents/games/<游戏号>、起 App 解包时的 S2A_LAUNCH_CTX   # run 推完起一次 App 只解包并等解完（s2armctl push-pack / Windows 装机）
    a = ap.parse_args(argv)
    pack = Path(a.pack)
    if not (pack / "index.tsv").is_file():
        print(f"【红】资源包目录不在或不是 v2（缺 index.tsv）：{pack}"); return 1
    stamp = a.op == "run"
    def say(m): print((f"[{time.strftime('%H:%M:%S')}] " if stamp else "") + m, flush=True)
    tmp = None
    if a.work: work = Path(a.work); work.mkdir(parents=True, exist_ok=True)
    elif stamp: tmp = tempfile.TemporaryDirectory(dir=os.environ.get("S2A_PUSH_TMP")); work = Path(tmp.name)
    else: ap.error(f"{a.op} 要 --work")
    state = work / "push.state"
    try:
        dev = device(a.device, a.bundle_id, work, a.udid, a.devpush, a.via, say=say, docs=a.docs.rstrip("/"), ctx=a.launch_ctx)
        if a.op == "free":
            fb = dev.free(); print(fb if fb is not None else "", flush=True); return 0
        if a.op == "need":   # plan 之后：机上要的可写字节（tools/push_pack.sh）
            print(need_bytes(work, int((state.read_text().split() + ["0"] * 4)[3])), flush=True); return 0
        if a.op == "plan":
            nb, nt, nbytes = plan(dev, pack, work, a.full, say); state.write_text(f"PLAN {nb} {nt} {nbytes}\n"); return 0
        if a.op == "send":
            state.write_text(f"SENT {send(dev, pack, work, say)}\n"); return 0
        say(f"推资源包：设备 {a.device} 包 {a.bundle_id} 目录 {pack}" + ("（--full 整包重推）" if a.full else ""))
        nb, _, _ = run(dev, pack, work, a.full, say)
        if a.apply: apply(dev, pack, work, nb, say)
        else: say(f"资源包：推完 {nb} 批；起一次 App 让它解开 .incoming（S2A_PACK_APPLY_ONLY=1 只解包不跑游戏）" if nb else "资源包：机上已是最新，不用推")
        return 0
    except Fail as e:
        state.write_text(f"FAIL {e}\n")
        if stamp: print(f"【红】{e}"); print(f"RESULT=push out= reason={e}")
        return 1
    finally:
        if tmp: tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
