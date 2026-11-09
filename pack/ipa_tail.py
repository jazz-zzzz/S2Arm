#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""出包收尾（#1100 Windows 出包，11-04 维护者定归 Windows 出包线）：预编壳 + 产物根 → <工作根>/pkg/<款>.ipa 与资源包 v2 <工作根>/pkg/pack/。
Mac 的 pack/ipa.sh 与 Windows 的 Python 编排调同一份（原 ipa.sh 这一段的 bash 搬来，只留一份实现）。不碰产物根事务、包键、存档挑选：那些在调用方。

  ipa_tail.py --work <工作根> --game <款> --shell <壳目录> --bundle-id <包名> --suffix <显示名后缀> --key <包键串> --title <游戏名>
              --sign none|adhoc|dev [--team <签名团队>] [--save-identity <存档身份 json>] [--app-only]
  步骤：组装（ship_ext build）→ 存档身份 → 图标 → Info.plist（包号 / 着色器目标 / 画面模式 / 出包时间）→ 签名 → 提高内存上限权限核实 → 打 ipa
        → Metal 3 辅助库进包核对 →（--app-only 到此为止）→ 资源包要件核对 → 资源包（pack.py）→ 资源包核对（verify.py）
  签名：none = 不签（Windows / 交给侧载工具签：壳主程序与游戏镜像包已各自带 ad-hoc 签名，侧载工具重签）；adhoc = 本机 codesign 自签（只 Mac）；
        dev = 开发直签（只 Mac，pack/dev_sign.py，缺描述文件时 ensure_profile.sh 补）
  日志：<工作根>/pkg/build.log、sign.log、pack.log、verify.log；每步一行计时追加到 <工作根>/pkg/timing.tsv（步 墙钟 user sys 退出码，同 tools/tm.py；Windows 取不到子进程 CPU 记 0）
  失败：标准输出打一行「【红】…」、退出 1（调用方 fail 收尾）；产物根有直连库就带上（同 pkg_srcs.srcs）
"""
import argparse
import datetime
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

H = os.path.dirname(os.path.abspath(__file__)); R = os.path.dirname(H)
sys.path.insert(0, H)
import pkg_srcs  # noqa: E402

PY = os.environ.get('S2A_PY') or sys.executable


class Red(Exception): pass


PROG = [int(os.environ.get('S2A_PKG_PROG_DONE', '0')), int(os.environ.get('S2A_PKG_PROG_TOTAL', '5'))]   # 出包进度：调用方已走的步数 / 总步数（出壳那步在调用方）


def dev_facilities():
    """Info.plist S2ADevFacilities：启动器给玩家出包（S2A_PACK_PURPOSE=user）写 "0"，宿主不起停住现场 / 地址走势 / 内存与音频周期账；
    别的用途（实验 / 普查 / 手测）返回空串 = 删键 = 开发设施照开（11-07 质量线，cmdr/壳内开发设施盘点.md）"""
    return '0' if os.environ.get('S2A_PACK_PURPOSE') == 'user' else ''

def tick():   # = tools/progress.sh prog_tick 出包 <已走> <总>（启动器进度条读 S2A_PROGRESS）
    PROG[0] += 1
    if os.environ.get('S2A_PROGRESS'):
        with open(os.environ['S2A_PROGRESS'], 'a', encoding='utf-8', newline='\n') as f: f.write(f'{int(time.time())}\t出包\t{PROG[0]}\t{PROG[1]}\n')


def step(timing, name, cmd, log=None, mode='a', env=None):
    """跑一步并记一行计时（同 tools/tm.py）；log 给了就把标准输出 / 错误写进去"""
    try:
        import resource; r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    except ImportError: resource = None; r0 = None
    t0 = time.time()
    if log:
        with open(log, mode, encoding='utf-8', errors='replace') as f: rc = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
    else: rc = subprocess.call(cmd, env=env)
    w = time.time() - t0
    u = s = 0.0
    if resource:
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN); u, s = r1.ru_utime - r0.ru_utime, r1.ru_stime - r0.ru_stime
    with open(timing, 'a', encoding='utf-8', newline='\n') as f: f.write(f'{name}\t{w:.1f}\t{u:.1f}\t{s:.1f}\t{rc}\n')
    tick()
    return rc


def test_udids():
    """我方测试设备硬件 UDID（tools/devices.sh 名单，真值在本机 tools/machine.local），空格分隔；没有本机名单（玩家机 / 贡献者）只有全 0 占位 → 空，不核。
    11-07 满屏线：Duo 登记前申请的包身份（本机 40 / 165 个）描述文件不含 Duo，装 Duo 报「This provisioning profile cannot be installed on this device」；
    原先只在完全没有描述文件时才申请，缺设备的旧描述文件一直被挑中"""
    sh = os.path.join(R, 'tools', 'devices.sh')
    if not os.path.isfile(sh): return ''
    from orch.bash import find_bash
    bash = find_bash()
    if not bash: return ''
    r = subprocess.run([bash, '-c', '. "$1" > /dev/null 2>&1; for n in $(s2a_device_ids any); do s2a_device_field "$n" udid; done', '_', sh],
                       capture_output=True, text=True)
    if r.returncode: return ''
    return ' '.join(u for u in r.stdout.split() if len(u.replace('-', '').lstrip('0')) > 1)   # 占位 00000000-000000000000000N 去掉


def run(cmd, log):   # 不计时的小步：输出追加进日志，失败返回非 0
    with open(log, 'a', encoding='utf-8', errors='replace') as f: return subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT)


def last(log, red=False):
    try: lines = open(log, encoding='utf-8', errors='replace').read().splitlines()
    except OSError: return ''
    if red:
        r = [l for l in lines if '【红】' in l]
        if r: return r[-1]
    return lines[-1] if lines else ''


def archive_reason(log):
    """「打 ipa」失败时要带出去的那一行：write_ipa 自己的红行优先，没有就取日志最后一行（python traceback 的最后一行 = 异常本身）。
    11-08 反馈（9.78 GB 的 PERSONA4 The GOLDEN 停在出包 2/5、错误码 package_failed「原因没认出来」）：这一步原先不带日志、
    失败只说一句「打 ipa 失败」，真正的原因（体积账 / errno / zipfile 异常）既没进红行也没进摘要，归因只能落到 package_failed。"""
    red = last(log, True)
    return red if '【红】' in red else last(log)


def build_id(key):   # = pack/build_id.sh s2a_build_id：<提交号>-<整串包键（含末尾换行）的 sha256 前 8 位>
    return f"{key.split(' ', 1)[0]}-{hashlib.sha256((key + chr(10)).encode()).hexdigest()[:8]}"


def pso_target(w):   # = s2a_pso_target：shaders/gal_pso.<架构>.<系统>.N.bin → 「<架构>.<系统>」去重排序逗号连
    ts = set()
    for f in glob.glob(os.path.join(glob.escape(w), 'shaders', 'gal_pso.*.bin')):
        if not os.path.isfile(f): continue
        t = os.path.basename(f)[len('gal_pso.'):]
        m = re.match(r'(.*)\.[^.]*\.bin$', t)   # bash ${t%.*.bin}：去掉最短的「.<任意>.bin」后缀
        ts.add(m.group(1) if m else t)
    return ','.join(sorted(ts, key=lambda s: s.encode()))


def direct_lib_version(w):   # 包里直连库的版本（b-<链> / vk / gl）；没有 = 空（早装件此刻还没有直连库）
    d = os.path.join(w, 'shaders', 'direct')
    bs = sorted(glob.glob(os.path.join(glob.escape(d), 'b-*')))
    v = os.path.basename(bs[0]) if bs else ''
    if v and pkg_srcs.nonempty(os.path.join(d, 'keys.tsv')): return v
    if pkg_srcs.nonempty(os.path.join(d, 'vk', 'vk_shaders.tsv')): return 'vk'   # Vulkan 同 GL 以表为准：RomFS 没有 SPIR-V 的款只有表头、没有 metallib（设备首遇现编）
    if pkg_srcs.nonempty(os.path.join(d, 'gl', 'gl_shaders.tsv')): return 'gl'   # GL 以表为准：离线 0 支的款只有表头、没有 metallib（宿主首遇现编）
    return ''


def _writable(func, path, _):   # rmtree 碰到只读项：加写权限再删（壳里拷来的文件可能只读）
    os.chmod(path, 0o700); func(path)


def assemble(a):
    w, g = a.work, a.game
    pkg = os.path.join(w, 'pkg'); os.makedirs(pkg, exist_ok=True)
    tl, bl, sl = os.path.join(pkg, 'timing.tsv'), os.path.join(pkg, 'build.log'), os.path.join(pkg, 'sign.log')
    al = os.path.join(pkg, 'archive.log')   # 打 ipa 自己的输出（原先直接落进调用方的 tail.out，失败时随临时产物根一起删掉）
    app = os.path.join(w, 'build', 'ios', 'S2Arm.app')
    if os.path.lexists(os.path.join(w, 'build')): shutil.rmtree(os.path.join(w, 'build'), onerror=_writable)   # 上次组装的临时 App（重跑 / 失败残留）
    icon = os.path.join(R, 'launchers/macos/Branding/AppIcon-1024.png')   # 宿主 App 永远用 S2Arm 自己的图标；游戏图标只给库里的入局入口（11-08 维护者）
    if not os.path.isfile(icon): raise Red(f'引擎里缺 S2Arm 图标（{icon}）')
    if step(tl, '组装（预编壳）', [PY, os.path.join(H, 'ship_ext.py'), 'build', '--shell', a.shell, '--root', w, '--out', app,
                                   '--bundle-id', a.bundle_id, '--suffix', a.suffix, '--icon', icon], bl):
        raise Red(f'预编壳组装失败（{last(bl, True)}）')
    if not os.path.isdir(app): raise Red(f'组装没产出 {app}')
    if a.save_identity:
        si = os.path.join(w, 'save-identity.json')
        if run([PY, os.path.join(H, 'save_identity.py'), '--check', a.save_identity, '--output', si], bl): raise Red('存档游戏身份校验失败')
        shutil.copyfile(si, os.path.join(app, 'save-identity.json'))
    if run([PY, os.path.join(H, 'app_bundle.py'), 'write_icons', icon, app], bl): raise Red(f'加图标失败（{last(bl)}）')
    kv = [('S2ABuildID', build_id(a.key)), ('S2APsoTarget', pso_target(w)), ('S2AGameTitle', a.title), ('S2AGraphicsMode', 'direct'),   # 只有直连（11-07 拆通用模式）
          ('S2ADirectLibVersion', direct_lib_version(w)),
          ('S2APackTime', datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')),
          ('S2ADevFacilities', dev_facilities())]   # 开发设施开关（runtime/src/os.c s2a_dev_on）：玩家包写 "0"，开发包删键（= 开）
    for k, v in kv:
        if run([PY, os.path.join(H, 'app_bundle.py'), 'update_metadata', app, '--set', f'{k}={v}'], bl): raise Red(f'Info.plist 写 {k} 失败')
    ent = os.path.join(H, 'app.entitlements')
    if a.sign == 'adhoc':
        if step(tl, '签名', ['bash', os.path.join(H, 'oneclick', 'adhoc_sign.sh'), app, ent], sl, 'w'): raise Red(f'自签失败（{last(sl)}）')
    elif a.sign == 'dev':
        os.environ.setdefault('S2A_PROFILE_NEED_UDIDS', test_udids())   # 描述文件要含的我方测试设备（dev_sign 缺了退 3 → ensure_profile 换新）
        dev = ['/usr/bin/python3', os.path.join(H, 'dev_sign.py'), app, a.bundle_id, a.team, ent]
        rc = step(tl, '签名', dev, sl, 'w')
        if rc == 3 and not run(['bash', os.path.join(H, 'ensure_profile.sh'), a.bundle_id, a.team, ent], sl): rc = run(dev, sl)
        if rc: raise Red(f"签名失败（{last(sl, True) or last(sl)}；日志 {sl}）")
    else:
        with open(sl, 'w', encoding='utf-8') as f: f.write('不签（S2A_SIGN=none）：壳主程序与游戏镜像包各自带 ad-hoc 签名，侧载工具重签\n')
    # 签后核实提高内存上限权限（缺 IML 或含私有 JIT 键判红、不出包）
    if run([PY, os.path.join(H, 'iml_check.py'), 'check_app', app], sl):
        raise Red(last(sl, True) if '【红】' in last(sl, True) else f'签名核实失败：提高内存上限权限缺失或含私有 JIT 键，见 {sl}')
    ipa = os.path.join(pkg, f'{g}.ipa')
    if os.path.lexists(ipa): os.remove(ipa)   # 上次的 ipa（write_ipa 不覆盖已有目标）
    if step(tl, '打ipa', [PY, os.path.join(H, 'app_bundle.py'), 'write_ipa', app, ipa], al, 'w'):
        reason = archive_reason(al).removeprefix('【红】').strip()   # 红标记去掉：外层 fail 会再加一个，摘要里不叠
        raise Red(f'打 ipa 失败（{reason or "没有输出"}）')
    # 进包核对：ipa 里 gal_helpers/m3/ 的库文件名集合 = 壳的 m3/（Metal 3 兼容辅助库漏拷时预热静默现编）
    hl = os.path.join(a.shell, 'root', 'gal_helpers', 'm3')
    want = sorted((n for n in os.listdir(hl) if n.endswith('.metallib')), key=lambda s: s.encode()) if os.path.isdir(hl) else []
    with zipfile.ZipFile(ipa) as z:
        got = sorted((n.rsplit('/', 1)[1] for n in z.namelist() if re.match(r'Payload/[^/]+\.app/gal_helpers/m3/[^/]+\.metallib$', n)), key=lambda s: s.encode())
    if not want or want != got: raise Red(f'ipa 里 Metal 3 兼容辅助库与构建目录不符（构建 {len(want)} 个、包里 {len(got)} 个）')
    if a.app_only: return ipa
    check_pack_inputs(w, g)
    srcs = os.path.join(pkg, 'srcs.txt')
    with open(srcs, 'w', encoding='utf-8', newline='\n') as f: f.write(''.join(f'{x}={y}\n' for x, y in pkg_srcs.srcs(w)))
    pl, vl = os.path.join(pkg, 'pack.log'), os.path.join(pkg, 'verify.log')
    if step(tl, '资源包', [PY, os.path.join(H, 'pack.py'), os.path.join(pkg, 'pack.files'), '@' + srcs], pl, 'w'): raise Red(f'资源包失败（{last(pl)}）')
    if step(tl, '资源包核对', [PY, os.path.join(H, 'verify.py'), os.path.join(pkg, 'pack.files'), '@' + srcs], vl, 'w'): raise Red(last(vl))
    il = os.path.join(pkg, 'image.log')   # 按文件的资源包 pack.files 核对过才转镜像；pack.files 留着给下次增量，推手机的是 pack
    if step(tl, '单文件镜像', [PY, os.path.join(H, 'romfs_image.py'), os.path.join(pkg, 'pack.files'), os.path.join(pkg, 'pack'), '--root', w], il, 'w'): raise Red(f'单文件镜像失败（{last(il)}）')
    return ipa


def runtime_only_lib(d):
    """抽取缺口出的空直连库（graphics/tools/direct/runtime_only_lib.sh 出，pack/stage_direct.sh 落进产物根）：
    runtime_only.txt + 空的 shader_map.tsv + 每个 b-<链>/ 的 lib.idx / inputs.tsv。这是一份合法的库——离线 0 支，宿主首遇现编
    （运行时判定看 b-<链> + shader_map.tsv + lib.idx，见 runtime_only_lib.sh 头注）；空 keys.tsv / shader_map.tsv 是它的正常形态，
    不能按「直连库不全」判红（#11：9.78 GB 的 PERSONA4 The GOLDEN 抽取缺口走这条路，出包被这一步挡死）。"""
    if not os.path.isfile(os.path.join(d, 'runtime_only.txt')): return False
    if not os.path.isfile(os.path.join(d, 'shader_map.tsv')): return False   # 宿主判直连起手要的三样之一（另外两样在 b-<链>/ 里）
    chains = sorted(glob.glob(os.path.join(glob.escape(d), 'b-*')))
    return bool(chains) and all(os.path.isfile(os.path.join(c, n)) for c in chains for n in ('lib.idx', 'inputs.tsv'))


def check_pack_inputs(w, g):
    """资源包要件（资源包根 = 游戏数据目录：exefs/main.npdm、romfs/、ctl/control.nacp；起局把 S2A_GAME_DATA 指到 Documents/资源包）"""
    if not os.path.isfile(os.path.join(w, 'exefs', 'main.npdm')): raise Red('产物根没有 exefs/main.npdm')
    if not os.path.isfile(os.path.join(w, 'ctl', 'control.nacp')): print(f'【黄】{g} 产物根没有 ctl/control.nacp（游戏标题 / 存档参数读不到，服务层会报红）')
    if not os.path.isdir(os.path.join(w, 'systemdata', 'fonts')): raise Red(f'产物根没有 systemdata/fonts（先 pack/stage_sysdata.sh {g}）')
    d = os.path.join(w, 'shaders', 'direct')
    runtime_only = runtime_only_lib(d)
    if not pkg_srcs.nonempty(os.path.join(d, 'keys.tsv')) and not pkg_srcs.nonempty(os.path.join(d, 'gl', 'gl_shaders.tsv')) and not runtime_only:
        try: dl = [l for l in open(os.path.join(w, 'direct_lib.log'), encoding='utf-8', errors='replace').read().splitlines() if '直连库]' in l]
        except OSError: dl = []
        print(f'🟡 {g} 资源包没带直连库（产物根没有 shaders/direct/keys.tsv：{dl[-1][:160] if dl else "早装件或直连库阶段未产出"}）；装上起局会报「游戏文件缺失」，要装带直连库的正式包')
    elif runtime_only:   # 空库照常进包：宿主按直连起，没编过的着色器首遇现编（新场景第一次会卡一下）
        print(f'🟡 {g} 本款离线直连库为空（抽取缺口，见产物根 shaders/direct/runtime_only.txt）：着色器运行时现编，新场景首次遇到会卡一下')
    chains = sorted(glob.glob(os.path.join(glob.escape(d), 'b-*')))
    if chains and not runtime_only:   # 带 b-<链>/ 就必须同时有 keys.tsv 与 shader_map.tsv（App 判直连起手看 b-<链>/）；空库由 runtime_only_lib 那一支认
        if not (pkg_srcs.nonempty(os.path.join(d, 'keys.tsv')) and pkg_srcs.nonempty(os.path.join(d, 'shader_map.tsv'))):
            raise Red(f'直连库不全：有 {os.path.basename(chains[0])}/，缺 keys.tsv 或 shader_map.tsv（直连库阶段重出）')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == 'build-id': print(build_id(sys.argv[2])); return 0   # pack/build_id.sh s2a_build_id 调
    p = argparse.ArgumentParser(description='出包收尾：预编壳 + 产物根 → ipa 与资源包')
    p.add_argument('--work', required=True); p.add_argument('--game', required=True); p.add_argument('--shell', required=True)
    p.add_argument('--bundle-id', required=True); p.add_argument('--suffix', default=''); p.add_argument('--key', required=True)
    p.add_argument('--title', required=True); p.add_argument('--sign', choices=('none', 'adhoc', 'dev'), required=True)
    p.add_argument('--team', default=''); p.add_argument('--save-identity', default=''); p.add_argument('--app-only', action='store_true')
    a = p.parse_args()
    if a.sign == 'dev' and not a.team: print('【红】开发直签要 --team'); return 1
    if a.sign != 'none' and sys.platform != 'darwin': print(f'【红】签名方式 {a.sign} 只能在 Mac 上（Windows 用 none，交给侧载工具签）'); return 1
    try: assemble(a)
    except Red as e: print(f'【红】{e}'); return 1
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    sys.exit(main())
