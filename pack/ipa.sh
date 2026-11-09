#!/bin/bash
# 出包·收尾阶段：pack/ipa.sh <款> [--register] [--all-caps | --native]
# 在新版本产物根 <款>@pkg-<本树提交> 里出可上机的包目录 pkg/：
#   ① 预编壳（与游戏无关，按提交出一次缓存，pack/shell_build.sh）+ 本款组装（pack/ship_ext.py build：游戏镜像写进 Frameworks/S2AImages.dylib、随行表进包）→ S2Arm.app（构建目录放在产物根里，不和别的构建共用 build/）
#   ② pack/dev_sign.py 开发直签（包名 io.s2arm.<款>，基础权限 pack/app.entitlements：只有「提高内存上限」）
#   ③ 打 pkg/<款>.ipa（Payload/S2Arm.app）
#   ④ 资源包 v2：pkg/pack ← exefs/main.npdm + romfs/ + ctl/control.nacp + systemdata/（字体、时区，pack/stage_sysdata.sh 放），pack/verify.py 逐字节核对
# 全绿才提交新版本并切现行；--register 再经维护方调度 维护方工具 pkg 登记（调度补位即用；没设 S2A_PRIVATE_TOOLS 报红）。任一步红：中止新版本、现行不动。
# --all-caps（10-28 全能力出包）：包内能力清单换成产物根里的 s2a_caps.all.tsv（有来源即开，改写阶段出），组装时拷进包、运行时设环境变量（未设时）。
#   正式出包缺省不带（对外仍按验证口径）；环境变量仍优先（调度器 A/B 不受影响）。
# --native（11-02 维护者：剩余普查只证原生打通，自动玩家进不了局的款装测试设备手玩）：发布口径清单里 widen / fps60 改成 0（16:9、游戏原帧率），其余能力不动。与 --all-caps 互斥。
set -u
G=${1:?款}; REG=0; ALLCAPS=0; NATIVE=0
shift; while [ $# -gt 0 ]; do case "$1" in --register) [ -n "${S2A_PRIVATE_TOOLS:-}" ] || { echo "【红】--register 是维护方调度登记，要设 S2A_PRIVATE_TOOLS"; exit 1; }; REG=1;; --all-caps) ALLCAPS=1;; --native) NATIVE=1;; *) echo "【红】不认的选项 $1"; exit 1;; esac; shift; done
# S2A_ALL_CAPS=1 同 --all-caps（11-01 维护者：packq 的实验 / 装真机单经环境变量给，宿主快路 tools/app_only.sh 调本脚本时也带上）
[ "${S2A_ALL_CAPS:-0}" = 1 ] && ALLCAPS=1
[ $ALLCAPS = 1 ] && export S2A_ALL_CAPS=1   # 交给刷新 / 宽屏自动出表（graphics/widen/scan/auto_pkg.sh wanted）：满屏开着的包才起闭环
[ $ALLCAPS = 1 ] && [ $NATIVE = 1 ] && { echo "【红】--all-caps 与 --native 不能同时给"; exit 1; }
# 普通堆与别名区由同一份按真实测量生成的地址需求表决定。
H=$(cd "$(dirname "$0")" && pwd); source "$H/ipa_key.sh"
[ -z "${IOS_VAMM_LAYOUT+x}" ] || { echo "【红】IOS_VAMM_LAYOUT 已停用；请使用自动地址需求表重新出包"; exit 1; }
R=$(cd "$H/.." && pwd); source "$H/roots.sh"; . "$R/tools/machine.sh"
source "$R/tools/toolchain.sh" || exit 2
# 并行度按本机核数（tools/cores.sh）；出包各步进度写进 S2A_PROGRESS（tools/progress.sh；10-25 维护者「能看到进度么」「要适配机器的核心数」）
source "$R/tools/cores.sh"; s2a_cores || exit 1; source "$R/tools/progress.sh"   # 统一工具链（tools/toolchain.env）：编 iOS、着色器预编、NativeAOT 都用这一套
# 签名方式 S2A_SIGN（一键出包 pack/oneclick/ 用；不设 = dev，与原来相同）：dev = 开发直签（要签名团队）；adhoc = 本机自签 codesign -s -、只带 pack/app.entitlements（提高内存上限），
#   给 AltStore / Sideloadly / Xcode 个人团队侧载时按原签名里的权限重签，不要团队、不登记调度
SIGN=${S2A_SIGN:-dev}; case $SIGN in dev|adhoc|none) ;; *) echo "【红】S2A_SIGN 只认 dev / adhoc / none"; exit 1;; esac   # none = 不签（交给侧载工具签，Windows 出包用）
[ "$SIGN" != dev ] && [ $REG = 1 ] && { echo "【红】${SIGN} 包不登记调度（测试设备只装开发直签包）"; exit 1; }
TEAM=${S2A_TEAM:-${S2A_TEAM_DEFAULT:-}}; [ -n "$TEAM" ] || [ "$SIGN" != dev ] || { echo "【红】没设 S2A_TEAM（复制 tools/machine.local.example 为 tools/machine.local 并填写签名团队）"; exit 1; }
C=$(git -C "$R" rev-parse --short=9 HEAD) || exit 1
# 源码树干净才出（包号里的提交号要对得上源码）：防开发者用改了没提交的树出包。发布快照（App 内置引擎，根有 engine.json 清单）
#   安装时已按清单逐文件核过 sha256、玩家机上没人改它，这条在玩家路径上没有意义，不判（11-02 维护者；beta.3 走查：快照缺
#   根 .gitignore 时编译产物被当改动，玩家导出 iPhone 版全红——.gitignore 已补进快照，作第二道）
src_dirty() { [ -f "$1/engine.json" ] && return 1; [ -n "$(git -C "$1" status --porcelain -- runtime ios graphics translator services compat Makefile)" ]; }
src_dirty "$R" && { echo "【红】本树构建相关目录有未提交改动，包版本对不上提交号，不出"; exit 1; }
# 同一产物根、同款、同提交只许一份在出（10-20 20:5x 我手出与自动正式局同时出动森 c85d1335a，两份抢 CPU）：后到的等先到的出完，
#   再走下面的正常流程——先到的已写好包键，后到的命中「现行已是包」直接登记，复用它的结果
#   锁是内核文件锁（tools/plock.sh，审计 P1-27：原先 mkdir 锁目录后才写 pid，空 pid 当死锁删、两个等待者可按旧读数删掉新锁）
PL=${S2A_PACK_LOCKS:-${S2A_RUN_ROOT:-$HOME/.s2arm/run}/s2arm/pack_lock}/$G@$C@$(printf %s "$S2A_ROOTS" | cksum | cut -d' ' -f1).lock
source "$R/tools/plock.sh"
plock_take "$PL" 0 || { echo "$G 同提交 $C 已有出包在跑（pid $(plock_holder "$PL")），等它出完复用"
  plock_take "$PL" 2400 || { echo "【红】$G 同提交 $C 已有出包（pid $(plock_holder "$PL")）跑了 2400 秒还没完，不等了"; exit 1; }; }
PL_PID=$PLOCK_PID; trap 'plock_release "$PL_PID"' EXIT
# 应用身份按根定（10-21 维护者：实验包用独立身份、不得覆盖正式款）：共享根 = 正式款 io.s2arm.<款>；私有根 = 实验身份 io.s2arm-exp.<款>（本机有描述文件，显示名「<游戏名> S2Arm 实验」）
if _root_identity_shared; then BID=io.s2arm.$G; EXP=; else BID=io.s2arm-exp.$G; EXP=1; fi
# 共享根只出正式包（测试出包线 10-23）：共享根的 pkg/ 就是正式包的位置，自签 / 改包名 / 改显示名的包会把它覆盖，一律报红
# S2A_DISPLAY_NAME 已停用（11-04 维护者：显示名只从游戏自带 control.nacp 取，pack/display_name.py）：还设着的调用方报红，不静默忽略
[ -z "${S2A_DISPLAY_NAME:-}" ] || { echo "【红】S2A_DISPLAY_NAME 已停用：显示名一律「<游戏自带名> S2Arm」（pack/display_name.py）；要区分的包用 S2A_DISPLAY_SUFFIX=<后缀>"; exit 1; }
if [ -z "$EXP" ]; then for v in S2A_BUNDLE_ID S2A_DISPLAY_SUFFIX; do [ -z "${!v:-}" ] || { echo "【红】共享根只出正式包，不许设 ${v}"; exit 1; }; done
  [ "$SIGN" = dev ] || { echo "【红】共享根只出正式包，不许 S2A_SIGN=${SIGN}"; exit 1; }; fi
# S2A_BUNDLE_ID：私有根改包名（一键出包给玩家自己的包名）
[ -z "${S2A_BUNDLE_ID:-}" ] || BID=$S2A_BUNDLE_ID
TM() { env PYTHONCOERCECLOCALE=0 python3 "$R/tools/tm.py" "$@"; }   # 关 Python 的 C 区域强制：否则它给子进程塞 LC_CTYPE=C.UTF-8，下面各线脚本里「$变量紧跟全角字符」在 UTF-8 下被读进变量名、set -u 炸（10-20 自动正式局 nxaot.sh 行 21 / shader_pack.sh 行 64）（函数而不是串：引擎在「Application Support」这类带空格的目录时串会被拆开，10-25 启动器实测 Hades 出包失败）
# 只出程序（S2A_APP_ONLY_FROM=<登记包的产物根>，tools/app_only.sh 调；10-23 维护者「运行时改动合入后 ≤30 分钟出真机读数」）：
#   不刷新阶段、不动现行；把那个产物根克隆到临时目录当工作根，编程序 / 签名 / 打 ipa 后拷到 S2A_APP_ONLY_OUT 就退出（资源包与归档由调用方沿用登记包的）
AO=${S2A_APP_ONLY_FROM:-}
# 直连单轨（10-30 维护者）：只出直连包；直连做不成 refresh.sh 报红退 1、这里不出包（11-07 兼容模式整条拆掉）
export S2A_WIDEN_PKG=1   # refresh.sh 改写段一过就起掌机档宽屏自动出表（graphics/widen/scan/auto_pkg.sh）
if [ -n "$AO" ]; then TL0=$(mktemp); else
# 一键出包刚刷过（10-25：oneclick 外层先 refresh、这里再 refresh，两处工具链不一致时直连库整份出两遍）：调用方给的 S2A_REFRESHED = 它刷完时的现行产物根名，
#   现行还是那一版 → 不再刷（各阶段与本树一致已由那次核过）；现行变了（期间别的进程刷过）照常刷
TL0=$(mktemp)
if [ -n "${S2A_REFRESHED:-}" ] && [ "$S2A_REFRESHED" = "$(basename "$(root_cur "$G")")" ]; then echo "🟢 $G 调用方刚刷过产物根（现行 ${S2A_REFRESHED}），不再刷新"; rfc=0
else TM "刷新各阶段（键核对 / 跳过）" "$TL0" -- bash "$H/refresh.sh" "$G"; rfc=$?; fi
[ $rfc = 0 ] || { echo "【红】$G 现行产物根刷不到本树的阶段键，不出包"; rm -f "$TL0"; exit 1; }   # 产物根落后于代码树（如着色器翻译器换了版本）就先重出对应阶段
# 宽屏表：refresh.sh 改写段一过就在后台起掌机档自动出表（与着色器 / 直连库并行）。出包不等它（10-25 维护者：巫师3 等了 573 秒、动森 713 秒，表还没出成；
#   维护者 要出包快，宽屏表晚一包生效可以接受）：已收尾 → 表在下面拷进本包；还在跑 → 本包用仓库表、没有就这一档位不带表（16:9 居中留黑边，不拉伸），
#   后台照跑，出好的表下一个包按同一构建号带上。确需等它显式设 S2A_WIDEN_WAIT_S=<秒>。主机档照旧后台出、下一个包带上。S2A_WIDEN_AUTO_RUN=0 两档都不出
TM 等宽屏 "$TL0" -- bash "$R/graphics/widen/scan/auto_pkg.sh" wait "$G" handheld "${S2A_WIDEN_WAIT_S:-0}"
S2A_LOOP_MODE=docked bash "$R/graphics/widen/scan/auto_kick.sh" "$G"
fi
CUR=${AO:-$(root_cur "$G")}; [ -n "$CUR" ] && ls "$CUR"/*.patched.bin > /dev/null 2>&1 || { echo "【红】$G 现行产物根没有改写产物（先 pack/stage_rewrite.sh ${G}）"; exit 1; }
# app-only skips refresh, and cached package reuse skips compilation. Check the
# source root before either path; never manufacture a verified marker or alter
# the root. Capability-manifest and runtime widescreen policy are unchanged.
WCAPS="$CUR/s2a_caps.tsv"
[ "$ALLCAPS" = 1 ] && WCAPS="$CUR/s2a_caps.all.tsv"
"${S2A_PYTHON:-${S2A_PY:-python3}}" "$H/widen_root.py" "$CUR" --table "${S2A_WIDEN_DIR:-$R/graphics/widen/tables}/verified.tsv" --caps "$WCAPS" || { rm -f "$TL0"; exit 1; }
SI=${S2A_SAVE_IDENTITY:-$CUR/save-identity.json}; SIKEY=
if [ -n "${S2A_SAVE_IDENTITY:-}" ] || [ -f "$SI" ]; then
  SIKEY=$("${S2A_PY:-python3}" "$H/save_identity.py" --check "$SI") || exit 1
fi
# 包键带 allcaps：全能力包与发布包不互相顶用（11-01 出包线：原先键里没有它，同提交先出全能力包、再出发布包会按「现行已是包」直接沿用全能力那份）
# 桌面显示名（11-04 维护者）：唯一来源 = 产物根 ctl/control.nacp 的游戏自带标题（pack/display_name.py：简中 → 繁中 → 英文 → 其余），「<游戏名> S2Arm[ <后缀>]」；
#   取不到报红、不出包（不退回对照表 / 款名 / 程序号）。后缀：私有根（实验身份）缺省「实验」，S2A_DISPLAY_SUFFIX 显式给的为准（一键出包给空 = 不带后缀；共享根不许设）
DSUF=${S2A_DISPLAY_SUFFIX-$([ -n "$EXP" ] && echo 实验)}
DN=$("${S2A_PY:-python3}" "$H/display_name.py" "$CUR" --suffix "$DSUF") || { echo "【红】$G 取不到桌面显示名（见上一行），不出包"; rm -f "$TL0"; exit 1; }
DT=$("${S2A_PY:-python3}" "$H/display_name.py" "$CUR" --title) || { rm -f "$TL0"; exit 1; }   # 游戏名单独写进 Info.plist（S2AGameTitle）：装机时加后缀（pack/jobs/install.py）同一函数拼
PK=$(ipa_pkg_key "$C$([ "$SIGN" = dev ] || echo " sign=$SIGN")${S2A_BUNDLE_ID:+ bid=$BID} dn=$DN$([ "${ALLCAPS:-0}" = 1 ] && echo " allcaps")$([ "${NATIVE:-0}" = 1 ] && echo " native")" "$CUR" "$G") || exit 1   # 资源输入键算不出 = 失败（ipa_key.sh 已报红），不拿缺项的键去比
[ -z "$SIKEY" ] || PK="$PK save=$SIKEY"
# 登记前核管线归档（10-21 维护者：归档 0 KB 的包判红、不登记，免得调度把首遇全现编的包装上测试设备 / 给 维护者）
# 登记说明带上没解出的追加内容（审计 P2-30：解不出不拦本体，但不许静默算全绿）
reg() { local an; root_pso0_gate "$(dirname "$1")" "$G" || return 1; an=$(root_aoc_note "$(dirname "$1")"); an=${an#；}
  if root_pso0 "$(dirname "$1")" > /dev/null; then S2A_PKG_NOTE="例外：归档 0 KB，${S2A_ALLOW_PSO0}${an:+；$an}" bash "$S2A_PRIVATE_TOOLS/sched.sh" pkg "$G" "$1" "$C"; else S2A_PKG_NOTE=$an bash "$S2A_PRIVATE_TOOLS/sched.sh" pkg "$G" "$1" "$C"; fi; }
[ -z "$AO" ] && root_stage_done "$CUR" pkg "$PK" && { rm -f "$TL0"; echo "🟢 $G 现行已是包 $PK$(root_aoc_note "$CUR")"; [ $REG = 1 ] && { reg "$CUR/pkg" || exit 1; }; exit 0; }
# -H：产物根的 romfs 是指向 RomFS 共享存储的相对软链（pack/roots.sh），临时目录里照抄会悬空；跟随它克隆成真目录（同原先）
if [ -n "$AO" ]; then AOT=$(mktemp -d) || exit 1; trap 'plock_release "$PL_PID"; _root_rm_red "$AOT" "$G 只出程序（临时工作根）"' EXIT; W=$AOT/root; TAG=; mkdir -p "$W" && for e in $(ls -A "$CUR"); do [ "$e" = pkg ] || cp -cRH "$CUR/$e" "$W/" || exit 1; done || { echo "【红】$G 只出程序：克隆产物根 $CUR 失败"; exit 1; }   # 不克隆 pkg/（上一版资源包几万个文件，快道的资源包取自登记快照）
else TAG=$(root_tag "$G" "pkg-$C"); W=$(root_begin "$G" "$TAG") || exit 1; fi
bash "$R/graphics/widen/scan/auto_pkg.sh" copy "$G" "$W" || { echo "【红】$G 宽屏表拷不进出包根"; exit 1; }   # 本次刚出的自动表直接进包（改写产物不依赖它，不重改写）
# 失败时 build.log 随临时版本一起被 root_abort 清掉（10-23 用户体验线：Spare-host 上缺 platform/plat.h，只看到「1 error generated」，复跑 make 才找到原因）：
#   清之前拷进 $S2A_ROOTS/.failed/<款>_ipa_build.log（同改写 / 着色器阶段的做法），报错行带上路径；只出程序的临时工作根不在这里删，由上面的 EXIT 陷阱删（所有出口）
#   同样带走 pkg/archive.log（「打 ipa」这一步的输出，ipa_tail.py 写）：打包失败时哪条超了上限 / 哪个 errno 才有地方看
fail() { local L=$W/pkg/build.log A=$W/pkg/archive.log K=; if [ -s "$L" ]; then mkdir -p "$S2A_ROOTS/.failed" && cp "$L" "$S2A_ROOTS/.failed/${G}_ipa_build.log" && K="；日志 $S2A_ROOTS/.failed/${G}_ipa_build.log"; fi
  if [ -s "$A" ]; then mkdir -p "$S2A_ROOTS/.failed" && cp "$A" "$S2A_ROOTS/.failed/${G}_ipa_archive.log" && K="${K}；日志 $S2A_ROOTS/.failed/${G}_ipa_archive.log"; fi
  echo "【红】$G 出包：$*$K"; [ -n "$AO" ] || root_abort "$G" "$TAG"; exit 1; }
# 编译失败的头一条错误行（clang「error:」/「fatal error」、make「Error N」之前那条），找不到退回最后两行
errline() { LC_ALL=C /usr/bin/grep -a -m1 -E 'error:|fatal error' "$1" 2> /dev/null | cut -c1-200 || true; }
# 上一版的资源包与它的状态表（克隆过来的）留着：pack.py 按源字节数 / mtime 只重克隆、重算变了的文件，verify.py 按上次核对记录只重比变了的。
#   10-21 查实：原先整个 pkg/ 先删，九款每版都把 3.5 万–13.8 万个文件（10–34 GB）全部重克隆、重算 SHA、再逐字节核对，资源包 + 核对 1–2 分钟
t0=$SECONDS; for e in "$W/pkg"/*; do case "${e##*/}" in pack|pack.files|pack.image.key|pack.files.state.tsv|pack.files.verify.tsv) ;; *) _root_rm "$e";; esac; done; _root_rm "$W/build"; mkdir -p "$W/pkg"
TL=$W/pkg/timing.tsv; { printf '步\t墙钟秒\tuser秒\tsys秒\t退出码\n'; cat "$TL0"; } > "$TL"; rm -f "$TL0"   # 出包子步拆账（10-20 维护者：有缓存时出包 476 秒，要分清是哪一步）
PN=5; PD=0   # 出包步数：组装（预编壳）、签名、打 ipa、资源包、资源包核对（本提交第一次出包另加一步出壳）
tmk() { TM "$@"; local r=$?; PD=$((PD + 1)); prog_tick 出包 "$PD" "$PN"; return $r; }   # 计时（tools/tm.py）+ 记一步进度
# 包内能力清单 s2a_caps.tsv（组装时 pack/ship_ext.py 拷进包、宿主 caps.c 读）：改写阶段已在各平台共用的产物根里出好两份（pack/stage_rewrite.sh → pack/gen_caps.sh），这里不再另生成：
#   缺省 = 发布包（默认开、强信号才关）；--all-caps = 测试包（有来源即开），用 s2a_caps.all.tsv 顶替
[ -s "$W/s2a_caps.tsv" ] && [ -s "$W/s2a_caps.all.tsv" ] || fail "产物根没有包内能力清单（s2a_caps.tsv / s2a_caps.all.tsv，改写阶段 pack/stage_rewrite.sh 出；先 pack/refresh.sh）"
#   换之前留一份发布口径，提交产物根前换回：产物根里的 s2a_caps.tsv 是 Mac 局 / 普查 / 安卓与下一次发布包读的发布口径（11-01 出包线：原先换了不换回，
#   本单提交成现行后能力清单阶段按键跳过，发布口径一直是全能力那份）
[ $ALLCAPS = 1 ] && { cp "$W/s2a_caps.tsv" "$W/.s2a_caps.release.tsv" && rm -f "$W/s2a_caps.tsv" && cp "$W/s2a_caps.all.tsv" "$W/s2a_caps.tsv" || fail "换 --all-caps 能力清单失败"; }
[ $NATIVE = 1 ] && { cp "$W/s2a_caps.tsv" "$W/.s2a_caps.release.tsv" && awk -F'\t' 'BEGIN { OFS = "\t" } $1 == "widen" || $1 == "fps60" { print $1, "0", "# 原生档（--native）：16:9、游戏原帧率"; next } { print }' "$W/.s2a_caps.release.tsv" > "$W/s2a_caps.tsv" || fail "换 --native 能力清单失败"; }
# 全外置 + 预编壳（11-04 维护者 定为唯一路径，阶段 0）：主程序 = 与游戏无关的壳（宿主 + C# + 渲染器 + 编解码库，按提交编一次），本款只组装：
#   游戏镜像全部写进 Frameworks/S2AImages.dylib（纯 Python），随行表 / main.npdm / 随包表拷进壳的副本（pack/ship_ext.py build）。
#   壳：S2A_SHELL=<壳目录> 显式给（发版下载的、内部按提交出的）；没给就按本树的壳编号取缓存 ${S2A_SHELL_CACHE:-<运行区>/s2arm/cache/shell}/<壳编号>/，没有就从发布源 S2A_SHELL_SOURCE 取（pack/shell/cache.py），再没有才现出一个（pack/shell_build.sh，只 Mac）
[ "${S2A_CS:-1}" = 1 ] || fail "S2A_CS=0 已停用：iOS 壳总带 C#（11-04 全外置）"
SHELL_DIR=${S2A_SHELL:-}
if [ -z "$SHELL_DIR" ]; then
  SID=$(cd "$R" && "${S2A_PY:-python3}" -c 'from pack.shell.manifest import current_id; print(current_id())') || fail "算不出本树的壳编号"
  SC=${S2A_SHELL_CACHE:-${S2A_RUN_ROOT:-$HOME/.s2arm/run}/s2arm/cache/shell}; SHELL_DIR=$SC/$SID
  # 壳编号不含编译器（跨平台要算得出），本机出的壳另在旁边记出壳那次的工具链版本行 .<壳编号>.toolchain（审计 #1534 P1-107）：没给发布源时壳只由本机出，
  #   记录与本机工具链不同（含旧缓存没有记录）就当没命中、重出一次；给了发布源的壳由发布方出，不按本机编译器判
  STALE=; [ -z "${S2A_SHELL_SOURCE:-}" ] && [ -f "$SHELL_DIR/manifest.json" ] && ! s2a_tc_same "$SC/.$SID.toolchain" && STALE=1
  # 缓存没有：先从发布源取（S2A_SHELL_SOURCE = HTTPS 发布根 / 本机发布目录，pack/shell/cache.py 下载并逐文件核；内部链路由 Mac / gate-host 出壳后 cache.py publish、ssh 同步到 Windows 出包机）；
  #   没给发布源才在本机现出（只有 Mac 能出：要 Xcode）
  if [ ! -f "$SHELL_DIR/manifest.json" ] && [ -n "${S2A_SHELL_SOURCE:-}" ]; then
    (cd "$R" && "${S2A_PY:-python3}" -m pack.shell.cache --shell-id "$SID" --cache-dir "$SC" --base-url "$S2A_SHELL_SOURCE") >> "$W/pkg/build.log" 2>&1 \
      || fail "取壳失败（$(grep -a '【红】' "$W/pkg/build.log" | tail -1)；发布源 ${S2A_SHELL_SOURCE}，壳编号 ${SID}）"
  fi
  if [ ! -f "$SHELL_DIR/manifest.json" ] || [ -n "$STALE" ]; then mkdir -p "$SC" || fail "建不了壳缓存目录 $SC"
    [ "$(uname -s)" = Darwin ] || fail "本机没有壳 ${SID}，也没给发布源 S2A_SHELL_SOURCE（只有 Mac 能现出壳）"
    ST=$SC/.tmp.$SID.$$; tmk "出壳（本提交第一次）" "$TL" -- bash "$H/shell_build.sh" "$ST" >> "$W/pkg/build.log" 2>&1 || { rm -rf "${ST:?}"; fail "出壳失败（$(grep -a '【红】' "$W/pkg/build.log" | tail -1)）"; }
    if [ -n "$STALE" ] && ! mv "$SHELL_DIR" "$SC/.tmp.stale.$SID.$$" 2> /dev/null; then   # 旧编译器出的那份挪开（.tmp.* 由 tools/gc.sh 两小时后删）
      [ ! -e "$SHELL_DIR" ] || fail "旧编译器出的壳 $SHELL_DIR 挪不开（不挪开新壳会被套进它里面）"; fi   # 别的单已先挪走 = 不在了，照常
    if ! mv "$ST" "$SHELL_DIR" 2> /dev/null; then rm -rf "${ST:?}"; fi   # 两单同时出同一提交的壳：先到的发布，后到的丢自己那份（内容相同）
    s2a_tc_fp > "$SC/.$SID.toolchain" || fail "记不下壳的工具链版本（$SC/.$SID.toolchain）"
  fi
fi
touch "$SHELL_DIR" 2> /dev/null   # 目录时间 = 最近使用（tools/gc.sh 按它清壳缓存）
[ -f "$SHELL_DIR/root/S2AShell.tables" ] && [ -f "$SHELL_DIR/manifest.json" ] || fail "壳目录 $SHELL_DIR 不完整（要 root/S2AShell.tables 与 manifest.json，pack/shell_build.sh 出）"
# 出包收尾（组装 → 存档身份 → 图标 → Info.plist → 签名 → 提高内存上限权限核实 → 打 ipa → 进包核对 → 资源包 → 资源包核对）：唯一实现在 pack/ipa_tail.py，
#   Windows 的 Python 编排调同一份（#1100，11-04）。包号 / 着色器目标 / 画面模式 / 资源包要件的判法都在那里
. "$H/build_id.sh"; PKID=$(s2a_build_id "$PK")
S2A_PKG_PROG_DONE=$PD S2A_PKG_PROG_TOTAL=$PN "${S2A_PY:-python3}" "$H/ipa_tail.py" --work "$W" --game "$G" --shell "$SHELL_DIR" --bundle-id "$BID" --suffix "$DSUF" \
  --key "$PK" --title "$DT" --sign "$SIGN" --team "$TEAM" ${SIKEY:+--save-identity "$SI"} ${AO:+--app-only} > "$W/pkg/tail.out" 2>&1; trc=$?
grep -av "^【红】" "$W/pkg/tail.out"; [ $trc = 0 ] || fail "$(grep -a "^【红】" "$W/pkg/tail.out" | tail -1 | sed "s/^【红】//")"
if [ -n "$AO" ]; then mkdir -p "${S2A_APP_ONLY_OUT:?只出程序要 S2A_APP_ONLY_OUT}" && cp "$W/pkg/$G.ipa" "$W/pkg/timing.tsv" "$W/pkg/build.log" "$S2A_APP_ONLY_OUT/" || fail "拷出 ipa 失败"
  echo "🟢 $G 只出程序（$((SECONDS - t0)) 秒）：ipa $(du -h "$S2A_APP_ONLY_OUT/$G.ipa" | cut -f1)，改写产物取自 $(basename "$CUR")，包号 ${PKID:-?}"; exit 0; fi
# 测试资产（不进 ipa / 资源包，只给调度作业用）：按 main 构建号对应的存档与开机时间，~/.s2arm/run/s2arm/saves/<款>_<构建号>/（device/ account/ system_time）
#   挑哪份存档与 Mac 局同口径（graphics/widen/scan/save_pick.sh：同构建号 > 同构建号带后缀 > 同构建号别款名 > 同款别的构建号；10-28 名单线：
#   原先只认 <款>_<构建号>，save_import.py 没给产物根时写成全 0 构建号的社区档 <款>_000…0@community 上真机一律带不上）。没有可用存档 = 空，下面不带
SA=$(bash "$R/graphics/widen/scan/save_pick.sh" "$G" "$(xxd -s 0x40 -l 16 -p "$W/exefs/main")" "$W")
_root_rm "$W/pkg/save"; rm -f "$W/pkg/system_time"
rm -f "$W/pkg/language" "$W/pkg/region" "$W/pkg/timezone"
if [ -d "$SA" ]; then mkdir -p "$W/pkg/save"; for d in device account; do [ -d "$SA/$d" ] && cp -cR "$SA/$d" "$W/pkg/save/"; done; [ -s "$SA/user" ] && cp "$SA/user" "$W/pkg/save/"   # user = 建档用户号（服务层设为当前用户）
  for k in system_time language region timezone; do [ -s "$SA/$k" ] && cp "$SA/$k" "$W/pkg/"; done; fi
[ -d "$SA/padrec" ] && ls "$SA"/padrec/*.tsv > /dev/null 2>&1 && cp -cR "$SA/padrec" "$W/pkg/"   # 录 维护者 手柄（tools/padrec_pull.sh 拉回的 tsv + 存档画面）：正式局 MARK 后按它回放
rm -rf "$W/build"; echo "$PK" > "$W/.stage_pkg"
[ $ALLCAPS = 1 ] || [ $NATIVE = 1 ] && { mv -f "$W/.s2a_caps.release.tsv" "$W/s2a_caps.tsv" || fail "换回发布口径能力清单失败"; }
# 壳编号戳（启动器 pack/jobs/ipa.py 成功时写 <输出>/shell.id）：命令行一键出包也写一份进产物根，调用方转交 <输出>/shell.id。
#   缺了它，启动器判「这款不是本壳出的」，装机 / 组装容器报 container_stale（11-08 玩家用命令行出完包，Mac 前端认不到、推不了）
"$S2A_PY" -c 'import json,sys;print(json.load(open(sys.argv[1]))["details"]["shellId"], end="")' "$SHELL_DIR/manifest.json" > "$W/.shell_id" || fail "记不下壳编号（$SHELL_DIR/manifest.json）"
ROOT_REPLAY="pkg .stage_pkg .shell_id${SIKEY:+ save-identity.json}" root_commit "$G" "$TAG" || exit 1
P=$(root_cur "$G")/pkg
echo "出包拆账（步 墙钟 / user / sys 秒）：$(tail -n +2 "$P/timing.tsv" | awk -F'\t' '{printf "%s %s/%s/%s；", $1, $2, $3, $4}')"
echo "🟢 $G 出包 ${TAG}（$((SECONDS - t0)) 秒）：ipa $(du -h "$P/$G.ipa" | cut -f1)，资源包 $(tail -1 "$P/image.log")$([ -s "$(root_cur "$G")/shaders/red.txt" ] && echo "；【红】着色器部分包：$(cat "$(root_cur "$G")/shaders/red.txt" 2>/dev/null)")$(root_aoc_note "$(root_cur "$G")")"
[ $REG = 1 ] && { reg "$P" || exit 1; }
exit 0
