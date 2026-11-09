import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from unittest.mock import patch

JOBS = Path(__file__).resolve().parents[1]
ROOT = JOBS.parents[1]
sys.path.insert(0, str(JOBS))
from core import Context, Failure, Cancelled, atomic, contained, digest
import build
import library


def oneclick_done(args, ipa, **fields):
    """假一键出包的交付：照 pack/oneclick/oneclick.sh --events 写最后一条 result 事件（ipa.py 从这里取交付物）"""
    ev = Path(str(args[args.index("--events") + 1]))
    with ev.open("a", encoding="utf-8") as f:
        f.write(json.dumps(dict(ev="result", t=0, ok=True, ipa=str(ipa), **fields), ensure_ascii=False) + "\n")


def shared_extract(f):
    """打 iPhone 包的单测：共用解包目录（build.ensure_extract，真解由 Mac 打包单测覆盖）桩成夹具，兄弟根起步（build.seed_from_sibling）关掉"""
    import functools
    @functools.wraps(f)
    def wrapped(self, *args, **kwargs):
        import unittest.mock as um, build
        x = self.root / "shared-extract"; (x / "exefs").mkdir(parents=True, exist_ok=True)
        (x / "exefs/main").write_bytes(b"m"); (x / "unpack.tsv").write_text("title_id\t0100000000000000\n")
        with um.patch.object(build, "ensure_extract", return_value=x) as ex, um.patch.object(build, "seed_from_sibling", return_value=False):
            self.shared_extract = (x, ex)
            return f(self, *args, **kwargs)
    return wrapped


class JobsTests(unittest.TestCase):
    def setUp(self):
        import ipa
        p = patch.object(ipa, "current_shell_id", return_value="0" * 16)   # 单测环境 PATH 没有 git，壳编号另有 test_install 覆盖
        p.start(); self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="s2arm job test ")
        self.root = Path(self.tmp.name)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.request = dict(schema=1, taskId=str(uuid.uuid4()), operation="setup",
                            engineDir=str(ROOT), privateRoot=str(self.root / "private"),
                            library=str(self.root / "library"), libraryId="")   # 不传 developerDir：走缺省（装着的最新 Xcode），不写死 Xcode.app
        self.c = Context(self.request)
        result = library.setup(self.c)
        self.request.update(libraryId=result["libraryId"])
        self.c.r.update(self.request)

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.tmp.cleanup()

    def catalogue(self, games):
        atomic(self.c.library / "catalog.json", dict(schema=1, games=games))

    def test_player_jobs_keep_shader_compilation_local(self):
        # App jobs must not inherit developer remote dispatch, or fall back to
        # dl_gen.py's automatic remote default when the variable is absent.
        with patch.dict(os.environ, {"S2A_DL_REMOTE": "auto", "S2A_PSO_REMOTE": "auto"}):
            env = self.c.env()
        self.assertEqual(env.get("S2A_DL_REMOTE"), "off")
        self.assertEqual(env.get("S2A_PSO_REMOTE"), "off")

    def test_bundled_patch_library_is_for_packing_not_for_play(self):
        # 补丁包部署方案 v2（11-04）：<资源>/Patches/INDEX.tsv 在 → 出包环境 S2A_PATCH_DIR 指它；没有就不设；起局环境去掉（宿主会往补丁根写 .applied，App 包内不能写）
        res = self.root / "res"; (res / "Patches").mkdir(parents=True)
        self.c.r["resources"] = str(res)
        self.assertNotIn("S2A_PATCH_DIR", self.c.env())
        (res / "Patches/INDEX.tsv").write_text("程序号\t已验构建号\tsha256\n")
        self.assertEqual(self.c.env().get("S2A_PATCH_DIR"), str(res / "Patches"))
        self.assertNotIn("S2A_PATCH_DIR", build.runtime_env(self.c, self.game(), self.root / "runtime"))

    def game(self, tid="0100000000000000", packages=None):
        return dict(id=tid, name="Test title", display="1.0", hidden=False, packages=packages or [])

    def package(self, kind="base", version=0, tid="0100000000000000", sha="a" * 64):
        p = self.c.library / "sources" / (sha + ".nsp")
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"unit-test content; not a Switch container")
        return dict(titleId=tid, baseId="0100000000000000", type=kind, version=version, path=str(p.relative_to(self.c.library)),
                    sha256=sha, display="1.0", guestIsa="aarch64")

    def events_of(self, kind):
        return [json.loads(l) for l in self.c.events.read_text().splitlines() if json.loads(l)["type"] == kind]

    def test_shared_extract_reused_until_selection_changes(self):
        # 11-02 多平台共用（维护者）：Mac 打包与打 iPhone 包共用 游戏库/cache/<程序号>/extract；所选包没变 → 复用（打复用行、不解）；加了 DLC → 重解
        import unittest.mock as um, build, toolchain
        base = self.package("base"); game = self.game(packages=[base])
        calls = []
        def run(args, log=None, env=None, cwd=None, on_line=None, timeout=None, ok_codes=(0,)):
            args = [str(a) for a in args]; self.c.last_rc = 0
            if "--npdm-isa" in args: Path(log).write_text("aarch64\n"); return
            calls.append(args); x = Path(args[3])
            for f, b in (("exefs/main.npdm", b"npdm"), ("ctl/control.nacp", b"nacp"), ("romfs/a.bin", b"a")):
                (x / f).parent.mkdir(parents=True, exist_ok=True); (x / f).write_bytes(b)
        logs = self.root / "logs"; logs.mkdir()
        import milestones as ms; ms.plan(self.c, 1, "build_dual")
        with um.patch.object(toolchain, "reader", return_value="reader.dll"), um.patch.object(self.c, "run", run):
            x1 = build.ensure_extract(self.c, game, [base], {}, logs)
            x2 = build.ensure_extract(self.c, game, [base], {}, logs)
            self.assertEqual((x1, x2, len(calls)), (self.c.library / "cache/0100000000000000/extract",) * 2 + (1,))
            self.assertTrue(any(e.get("message") == "已验证并复用解包产物" for e in self.events_of("cache")))
            dlc = dict(self.package("dlc", tid="0100000000001001", sha="d" * 64))
            build.ensure_extract(self.c, self.game(packages=[base, dlc]), [base, dlc], {}, logs)
            self.assertEqual(len(calls), 2, "所选包变了（加 DLC）要重解，不能认旧解包")

    def test_nup_extract_goes_through_ns2_nup(self):
        # ③ Switch 2 NUP（导入记 format=nup）：共用解包走 pack/ns2_nup.py --to-extract（真跑：展开 + ns2_dump 整理），不调读取器解密、不要 prod.keys；
        #   产物与 nx_unpack 解包同形（exefs/main.npdm、ctl/control.nacp、unpack.tsv 程序号、aoc/），中间导出目录不留。只桩程序位数判定（nx_unpack --npdm-isa，要 .NET）
        import unittest.mock as um, toolchain, milestones as ms
        from jobcase import synthetic_nacp, synthetic_npdm, synthetic_nup
        app = 0x0400486024DEC000
        src = self.root / "drive" / "Zelda T [0400486024dec000][v131072].nup"; src.parent.mkdir()
        synthetic_nup(src, app, [(0, app, 0, {"main": b"\x01" * 300, "main.npdm": synthetic_npdm(app)}, {"a.bin": b"base"}, {}),
                                 (1, app | 0x800, 131072, {"main": b"\x02" * 500, "main.npdm": synthetic_npdm(app)}, {"a.bin": b"upd"},
                                  {2: synthetic_nacp("Ocarina T", "1.2.0")}),
                                 (2, app + 0x1001, 0, None, {"d.bin": b"DLC"}, {})])
        common = dict(baseId="0400486024DEC000", path=str(src), sha256="e" * 64, display="1.2.0", guestIsa="unknown", format="nup")
        packages = [dict(common, titleId="0400486024DEC000", type="base", version=0),
                    dict(common, titleId="0400486024DEC800", type="update", version=131072),
                    dict(common, titleId="0400486024DED001", type="dlc", version=0)]
        game = self.game("0400486024DEC000", packages); game["name"] = "Ocarina T"
        calls, real = [], self.c.run
        def run(args, log=None, env=None, cwd=None, on_line=None, timeout=None, ok_codes=(0,)):
            calls.append([str(a) for a in args])
            if "--npdm-isa" in calls[-1]:
                self.c.last_rc = 0; Path(log).write_text("aarch64\n"); return
            return real(args, log, env, cwd, on_line, timeout, ok_codes)
        logs = self.root / "logs"; logs.mkdir()
        ms.plan(self.c, 1, "build_dual")
        self.assertFalse(self.c.keys.exists())
        with um.patch.object(toolchain, "reader", return_value=self.root / "reader.dll"), um.patch.object(self.c, "run", run):
            x = build.ensure_extract(self.c, game, build.selected_packages(game), {}, logs)
        self.assertEqual([c[4:6] for c in calls if "--npdm-isa" not in c], [["--to-extract", str(src)]])
        self.assertTrue(calls[0][3].endswith("pack/ns2_nup.py") and calls[0][6] == str(x), calls[0])
        self.assertFalse(any(str(self.c.keys) in c for c in calls), "NUP 不调读取器解密")
        self.assertEqual((x / "exefs/main.npdm").read_bytes(), synthetic_npdm(app))
        self.assertEqual((x / "exefs/main").read_bytes(), b"\x02" * 500)
        self.assertEqual((x / "ctl/control.nacp").read_bytes(), synthetic_nacp("Ocarina T", "1.2.0"))
        self.assertIn("title_id\t0400486024dec000", (x / "unpack.tsv").read_text())
        self.assertEqual((x / "aoc/1/d.bin").read_bytes(), b"DLC")
        self.assertEqual(sorted(p.name for p in x.parent.iterdir()), ["extract", "extract-checkpoint.json"])

    def test_two_nups_use_the_newest_update_container(self):
        # 同一款放过两个 NUP（旧的带 DLC、新的带更新）：展开选中最新更新所在的那个；旧 NUP 里的 DLC 不带、发黄字 aoc_missing，不静默
        import unittest.mock as um, toolchain, milestones as ms
        old, new = self.root / "old.nup", self.root / "new.nup"
        common = dict(baseId="0400486024DEC000", display="", guestIsa="unknown", format="nup")
        packages = [dict(common, titleId="0400486024DEC000", type="base", version=0, path=str(old), sha256="1" * 64),
                    dict(common, titleId="0400486024DED001", type="dlc", version=0, path=str(old), sha256="1" * 64),
                    dict(common, titleId="0400486024DEC000", type="base", version=0, path=str(new), sha256="2" * 64),
                    dict(common, titleId="0400486024DEC800", type="update", version=131072, path=str(new), sha256="2" * 64)]
        game = self.game("0400486024DEC000", packages)
        calls = []
        def run(args, log=None, env=None, cwd=None, on_line=None, timeout=None, ok_codes=(0,)):
            args = [str(a) for a in args]; self.c.last_rc = 0; calls.append(args)
            if "--npdm-isa" in args: Path(log).write_text("aarch64\n"); return
            x = Path(args[6])
            for f in ("exefs/main.npdm", "ctl/control.nacp"):
                (x / f).parent.mkdir(parents=True, exist_ok=True); (x / f).write_bytes(b"x")
        logs = self.root / "logs"; logs.mkdir()
        ms.plan(self.c, 1, "build_dual")
        with um.patch.object(toolchain, "reader", return_value=self.root / "reader.dll"), um.patch.object(self.c, "run", run):
            build.ensure_extract(self.c, game, build.selected_packages(game), {}, logs)
        self.assertEqual(calls[0][4:6], ["--to-extract", str(new)])
        d = [y for y in self.c.job["diagnostics"] if y["code"] == "aoc_missing"]
        self.assertEqual((len(d), d[0]["severity"], "0400486024DED001" in d[0]["summary"]), (1, "warning", True))

    def test_license_only_dlc_warns_not_fails(self):
        # 审计 P1-89：所选 DLC 一行文件数 -1（只有授权 / 数据解不开）→ 不整单失败、解包照常记成完整准备，发一条黄字 aoc_missing 带那份 DLC 的标题号；
        #   全部 DLC 有数据 → 没有 aoc_missing
        import unittest.mock as um, build, toolchain
        base = self.package("base"); dlc = dict(self.package("dlc", tid="0100000000001001", sha="d" * 64)); game = self.game(packages=[base, dlc])
        rows = {"v": "1\t0100000000001001\t0\t-1\t0\n"}
        def run(args, log=None, env=None, cwd=None, on_line=None, timeout=None, ok_codes=(0,)):
            args = [str(a) for a in args]; self.c.last_rc = 0
            if "--npdm-isa" in args: Path(log).write_text("aarch64\n"); return
            x = Path(args[3])
            for f, b in (("exefs/main.npdm", b"npdm"), ("ctl/control.nacp", b"nacp"), ("aoc.tsv", ("idx\ttid\tver\tfiles\tbytes\n" + rows["v"]).encode())):
                (x / f).parent.mkdir(parents=True, exist_ok=True); (x / f).write_bytes(b)
        logs = self.root / "logs"; logs.mkdir()
        import milestones as ms; ms.plan(self.c, 1, "build_dual")
        with um.patch.object(toolchain, "reader", return_value="reader.dll"), um.patch.object(self.c, "run", run):
            x = build.ensure_extract(self.c, game, [base, dlc], {}, logs)
            self.assertTrue((x.parent / "extract-checkpoint.json").is_file(), "仅授权行不该挡住本体的完整准备")
            d = [y for y in self.c.job["diagnostics"] if y["code"] == "aoc_missing"]
            self.assertEqual((len(d), d[0]["severity"] if d else None, "0100000000001001" in (d[0]["summary"] if d else "")), (1, "warning", True))
            shutil.rmtree(x); rows["v"] = "1\t0100000000001001\t0\t3\t30\n"; self.c.job["diagnostics"].clear()
            build.ensure_extract(self.c, game, [base, dlc], {}, logs)
            self.assertEqual([y for y in self.c.job["diagnostics"] if y["code"] == "aoc_missing"], [])

    def test_seed_from_sibling_clones_current_root_and_names_commit(self):
        # 11-02：本平台还没现行根、别的平台有 → 克隆它的现行根起步（不从解包目录重新播种），复用行带被复用根的提交号；没有兄弟根 → False；
        #   克隆不过来（如 exFAT：clonedir 失败关闭）→ 黄字 sibling_clone_failed、False（调用方从解包目录播种）
        import unittest.mock as um, build
        game = self.game()
        sib = self.c.library / "iPhone/0100000000000000/roots"; v = sib / "party@pkg-abc"
        (v / "romfs").mkdir(parents=True); (v / ".stage_rewrite").write_text("K-rewrite\n"); (v / ".root_src").write_text("abcdef1234567890\n")
        (sib / "party@现行").symlink_to("party@pkg-abc")
        mac = self.c.library / "cache/0100000000000000/key1/roots"
        self.assertFalse(build.seed_from_sibling(self.c, self.game(tid="0100000000000001"), mac, "0100000000000001"), "别的款的根不算兄弟")
        self.assertTrue(build.seed_from_sibling(self.c, game, mac, "0100000000000000"))
        cur = (mac / "0100000000000000@现行").resolve()
        self.assertEqual((cur.name, (cur / ".stage_rewrite").read_text()), ("0100000000000000@sib-pkg-abc", "K-rewrite\n"))
        ev = [e for e in self.events_of("cache") if "复用兄弟平台" in e.get("message", "")]
        self.assertTrue(ev and "提交 abcdef123" in ev[-1]["message"] and "克隆路线：APFS 克隆" in ev[-1]["message"], ev)
        with um.patch.object(self.c, "run", side_effect=Failure("克隆失败：x：[Errno 45] Operation not supported")):
            self.assertFalse(build.seed_from_sibling(self.c, game, self.c.library / "cache/0100000000000000/key2/roots", "0100000000000000"))
        d = [x for x in self.c.job["diagnostics"] if x["code"] == "sibling_clone_failed"]
        self.assertTrue(d and d[0]["severity"] == "warning", d)

    def test_developer_dir_default_is_newest_installed_not_hardcoded(self):
        # 10-25 维护者：请求没带 developerDir 时不写死 /Applications/Xcode.app；11-07 Duo 定因：缺省取装着的最新 Xcode（toolchain.detect_xcode，
        #   同启动器与 tools/toolchain.sh），不再取 xcode-select（本机指 26.5 → 旧 SDK 壳在 iOS 27 新机型上缩小显示）；找不到就不设（由工具链检查说清没装）
        import core, toolchain
        newest = self.root / "apps/Xcode-99.app/Contents/Developer"; newest.mkdir(parents=True)
        with patch.object(toolchain, "detect_xcode", return_value=str(newest)):
            env = Context(dict(self.request)).env()
        self.assertEqual(env.get("DEVELOPER_DIR"), str(newest))
        self.assertEqual(env.get("S2A_DEVELOPER_DIR"), env.get("DEVELOPER_DIR"))
        chosen = str(self.root / "picked/Xcode.app/Contents/Developer")
        self.assertEqual(Context(dict(self.request, developerDir=chosen)).env()["DEVELOPER_DIR"], chosen)   # 启动器传的优先
        with patch.object(core, "default_developer_dir", return_value=None), patch.dict(os.environ, {"DEVELOPER_DIR": "/stale/Xcode.app/Contents/Developer"}):
            env = Context(dict(self.request)).env()
        self.assertNotIn("DEVELOPER_DIR", env); self.assertNotIn("S2A_DEVELOPER_DIR", env)   # 外层环境里的旧值也不漏进来

    def test_preflight_offers_one_click_fix_for_missing_xcode(self):
        # 没有 Xcode：Xcode 项不过、给「去 App Store」；Metal 项不过、不给按钮（没 Xcode 下载不了组件）；不再有「共享字体」检测项
        import toolchain
        self.c.r.update(developerDir=str(self.root / "NoXcode.app/Contents/Developer"))
        checks = {k["name"]: k for k in toolchain.preflight(self.c)["checks"]}
        self.assertFalse(checks["Xcode"]["ok"]); self.assertEqual(checks["Xcode"].get("action"), "appStoreXcode")
        self.assertFalse(checks["Metal 编译组件"]["ok"]); self.assertIsNone(checks["Metal 编译组件"].get("action"))
        self.assertNotIn("共享字体", checks)

    def test_preflight_timeout_probe_runs_a_bounded_command(self):
        # #1133：timeout 按「限时跑一次命令退出 0」判可用：认不认 --version 不算数；缺 / 跑不起来 / 命令失败照旧判缺
        import toolchain, unittest.mock as um
        def fake(rc_of):
            def probe(c, args, timeout=15):
                return rc_of(list(map(str, args))) == 0, ""
            return probe
        def bundled(a):   # 内附替身的行为：不认任何选项（返回 125），限时跑命令原样返回命令的码
            if a[0] != "timeout": return 0
            return 125 if a[1].startswith("-") else 0 if a[2:] == ["/usr/bin/true"] else 1
        for rc_of, want in ((bundled, True), (lambda a: 127 if a[0] == "timeout" else 0, False), (lambda a: 1 if a[0] == "timeout" else 0, False)):
            with um.patch.object(toolchain, "probe", fake(rc_of)):
                checks = {k["name"]: k for k in toolchain.preflight(self.c)["checks"]}
            self.assertEqual(checks["timeout"]["ok"], want)

    def test_inspect_turns_decrypt_failure_into_keys_advice(self):
        # 读取器报「could not be decrypted… Check keys」退出 → 提示换新密钥；别的失败原样往上抛
        import unittest.mock as um
        keys = self.root / "prod.keys"; keys.write_text("header_key = " + "0" * 64 + "\n")
        def fake(msg):
            def run(args, log=None, env=None, cwd=None, on_line=None):
                on_line(msg); raise Failure("S2Arm 工具退出，当前阶段未完成。")
            return run
        with um.patch.object(library.toolchain, "reader", return_value="nx_unpack.dll"):
            with um.patch.object(self.c, "run", fake("Container metadata could not be decrypted or verified. Check keys, tickets and file integrity.")):
                with self.assertRaises(Failure) as e: library.inspect(self.c, self.root / "a.xci", self.root, keys)
                self.assertIs(str(e.exception), library.KEYS_CANNOT_DECRYPT)
            with um.patch.object(self.c, "run", fake("Unhandled exception: disk full")):
                with self.assertRaises(Failure) as e: library.inspect(self.c, self.root / "a.xci", self.root, keys)
                self.assertEqual(str(e.exception), "S2Arm 工具退出，当前阶段未完成。")

    def test_connected_counts_wired_paired_with_idle_tunnel(self):
        # 10-26 测试设备2：数据线连着、已配对，隧道空闲 disconnected → 算连着；无线且隧道没通、没配对的不算
        import unittest.mock as um, diag, target
        devices = {"result": {"devices": [
            {"identifier": "W", "hardwareProperties": {"reality": "physical", "platform": "iOS", "productType": "iPhone18,2"}, "deviceProperties": {"name": "测试设备2", "osVersionNumber": "27.1"},
             "connectionProperties": {"tunnelState": "disconnected", "transportType": "wired", "pairingState": "paired"}},
            {"identifier": "N", "hardwareProperties": {"reality": "physical", "platform": "iOS"}, "connectionProperties": {"tunnelState": "unavailable", "transportType": None, "pairingState": "paired"}},
            {"identifier": "U", "hardwareProperties": {"reality": "physical", "platform": "iOS"}, "connectionProperties": {"tunnelState": "disconnected", "transportType": "wired", "pairingState": "unpaired"}}]}}
        with um.patch.object(diag, "devicectl", lambda c, *a, timeout=120: (0, devices, "")), um.patch.object(target, "gpu_arch", lambda *a: "g18"):
            self.assertEqual([d["id"] for d in target.connected(self.c)], ["W"])

    def test_emit_survives_frontend_gone(self):
        # 10-26：启动器（读作业标准输出的一端）没了，作业不能因写断掉的管道而死；事件照写记录，之后起的子进程标准输出指向 /dev/null
        code = (f"import os, sys; sys.path.insert(0, {str(ROOT / 'pack/jobs')!r}); from core import Context\n"
                f"r, w = os.pipe(); os.dup2(w, 1); os.close(w); os.close(r)\n"   # 读端已关 = 启动器没了
                f"c = Context({dict(self.request, taskId='gone')!r})\n"
                "c.begin('甲', 1, 2); c.emit(done=1, total=2); c.begin('乙', 2, 2)\n"
                "import subprocess; rc = subprocess.run(['/bin/sh', '-c', 'yes | head -c 200000']).returncode\n"
                "open(os.environ['OUT'], 'w').write(f'{c.frontend_gone} {c.job[\"stage\"]} {c.seq} {rc}')\n")
        out = self.root / "gone.out"
        p = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, OUT=str(out)), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(out.read_text(), "True 乙 3 0")   # 发现断管、没抛；三条事件都进了记录；子进程照常跑完
        events = (self.root / "private/jobs/gone.events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["stage"] for l in events], ["甲", "甲", "乙"])

    def test_pull_diagnostics_only_s2arm_apps_on_connected_ios(self):
        # Same-model devices and same-second batches retain independent snapshots.
        import unittest.mock as um, diag
        devices = {"result": {"devices": [
            {"identifier": "D1", "hardwareProperties": {"platform": "iOS", "udid": "U1", "marketingName": "iPhone 17 Pro Max"}, "connectionProperties": {"tunnelState": "connected"}},
            {"identifier": "D3", "hardwareProperties": {"platform": "iOS", "udid": "U3", "marketingName": "iPhone 17 Pro Max"}, "connectionProperties": {"tunnelState": "connected"}},
            {"identifier": "D2", "hardwareProperties": {"platform": "iOS", "udid": "U2"}, "connectionProperties": {"tunnelState": "disconnected"}},
            {"identifier": "M1", "hardwareProperties": {"platform": "macOS"}, "connectionProperties": {"tunnelState": "connected"}}]}}
        apps = {"result": {"apps": [{"bundleIdentifier": "io.s2arm.d2r.ABCDE12345", "name": "D2R S2A"}, {"bundleIdentifier": "com.other.app", "name": "Other"}]}}
        calls, batch = [], 1
        def fake(c, *args, timeout=120):
            calls.append(args)
            if args[:2] == ("list", "devices"): return 0, devices, ""
            if args[:3] == ("device", "info", "apps"): return 0, apps, ""
            source = args[args.index("--source") + 1]
            device = args[args.index("--device") + 1]
            target = Path(args[args.index("--destination") + 1])
            if target.name in ("diag", "metrickit"):
                target.mkdir(); (target / "partial.json").write_text("failed partial")
            else:
                target.write_text(f"batch {batch} {device} {source}")
            return (0 if batch < 3 and (source.endswith("host.log") or batch == 1 and source.endswith("frames.tsv")) else 1), {}, ""
        with um.patch.object(diag, "devicectl", fake), um.patch.object(diag.time, "strftime", return_value="same-second"):
            first = diag.pull(self.c)
            self.assertEqual([a["bundle"] for a in first["apps"]], ["io.s2arm.d2r.ABCDE12345"] * 2)
            self.assertEqual(len({a["path"] for a in first["apps"]}), 2)
            self.assertEqual({(Path(a["path"]) / "host.log").read_text() for a in first["apps"]}, {"batch 1 D1 Documents/host.log", "batch 1 D3 Documents/host.log"})
            copies = [a for a in calls if a[:3] == ("device", "copy", "from")]
            self.assertEqual({a[a.index("--device") + 1] for a in copies}, {"D1", "D3"})
            self.assertEqual(sorted(a[a.index("--source") + 1] for a in copies), sorted(diag.ITEMS * 2))
            self.assertFalse(any("save" in a[a.index("--source") + 1] for a in copies))
            snapshot = {p: p.read_bytes() for p in Path(first["outputPath"]).rglob("*") if p.is_file()}
            batch = 2
            second = diag.pull(self.c)
            self.assertNotEqual(first["outputPath"], second["outputPath"])
            for app in second["apps"]:
                self.assertEqual(app["files"], 1)
                self.assertEqual([p.name for p in Path(app["path"]).iterdir()], ["host.log"])
                self.assertNotIn("U1", app["path"]); self.assertNotIn("U3", app["path"])
            self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
            calls.clear(); self.c.r.update(device="U1")
            selected = diag.pull(self.c)
            self.assertEqual(len(selected["apps"]), 1)
            self.assertTrue(all(a[a.index("--device") + 1] == "D1" for a in calls if "--device" in a))
            self.c.r.update(device="U2")
            with self.assertRaises(Failure): diag.pull(self.c)
            self.c.r.update(device="U1"); batch = 3
            before = set((self.c.private / "diagnostics").iterdir())
            with self.assertRaises(Failure): diag.pull(self.c)
            failed = set((self.c.private / "diagnostics").iterdir()) - before
            self.assertEqual(len(failed), 1)
            self.assertFalse(any(p.is_file() for root in failed for p in root.rglob("*")))
            self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)

    def test_pull_diagnostics_hints_resign_when_probe_says_iml_lost(self):
        # 拉回的 diag/iml_probe.json（App 首启复核写）has_iml_ent=false → 一行黄字提示重签；true / null / 没文件 / 坏文件 → 不提示、不报错
        import unittest.mock as um, diag
        devices = {"result": {"devices": [{"identifier": "D1", "hardwareProperties": {"platform": "iOS", "udid": "U1"}, "connectionProperties": {"tunnelState": "connected"}}]}}
        apps = {"result": {"apps": [{"bundleIdentifier": "io.s2arm.acnh", "name": "ACNH S2A"}]}}
        probe = {"v": None}
        def fake(c, *args, timeout=120):
            if args[:2] == ("list", "devices"): return 0, devices, ""
            if args[:3] == ("device", "info", "apps"): return 0, apps, ""
            target = Path(args[args.index("--destination") + 1])
            if target.name != "diag": return 1, {}, ""
            target.mkdir()
            if probe["v"] is not None: (target / "iml_probe.json").write_text(probe["v"])
            return 0, {}, ""
        with um.patch.object(diag, "devicectl", fake):
            for v, hint in (('{"has_iml_ent": false, "window_gb": 5.4}', True), ('{"has_iml_ent": true}', False), ('{"has_iml_ent": null}', False), (None, False), ("{坏", False)):
                self.c.job["diagnostics"].clear(); probe["v"] = v
                diag.pull(self.c)
                got = [x for x in self.c.job["diagnostics"] if x["code"] == "iml_missing"]
                self.assertEqual(len(got), 1 if hint else 0, v)
                if hint:
                    self.assertEqual(got[0]["severity"], "warning")
                    self.assertIn("ACNH S2A：这台机上装的包签名丢了提高内存上限权限", got[0]["summary"])
                    self.assertIn("重签", got[0]["summary"])

    @shared_extract
    def test_ipa_calls_oneclick_with_player_keys_fonts_and_ios(self):
        # 打 iPhone 包：没 SDK → 提示一键安装；有 SDK → 调引擎里的 oneclick.sh，带玩家密钥、应用字体、iOS 平台、出到 <游戏库>/iPhone/<游戏号>
        import unittest.mock as um, ipa
        base = self.package("base")
        self.catalogue([self.game(packages=[base])])
        res = self.root / "res"; (res / "Fonts").mkdir(parents=True); (res / "Fonts" / "a.bfttf").write_bytes(b"x")
        self.c.r.update(resources=str(res), gameId="0100000000000000")
        import target as tg
        um.patch.object(tg, "os_choices", return_value=[27, 26]).start(); self.addCleanup(um.patch.stopall)
        # 没给目标、也没连设备：一包通用（10-30）→ 不拦，接着走到装 SDK 那一步
        with um.patch.object(tg, "connected", return_value=[]):
            with um.patch.object(ipa, "dotnet_sdk", return_value=(None, None)), self.assertRaises(Failure) as e: ipa.build(self.c)
            self.assertIn("一键安装", str(e.exception))
        self.c.r["target"] = dict(model="iPhone 16 Pro", productType="iPhone17,1", gpuArch="applegpu_g17p", os=26)
        with um.patch.object(ipa, "dotnet_sdk", return_value=(None, None)):
            with self.assertRaises(Failure) as e: ipa.build(self.c)
            self.assertIn("一键安装", str(e.exception))
        seen = {}
        def run(args, log=None, env=None, cwd=None, on_line=None):
            seen.update(args=[str(a) for a in args], env=env)
            out = Path(args[args.index("--out") + 1]); (out / "x.ipa").write_bytes(b"ipa")
            oneclick_done(args, out / "x.ipa", build_id="abc-123", graphics_mode="direct")
        home = self.root / "dn"; (home / "sdk/10.0.403").mkdir(parents=True); (home / "dotnet").write_text("")
        def build():
            with um.patch.object(ipa, "dotnet_sdk", return_value=(home, home / "dotnet")), um.patch.object(self.c, "run", run), \
                 um.patch.object(type(self.c), "engine", new_callable=um.PropertyMock, return_value=ROOT) if isinstance(getattr(type(self.c), "engine", None), property) else um.patch.object(self.c, "engine", ROOT):
                return ipa.build(self.c)
        r = build()
        a = seen["args"]
        self.assertTrue(a[1].endswith("pack/oneclick/oneclick.sh"))
        # 11-02 多平台共用：从共用解包目录往下（--from-extract），不再暂存原件、一键出包不再自己解（解包、按程序号挑款由 build.ensure_extract 做：--title-id 同 #456）
        self.assertEqual(a[2:4], ["--from-extract", str(self.shared_extract[0])])
        self.assertNotIn("--keys", a)
        self.assertEqual(self.shared_extract[1].call_args.args[1]["id"], "0100000000000000")
        self.assertEqual(a[a.index("--fonts") + 1], str((res / "Fonts").resolve()))
        self.assertEqual(Path(a[a.index("--out") + 1]), self.c.library / "iPhone" / "0100000000000000")
        self.assertEqual((seen["env"]["S2A_PACK_PLAT"], seen["env"]["DOTNET_ROOT"]), ("ios", str(home)))
        # 直连包与机型无关（一包通用 10-30）：不把目标架构 / 系统大版本交给出包
        self.assertNotIn("S2A_PSO_ARCHS", seen["env"]); self.assertNotIn("S2A_PSO_OS", seen["env"])
        self.assertTrue(r["ipa"].endswith("x.ipa")); self.assertEqual(r["buildId"], "abc-123")

    @shared_extract
    def test_ipa_result_is_the_delivered_file_not_the_newest_in_output(self):
        # #67 qidian99 反证：输出目录里有修改时间更「新」（2100 年）的旧 IPA，本轮交付的是 current-title.ipa → 必须返回本轮的，不按 mtime 挑
        import unittest.mock as um, ipa
        base = self.package("base"); self.catalogue([self.game(packages=[base])])
        self.c.r.update(gameId="0100000000000000", target={"gpuArch": "applegpu_g18p", "os": 27})
        res = self.root / "res"; (res / "Fonts").mkdir(parents=True); (res / "Fonts/a.bfttf").write_bytes(b"f")
        out = self.c.library / "iPhone" / "0100000000000000"; out.mkdir(parents=True)
        old = out / "old-title.ipa"; old.write_bytes(b"old"); os.utime(old, (4102444800, 4102444800))
        # Q15: real synthetic cache payload, mocked external packaging only.
        cache = self.c.work_root / 'cache/air/ios/aa' / ('a' * 64 + '.air')
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'synthetic reusable AIR')
        direct = self.c.work_root / 'run/s2arm/cache/direct/0100000000000000'
        direct_lib = direct / 'store/aa.pack'   # S2Arm 10-31: the direct cache is the store (shard files); the old ios/ / mac/ trees are cleared as transients
        direct_work = direct / 'work/log'
        for path in (direct_lib, direct_work):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic direct')
        import cache_retention
        lines = {}
        def run(args, log=None, env=None, cwd=None, on_line=None):
            o = Path(args[args.index("--out") + 1]); (o / "current-title.ipa").write_bytes(b"new")
            if lines.get("done", True):
                oneclick_done(args, o / "current-title.ipa", build_id="current-123", graphics_mode="direct")
        home = self.root / "dn"; (home / "sdk/10.0.403").mkdir(parents=True); (home / "dotnet").write_text("")
        with um.patch.object(ipa, "dotnet_sdk", return_value=(home, home / "dotnet")), um.patch.object(self.c, "run", run), \
             um.patch.object(ipa, "resolve_target", return_value={"gpuArch": "applegpu_g18p", "os": 27}), \
             um.patch.object(self.c, "fonts_dir", return_value=res / "Fonts"), \
             um.patch.object(library, "check_pack_space", return_value={"compilerMinFreeBytes": 48 << 30}) as preflight, \
             um.patch.object(cache_retention, "trim", wraps=cache_retention.trim) as trim, \
             um.patch.object(cache_retention.shutil, "disk_usage", return_value=um.Mock(free=100 * 1024**3)) as disk:
            r = ipa.build(self.c)
            self.assertEqual((Path(r["ipa"]).name, r["buildId"]), ("current-title.ipa", "current-123"))
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(cache.read_bytes(), b'synthetic reusable AIR')
            self.assertEqual(direct_lib.read_bytes(), b'synthetic direct')
            self.assertFalse(direct_work.exists())
            self.assertEqual(r['compilerCache']['removedEntries'], 0)
            self.assertEqual(trim.call_count, 1)
            self.assertEqual(trim.call_args.kwargs['min_free_bytes'], 48 << 30)
            self.assertIn('compiler_policy', preflight.call_args.kwargs)
            # Optional eviction refusal must warn, not hide the validated deliverable.
            os.utime(cache, (1, 1))
            real_unlink = Path.unlink
            def refuse_cache_unlink(path, *args, **kwargs):
                if path == cache:
                    raise PermissionError(13, 'synthetic read-only cache', str(path))
                return real_unlink(path, *args, **kwargs)
            disk.return_value.free = 0  # controlled disk-pressure oracle, no real disk filled
            with um.patch.dict(os.environ, {'S2A_COMPILER_CACHE_MIN_FREE_BYTES': str(10**15)}), \
                 um.patch.object(Path, 'unlink', refuse_cache_unlink):
                warned = ipa.build(self.c)
            self.assertEqual(Path(warned['ipa']).name, 'current-title.ipa')
            self.assertEqual(warned['compilerCache']['evictionErrors'], 1)
            warnings = [d for d in self.c.job['diagnostics'] if d['code'] == 'compiler_cache_incomplete']
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]['severity'], 'warning')
            self.assertEqual(cache.read_bytes(), b'synthetic reusable AIR')
            # 事件流里没有 result（一键出包中途退出等）→ 报错，不退回按时间猜
            lines["done"] = False
            with self.assertRaises(Failure):
                ipa.build(self.c)
            self.assertEqual(trim.call_count, 2, 'failed packaging must not trim caches')
            self.assertEqual(cache.read_bytes(), b'synthetic reusable AIR')

    def test_run_reaps_leftover_session_processes(self):
        # 10-26 斯普拉遁3 Spare-host：工具退出后，留在它会话里的孙进程（dl_gen 超时只杀 metal-tt，air-tt 过继给 launchd 继续编）要收掉；
        #   有意活得比任务久、自立会话的（宽屏自动出表，perl setsid）不收
        import shlex
        d = self.root / "reap"; d.mkdir(); q = shlex.quote(str(d))
        #   两个后台都等到进程名变成 sleep 再退出（exec 完成；自立会话那个 setsid 在 exec 之前）：机器忙时 fork 后还没 exec，收残留扫到的是「bash」，日志对不上（10-26 gate-host 高负载偶发红）
        script = (f'sleep 60 & echo $! > {q}/orphan; '
                  f"(exec perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' -- sleep 60) > /dev/null 2>&1 < /dev/null & echo $! > {q}/detached; "
                  f'for p in $(cat {q}/orphan {q}/detached); do for i in $(seq 1 600); do [ "$(ps -o comm= -p $p)" = sleep ] && break; sleep 0.05; done; done; exit 0')
        self.c.run(["bash", "-c", script], log=d / "run.log")
        orphan, detached = (int((d / n).read_text()) for n in ("orphan", "detached"))
        def alive(pid):
            try: os.kill(pid, 0); return True
            except OSError: return False
        try:
            self.assertFalse(alive(orphan), "留在任务会话里的孙进程应被收掉")
            self.assertTrue(alive(detached), "自立会话的后台不该被收")
            self.assertIn("收掉本阶段工具会话里的残留进程 1 个：sleep", (d / "run.log").read_text())
        finally:
            for pid in (orphan, detached):
                if alive(pid): os.kill(pid, 9)

    @shared_extract
    def test_ipa_aoc_missing_becomes_warning(self):
        # 审计 P1-74：追加内容没解出，本体照常出（不挡），但结果事件的 aoc_missing 要成一条黄字 aoc_missing，任务记「完成（有警告）」、完成页不画完成章
        import unittest.mock as um, ipa
        base = self.package("base"); self.catalogue([self.game(packages=[base])])
        self.c.r.update(gameId="0100000000000000", target={"gpuArch": "applegpu_g18p", "os": 27})
        res = self.root / "res"; (res / "Fonts").mkdir(parents=True); (res / "Fonts/a.bfttf").write_bytes(b"f")
        out = self.c.library / "iPhone" / "0100000000000000"; out.mkdir(parents=True)
        miss = {"v": ["DLC 套装 A.nsp", "dlc_b.nsp"]}
        def run(args, log=None, env=None, cwd=None, on_line=None):
            o = Path(args[args.index("--out") + 1]); (o / "g.ipa").write_bytes(b"new")
            oneclick_done(args, o / "g.ipa", graphics_mode="direct", aoc_missing=miss["v"])
        home = self.root / "dn"; (home / "sdk/10.0.403").mkdir(parents=True); (home / "dotnet").write_text("")
        with um.patch.object(ipa, "dotnet_sdk", return_value=(home, home / "dotnet")), um.patch.object(self.c, "run", run), \
             um.patch.object(ipa, "resolve_target", return_value={"gpuArch": "applegpu_g18p", "os": 27}), \
             um.patch.object(self.c, "fonts_dir", return_value=res / "Fonts"):
            ipa.build(self.c)
            d = [x for x in self.c.job["diagnostics"] if x["code"] == "aoc_missing"]
            self.assertEqual([(x["severity"], x["summary"]) for x in d], [("warning", "追加内容 2 份没解出，包里不含（DLC 套装 A.nsp、dlc_b.nsp）")])
            # 都解出了（空名单）→ 不加
            miss["v"] = []; self.c.job["diagnostics"].clear()
            ipa.build(self.c)
            self.assertEqual([x for x in self.c.job["diagnostics"] if x["code"] == "aoc_missing"], [])

    def test_dotnet_sdk_user_directory(self):
        import unittest.mock as um, ipa, shlex
        home = self.root / "user home"
        old = home / ".dotnet/sdk/10.0.100"; old.mkdir(parents=True)
        host = home / ".dotnet/dotnet"
        def lists(*versions):
            host.write_text('#!/bin/sh\n[ "$1" = --list-sdks ] || exit 2\n' + "".join(f'printf "%s\\n" {shlex.quote(f"{v} [{old.parent}]")}\n' for v in versions))
            host.chmod(0o755)
        env = {"DOTNET": "", "DOTNET_ROOT": "", "DOTNET_ROOT_ARM64": "", "DOTNET_ROOT_X64": "", "PATH": ""}
        # 候选只留本测试临时目录里的：实现还会探测 /usr/local/share/dotnet、Homebrew 等固定位置，
        #   装了系统级 .NET 的机器上会探到真 SDK，「只有 10.0.100 时认不出」就随机器而变（10-24 #76 作者 Intel 机上实撞）
        real = ipa._dotnet_candidates
        only_tmp = lambda: [p for p in real() if str(p).startswith(str(self.root))]
        with um.patch.object(Path, "home", return_value=home), um.patch.dict(os.environ, env), \
             um.patch.object(ipa, "_dotnet_candidates", side_effect=only_tmp):
            # compat/tree/global.json 要 10.0.403 + latestFeature：只有 10.0.100 不算有 SDK
            lists("10.0.100")
            self.assertEqual(ipa.dotnet_sdk(), (None, None))
            (old.parent / "10.0.403").mkdir()
            lists("10.0.100", "10.0.403")
            self.assertEqual(ipa.dotnet_sdk(), (home / ".dotnet", host))

    def test_install_dotnet_reports_download_progress(self):
        # 10-24 #152：官方安装脚本下载时零输出、23 分钟看着像卡死。期望：按 --dry-run 给的包地址和 HEAD 大小，下载期间报「已下载 N / 总数」
        import unittest.mock as um, toolchain, ipa, subprocess as sp, time
        url = "https://builds.dotnet.microsoft.com/dotnet/Sdk/10.0.402/dotnet-sdk-10.0.402-osx-x64.tar.gz"
        def fake_sub(args, *a, **k):
            if "--dry-run" in args:
                return sp.CompletedProcess(args, 0, "dotnet-install: URL #0 - aka.ms: " + url + "\n", "")
            if args[:2] == ["curl", "-fsIL"]:
                return sp.CompletedProcess(args, 0, "HTTP/2 200\r\ncontent-length: 3000\r\n", "")
            raise AssertionError(args)
        seen = []
        def fake_run(args, log=None, env=None, cwd=None, on_line=None):
            if args[0] == "curl":
                return
            zp = Path(env["zip_path"])
            for i in range(3):
                with zp.open("ab") as f: f.write(b"x" * 1000)
                time.sleep(0.15)
        with um.patch.object(toolchain, "DOTNET_PROGRESS_S", 0.05), um.patch.object(toolchain.subprocess, "run", side_effect=fake_sub), \
             um.patch.object(self.c, "run", side_effect=fake_run), um.patch.object(self.c, "emit", side_effect=lambda *a, **k: seen.append(k)), \
             um.patch.object(ipa, "dotnet_sdk", return_value=(Path("/x"), Path("/x/dotnet"))):
            toolchain.install_dotnet(self.c)
        progress = [k for k in seen if k.get("unit") == "B"]
        self.assertTrue(progress, "下载期间没有进度")
        self.assertEqual(progress[0]["total"], 3000.0)
        self.assertTrue(all(0 < k["done"] <= 3000 for k in progress))

    def test_dotnet_sdk_version_rule_matches_global_json(self):
        # 期望按 .NET 的 rollForward 规则手写（与 tools/doctor.sh 同一判法），不跑被测代码推
        import ipa
        req = ("10.0.403", "latestFeature")
        for version, ok in [("10.0.403", True), ("10.0.404", True), ("10.0.500", True), ("10.0.402", False), ("10.0.400", False),
                            ("10.0.301", False), ("10.0.100", False), ("10.1.100", False), ("11.0.100", False), ("9.0.300", False),
                            ("10.0.404-rc.1.25", True)]:
            self.assertEqual(ipa._sdk_ok(version, req), ok, version)
        self.assertTrue(ipa._sdk_ok("10.0.100", None))
        self.assertFalse(ipa._sdk_ok("9.0.100", None))
        self.assertEqual(ipa._sdk_requirement(), req)

    @shared_extract
    def test_dotnet_wrapper_fallback_and_ipa_environment(self):
        import unittest.mock as um, ipa, shlex
        home = self.root / "user home"; home.mkdir()
        sdk_root = self.root / "actual SDK root"
        (sdk_root / "sdk/10.0.403").mkdir(parents=True)
        bins = [self.root / name for name in ("runtime bin", "failed bin", "timeout bin", "wrapper bin")]
        scripts = ['exit 0', 'exit 7', 'exec /bin/sleep 10',
                   '[ -z "${DOTNET_ROOT:-}${DOTNET_ROOT_ARM64:-}${DOTNET_ROOT_X64:-}" ] || exit 9\nprintf "%s\\n" ' + shlex.quote(f"10.0.403 [{sdk_root / 'sdk'}]")]
        for directory, script in zip(bins, scripts):
            directory.mkdir()
            host = directory / "dotnet"
            host.write_text('#!/bin/sh\n[ "$1" = --list-sdks ] || exit 2\n' + script + '\n')
            host.chmod(0o755)
        selected = bins[-1] / "dotnet"
        real_probe = subprocess.run
        def probe(args, **kwargs):
            if args[0] == str(bins[2] / "dotnet"):
                self.assertEqual(kwargs["timeout"], 3)
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            # 真跑的桩（成功 / 失败退出 / 包装脚本）不按产品的 3 秒探测上限：这条用例测的是回退顺序与环境剥离，不是快慢；
            #   11-02 合入线：满负载时 sh 桩起进程超 3 秒，包装脚本那支被当超时跳过，返回 (None, None) 偶发红
            return real_probe(args, **{**kwargs, "timeout": 120})
        base = self.package("base"); self.catalogue([self.game(packages=[base])])
        self.c.r.update(gameId="0100000000000000", target={"gpuArch": "applegpu_g18p", "os": 27})
        fonts = self.root / "fonts"; fonts.mkdir()
        seen = {}
        def run(args, log=None, env=None, cwd=None, on_line=None):
            seen.update(env=env)
            out = Path(args[args.index("--out") + 1]); (out / "synthetic.ipa").write_bytes(b"synthetic IPA fixture")
            oneclick_done(args, out / "synthetic.ipa", graphics_mode="direct")
        env = {"DOTNET": str(bins[0] / "dotnet"), "DOTNET_ROOT": str(bins[0]),
               "DOTNET_ROOT_ARM64": str(bins[0]), "DOTNET_ROOT_X64": str(bins[0]), "PATH": os.pathsep.join(str(p) for p in bins)}
        with um.patch.object(Path, "home", return_value=home), um.patch.dict(os.environ, env), um.patch.object(ipa.subprocess, "run", side_effect=probe):
            self.assertEqual(ipa.dotnet_sdk(), (sdk_root, selected))
            with um.patch.object(ipa, "resolve_target", return_value={"gpuArch": "applegpu_g18p", "os": 27}), \
                 um.patch.object(self.c, "fonts_dir", return_value=fonts), um.patch.object(self.c, "env", return_value=dict(env)), \
                 um.patch.object(self.c, "run", side_effect=run):
                ipa.build(self.c)
            self.assertEqual(seen["env"]["DOTNET"], str(selected))
            self.assertEqual(seen["env"]["DOTNET_ROOT"], str(sdk_root))
            self.assertEqual(seen["env"]["PATH"].split(os.pathsep)[:2], [str(selected.parent), str(sdk_root)])
            self.assertNotIn("DOTNET_ROOT_ARM64", seen["env"])
            self.assertNotIn("DOTNET_ROOT_X64", seen["env"])
            # Runtime-only, failed and timed-out candidates do not count as an SDK.
            with um.patch.object(ipa, "_dotnet_candidates", return_value=[p / "dotnet" for p in bins[:-1]]):
                self.assertEqual(ipa.dotnet_sdk(), (None, None))

    def test_target_arch_via_deployment_variant(self):
        # 机型号 → GPU 架构：Xcode 27.1 的库里 Devices.DeviceTraitSet 是占位（指向 g14g），真值在 DeploymentVariant（老系统行是 air64、要跳过）；
        # iPad 机型号带 -A / -B 后缀按前缀认；没有 applegpu 记录 → 报「认不出」；可选机型只列 g13（A14）起、新的在前、同名去重
        import sqlite3, target as tg
        dev = self.root / "Xcode.app/Contents/Developer"; db = dev / "Platforms/iPhoneOS.platform/usr/standalone/device_traits.db"; db.parent.mkdir(parents=True)
        con = sqlite3.connect(db)
        con.executescript("""create table DeviceTraits (DeviceTraitSetID integer primary key, GPUArchName text);
            create table Devices (Target text, ProductType text, ProductDescription text, DeviceTraitSet integer);
            create table DeploymentVariant (DeploymentTarget text, DeviceTarget text, DeviceTraitSet integer);
            insert into DeviceTraits values (1,'applegpu_g14g'),(2,'air64'),(3,'applegpu_g18p'),(4,'applegpu_g17g'),(5,'applegpu_g12p'),(6,'applegpu_g13p');
            insert into Devices values ('v54ap','iPhone18,2','iPhone 17 Pro Max',1),('j717ap','iPad17,1-A','iPad Pro 11-inch (M5)',1),('j717bap','iPad17,1-B','iPad Pro 11-inch (M5)',1),
                ('n104ap','iPhone12,1','iPhone 11',1),('k93ap','iPad2,1','iPad 2',1),('d53gap','iPhone13,2','iPhone 12',1);
            insert into DeploymentVariant values ('17.0','v54ap',2),('26.0','v54ap',3),('26.0','j717ap',4),('26.0','j717bap',4),('26.0','n104ap',5),('9.0','k93ap',2),('26.0','d53gap',6);""")
        con.commit(); con.close()
        self.assertEqual(tg.gpu_arch("iPhone18,2", dev), "applegpu_g18p")
        self.assertEqual(tg.gpu_arch("iPad17,1", dev), "applegpu_g17g")
        with self.assertRaises(tg.TargetError): tg.gpu_arch("iPad2,1", dev)
        self.assertEqual([(m["name"], m["productType"]) for m in tg.models(dev)],
                         [("iPhone 17 Pro Max", "iPhone18,2"), ("iPhone 12", "iPhone13,2"), ("iPad Pro 11-inch (M5)", "iPad17,1")])
        self.assertEqual(tg.check(dict(model="m", gpuArch="applegpu_g13p", os=26)), dict(gpuArch="applegpu_g13p", os=26, model="m"))
        for bad in (dict(gpuArch="applegpu_g12p", os=27), dict(gpuArch="applegpu_g18p", os=17), dict(gpuArch="", os=27)):
            with self.assertRaises(tg.TargetError): tg.check(bad)
        self.assertEqual(tg.check(dict(gpuArch="applegpu_g18p", os=27), 27)["os"], 27)
        with self.assertRaises(tg.TargetError) as e: tg.check(dict(gpuArch="applegpu_g18p", os=27), 26)
        self.assertIn("更新到最新版", str(e.exception))


    def test_pack_space_needs_unpack_plus_direct(self):
        # 预检门槛 = 原包 × 1.1 + 直连上限：恰好够放行，差 1 字节拦下，提示里写需要与剩余
        import unittest.mock as um, collections
        base = dict(self.package("base"), size=6 << 30)
        game = self.game(packages=[base]); need = (6 << 30) * 11 // 10 + 15_000_000_000
        du = collections.namedtuple("du", "total used free")
        with um.patch("shutil.disk_usage", return_value=du(0, 0, need)):
            library.check_pack_space(self.c, game)
        with um.patch("shutil.disk_usage", return_value=du(0, 0, need - 1)):
            with self.assertRaises(Failure) as e: library.check_pack_space(self.c, game)
        self.assertIn(f"要 {need / 1e9:.0f} GB", str(e.exception)); self.assertIn(f"只剩 {(need - 1) / 1e9:.0f} GB", str(e.exception))

    def test_stage_sources_links_originals_in_place(self):
        # 原件原地只读（11-02）：出包原件目录里是指向原件的软链（按内容哈希命名、整合版同一文件列两次只链一次），不复制、不改原件；
        #   旧版复制进游戏库的托管副本（相对路径）照样链到库里那份
        outside = self.root / "exfat-drive"; outside.mkdir()
        original = outside / "整合版.xci"; original.write_bytes(b"synthetic combined image")
        base = dict(self.package("base"), path=str(original), sha256="a" * 64)
        update = dict(self.package("update"), path=str(original), sha256="a" * 64)
        legacy = dict(self.package("dlc", sha="c" * 64))
        (self.c.library / legacy["path"]).write_bytes(b"legacy managed copy")
        stage = self.root / "stage"
        library.stage_sources(self.c, [base, update, legacy], stage)
        entries = sorted(stage.iterdir())
        self.assertEqual([p.name for p in entries], ["a" * 64 + ".xci", "c" * 64 + Path(legacy["path"]).suffix])
        self.assertTrue(all(p.is_symlink() for p in entries))
        self.assertEqual(entries[0].resolve(), original.resolve())
        self.assertEqual(entries[1].resolve(), (self.c.library / legacy["path"]).resolve())
        self.assertEqual(original.read_bytes(), b"synthetic combined image")

    def test_missing_original_says_where(self):
        # 原件在的盘没接上 / 被挪走：缺项直接写原件位置和怎么办，不是「托管文件缺失」
        gone = self.root / "unplugged" / "game.nsp"
        game = self.game(packages=[dict(self.package("base"), path=str(gone))])
        [missing] = library.requirements(self.c, game)
        self.assertIn("找不到游戏文件「" + gone.name, missing); self.assertNotIn(str(gone.parent), missing); self.assertIn("接上磁盘", missing)

    def space_events(self):
        return [json.loads(l) for l in self.c.events.read_text().splitlines() if json.loads(l)["type"] == "space"]

    def test_space_budget_adds_direct_library_for_every_game(self):
        # 直连优先、零配置（10-25）：每款都另加直连库预算（按动森实测 × 体积换算），不看有没有手写配置档；只有直连（11-07），没有不加的分支
        import collections
        import unittest.mock as um
        du = collections.namedtuple("du", "total used free")
        for tid in ("01006F8002326000", "0100000000000000"):
            with self.subTest(tid=tid):
                game = self.game(tid=tid, packages=[dict(self.package("base", tid=tid), size=6 << 30)])
                with um.patch("shutil.disk_usage", return_value=du(0, 0, 50 << 30)):
                    lib = library.space_budget(self.c, game)["volumes"][0]
                self.assertEqual(lib["needBytes"], (6 << 30) * 11 // 10 + 15_000_000_000)
                self.assertIn("直连着色器上限", lib["note"])

    def test_space_budget_uses_unpacked_bytes_for_compressed_packages(self):
        # nsz / xcz（10-26）：文件比解出来小得多，按 inspect 的 contentBytes（清单里原始 NCA 大小之和）与文件字节的大值算；没有 contentBytes 的老记录照旧按文件字节
        import collections
        import unittest.mock as um
        du = collections.namedtuple("du", "total used free")
        for label, pkg, want in (("nsz", dict(size=1 << 30, contentBytes=6 << 30), 6 << 30), ("old record", dict(size=6 << 30), 6 << 30),
                                 ("nsp", dict(size=6 << 30, contentBytes=5 << 30), 6 << 30)):
            with self.subTest(label=label):
                game = self.game(packages=[dict(self.package("base"), **pkg)])
                with um.patch("shutil.disk_usage", return_value=du(0, 0, 50 << 30)):
                    budget = library.space_budget(self.c, game)
                self.assertEqual(budget["gameBytes"], want)
                self.assertEqual(budget["volumes"][0]["needBytes"], want * 11 // 10 + 15_000_000_000)

    def test_space_budget_library_volume_only(self):
        # 11-02 起打 iPhone 包不再把原件暂存到数据目录（共用 游戏库/cache/<程序号>/extract），预算只有游戏库一卷：原件跨卷与否都一样
        # 预算明细：游戏库一条（经验门槛）；未量化开销单列；事件里不出现绝对路径
        import collections
        import unittest.mock as um
        base = dict(self.package("base"), size=6 << 30)
        source = self.c.library / base["path"]; source.write_bytes(b"x" * 1000)
        game = self.game(packages=[base])
        du = collections.namedtuple("du", "total used free")
        real_stat = Path.stat
        for label, src_dev, expect_staging in (("same volume", 9, False), ("cross volume", 1, True)):
            with self.subTest(label=label):
                def fake_stat(path, *args, **kwargs):
                    info = real_stat(path, *args, **kwargs)
                    device = {source: src_dev, self.c.private: 9}.get(path)
                    if device is None:
                        return info
                    values = list(info); values[2] = device
                    return os.stat_result(values)
                def usage(path):
                    return du(0, 0, 5000 if Path(path) == self.c.private else 50 << 30)
                with um.patch.object(Path, "stat", fake_stat), um.patch("shutil.disk_usage", side_effect=usage):
                    budget = library.space_budget(self.c, game)
                    library.check_pack_space(self.c, game)
                lib = budget["volumes"][0]
                self.assertEqual((lib["role"], lib["basis"], lib["needBytes"], lib["freeBytes"], lib["enough"], lib["volume"]),
                                 ("library", "rule", (6 << 30) * 11 // 10 + 15_000_000_000, 50 << 30, True, "系统盘"))
                self.assertIn("直连着色器上限", lib["note"]); self.assertEqual(lib["unknown"], ["shader_cache"])
                self.assertEqual(budget["gameBytes"], 6 << 30)
                self.assertEqual([u["id"] for u in budget["unknown"]], ["shader_cache"])
                self.assertIn("未量化", budget["unknown"][0]["note"])
                self.assertEqual(len(budget["volumes"]), 1)

    def test_space_rejection_emits_budget_and_persists_in_job(self):
        # 游戏库不够：先发 space 事件（enough=false）再报 disk_full；后续阶段事件不冲掉任务文件里的 space
        import collections
        import unittest.mock as um
        base = dict(self.package("base"), size=1 << 30)
        game = self.game(packages=[base])
        du = collections.namedtuple("du", "total used free")
        with um.patch("shutil.disk_usage", return_value=du(0, 0, 5 << 30)):
            with self.assertRaises(Failure) as e: library.check_pack_space(self.c, game)
        self.assertEqual(e.exception.code, "disk_full")
        self.assertIn("磁盘空间不够", str(e.exception))
        event = self.space_events()[-1]
        self.assertFalse(event["space"]["volumes"][0]["enough"])
        self.assertEqual(event["space"]["volumes"][0]["needBytes"], (1 << 30) * 11 // 10 + 5_000_000_000)
        self.c.begin("下一阶段", 2, 3); self.c.emit(done=1, total=2)
        job = json.loads(self.c.jobpath.read_text())
        self.assertEqual(job["space"], event["space"]); self.assertEqual(job["message"], "下一阶段")

    def test_mac_prepare_space_rejection_keeps_previous_runtime(self):
        # Mac 打包：空间不够在建缓存 / 解包之前就拦；上次成功的运行版本与 current 链接原样保留，也不发计划
        import collections
        import unittest.mock as um
        base = dict(self.package("base"), size=1 << 30)
        game = self.game(packages=[base])
        self.catalogue([game])
        game_root = self.c.library / "games" / game["id"]
        release = game_root / "releases" / "working"
        (release / "runtime").parent.mkdir(parents=True); (release / "runtime").write_text("keep")
        (game_root / "current").symlink_to(release.relative_to(game_root))
        self.c.r.update(gameId=game["id"])
        du = collections.namedtuple("du", "total used free")
        with um.patch("shutil.disk_usage", return_value=du(0, 0, 1)), um.patch.object(self.c, "run") as run, \
             um.patch.object(build, "digest") as dig:
            with self.assertRaises(Failure) as e: build.prepare(self.c)
        self.assertEqual(e.exception.code, "disk_full")
        run.assert_not_called(); dig.assert_not_called()
        self.assertFalse((self.c.library / "cache" / game["id"]).exists())
        self.assertEqual((game_root / "current" / "runtime").read_text(), "keep")
        self.assertEqual(os.readlink(game_root / "current"), "releases/working")
        types = [json.loads(l)["type"] for l in self.c.events.read_text().splitlines()]
        self.assertIn("space", types); self.assertNotIn("plan", types)

    def test_library_on_unplugged_disk_says_plug_it_in(self):
        # 游戏库在没插的外接盘上：报「磁盘没连上」并写出盘名；本机磁盘上标识不符仍是原提示
        from pathlib import Path as P
        self.c.r["libraryId"] = "x"
        self.c.library = P("/Volumes/Synthetic/游戏库")
        with self.assertRaises(Failure) as e: self.c.require_library()
        self.assertIn("「Synthetic」没连上", str(e.exception))
        self.c.library = self.root / "lib-elsewhere"
        with self.assertRaises(Failure) as e: self.c.require_library()
        self.assertIn("标识不符", str(e.exception))

    def test_detect_xcode_picks_newest_complete_xcode(self):
        # 与前端 Store.detectXcode 同一规则：只认 com.apple.dt.Xcode 且有 usr/bin/xcodebuild 的；版本按数字比（27.1 > 27.0 > 26.6，10 > 9）
        import plistlib, toolchain, unittest.mock as um
        apps = self.root / "Applications"
        def mk(name, version, bid="com.apple.dt.Xcode", complete=True):
            app = apps / name; (app / "Contents/Developer/usr/bin").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(dict(CFBundleIdentifier=bid, CFBundleShortVersionString=version)))
            if complete: (app / "Contents/Developer/usr/bin/xcodebuild").write_text("")
        mk("Xcode.app", "26.6"); mk("Xcode-27.app", "27.0"); mk("Xcode-27.1.app", "27.1")
        mk("Xcode-beta.app", "28.0", complete=False); mk("XcodeTool.app", "99", bid="com.example.other")
        with um.patch.object(toolchain.subprocess, "run", side_effect=OSError):
            self.assertEqual(toolchain.detect_xcode((str(apps),)), str(apps / "Xcode-27.1.app/Contents/Developer"))
            self.assertIsNone(toolchain.detect_xcode((str(self.root / "none"),)))

    def test_installed_xcodes_by_version_and_build_not_path(self):
        # 11-07 gate-host 现场：Xcode-27.app 被 App Store 原地升到 27.1（构建号 27A9275），Xcode-27.1.app 是指向它的软链 → 只算一套；
        #   改名拷贝（同版本同构建号）也只算一套；同版本不同构建号（beta 与正式）算两套；路径名写 27 不影响读出的 27.1
        import plistlib, os, toolchain, unittest.mock as um
        apps = self.root / "Apps"
        def mk(name, version, build):
            app = apps / name; (app / "Contents/Developer/usr/bin").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(dict(CFBundleIdentifier="com.apple.dt.Xcode", CFBundleShortVersionString=version)))
            (app / "Contents/version.plist").write_bytes(plistlib.dumps(dict(ProductBuildVersion=build)))
            (app / "Contents/Developer/usr/bin/xcodebuild").write_text("")
        mk("Xcode-26.6.app", "26.6", "17F113"); mk("Xcode-27.app", "27.1", "27A9275")
        os.symlink(apps / "Xcode-27.app", apps / "Xcode-27.1.app")
        mk("Xcode-copy.app", "27.1", "27A9275"); mk("Xcode-27.1-rc.app", "27.1", "27A9270")
        with um.patch.object(toolchain.subprocess, "run", side_effect=OSError):
            got = [(vs, b) for _, vs, b, _ in toolchain.installed_xcodes((str(apps),))]
        self.assertEqual(sorted(got), sorted([("27.1", "27A9275"), ("27.1", "27A9270"), ("26.6", "17F113")]))
        self.assertEqual(got[-1], ("26.6", "17F113"))   # 版本新的在前

    def test_fonts_dir_prefers_player_then_bundled(self):
        # 共享字体：玩家选的目录（含 .bfttf）优先；没选或选的不含 .bfttf → 应用自带 Resources/Fonts；都没有 → None
        res = self.root / "res"; (res / "Fonts").mkdir(parents=True); (res / "Fonts" / "a.bfttf").write_bytes(b"x")
        mine = self.root / "mine"; mine.mkdir(); empty = self.root / "empty"; empty.mkdir()
        self.c.r.update(resources=str(res))
        self.assertEqual(self.c.fonts_dir(), (res / "Fonts").resolve())
        self.c.r.update(fontsDir=str(empty))
        self.assertEqual(self.c.fonts_dir(), (res / "Fonts").resolve())
        (mine / "b.bfttf").write_bytes(b"y"); self.c.r.update(fontsDir=str(mine))
        self.assertEqual(self.c.fonts_dir(), mine.resolve())
        self.c.r.update(fontsDir="", resources=str(self.root / "nores"))
        self.assertIsNone(self.c.fonts_dir())

    def test_library_identity_reopen_and_offline_drive(self):
        self.assertEqual(library.setup(self.c)["libraryId"], self.c.r["libraryId"])
        marker = self.c.library / ".s2arm-library.json"
        marker.unlink()
        with self.assertRaises(Failure):
            self.c.catalog()
        self.assertFalse(marker.exists())

    def test_caches_follow_a_library_outside_the_private_root(self):
        """外接盘上的游戏库：出包缓存和运行目录要跟着走，不写系统盘；缺省游戏库（在数据目录里）位置不变。"""
        outside = self.root / "external" / "lib"
        c = Context(self.request | dict(taskId="outside", library=str(outside)))
        self.assertEqual(c.work_root, outside.resolve() / ".s2arm-work")
        env = c.env()
        self.assertEqual(env["S2A_RUN_ROOT"], str(outside.resolve() / ".s2arm-work/run"))
        self.assertTrue(env["GAL_PSO_CACHE"].startswith(str(outside.resolve())))
        inside = Context(self.request | dict(taskId="inside", library=str(Path(self.request["privateRoot"]) / "lib")))
        self.assertEqual(inside.work_root, inside.private)
        self.assertEqual(inside.env()["S2A_RUN_ROOT"], str(inside.private / "run"))

    def test_refuses_nonempty_or_legacy_library(self):
        other = self.root / "legacy"
        other.mkdir()
        (other / ".nsmac-library.json").write_text("original")
        c = Context(self.request | dict(taskId="legacy", library=str(other)))
        with self.assertRaises(Failure):
            library.setup(c)
        self.assertEqual((other / ".nsmac-library.json").read_text(), "original")

    def test_missing_base_and_aarch32_are_not_buildable(self):
        update = self.package("update", 2)
        self.assertTrue(any("缺游戏本体" in m for m in library.requirements(self.c, self.game(packages=[update]))))
        base = self.package()
        base["guestIsa"] = "aarch32"
        self.assertTrue(any("32 位" in x for x in library.requirements(self.c, self.game(packages=[base]))))
        # 本体 32 位、最新更新 64 位（马车8 1.0 → 4.0.1）：按更新判 → 能打包；更新读不出指令集（unknown）→ 退回本体 → 不能打包
        update64 = dict(self.package("update", 2), guestIsa="aarch64")
        self.assertFalse(any("32 位" in x for x in library.requirements(self.c, self.game(packages=[base, update64]))))
        update_unknown = dict(self.package("update", 2), guestIsa="unknown")
        self.assertTrue(any("32 位" in x for x in library.requirements(self.c, self.game(packages=[base, update_unknown]))))

    def test_newest_update_and_each_dlc_are_frozen(self):
        packages = [self.package(), self.package("update", 1), self.package("update", 2),
                    self.package("dlc", 1, "0100000000001001"), self.package("dlc", 4, "0100000000001001")]
        game = self.game(packages=packages)
        self.assertEqual([p["version"] for p in library.selected_packages(game)], [0, 2, 4])
        identity = library.input_identity(game)
        self.assertEqual(identity, library.input_identity(self.game(packages=list(reversed(packages)))))
        packages[2]["sha256"] = "b" * 64
        self.assertNotEqual(identity, library.input_identity(game))

    def test_catalogue_keeps_unsupported_and_unverified_distinct(self):
        game = self.game(packages=[self.package()])
        self.catalogue([game])
        output = library.catalog(self.c)
        self.assertEqual(output["games"][0]["compatibility"], "experimental")
        self.assertFalse(output["games"][0]["ready"])
        self.assertTrue(output["games"][0]["needsPreparation"])

    def test_catalogue_lists_job_records_not_retained_requests(self):
        # 任务目录里同一任务有记录 <id>.json 和留存的请求 <id>.request.json（J10 resume）；catalog 只该返回记录。
        #   请求混进来时启动器把整个任务列表解码失败（10-25 main 上 BudgetTests 重开读回 space 为 nil）
        self.catalogue([self.game(packages=[self.package()])])
        jobs = self.c.private / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / "older.json").write_text(json.dumps({"id": "older", "operation": "build", "state": "failed"}), encoding="utf-8")
        (jobs / "older.request.json").write_text(json.dumps({"schema": 1, "taskId": "older", "operation": "build"}), encoding="utf-8")
        output = library.catalog(self.c)
        self.assertEqual([j.get("id") for j in output["jobs"]], ["older"])
        self.assertTrue(all("state" in j for j in output["jobs"]))

    def test_catalogue_returns_only_newest_job_records(self):
        # 11-07：任务目录 1.5 万条记录时 catalog 整份读 + 回传 7 MB；只回最新 JOB_LIST_LIMIT 条（按修改时间）
        self.catalogue([self.game(packages=[self.package()])])
        jobs = self.c.private / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for i in range(library.JOB_LIST_LIMIT + 5):
            f = jobs / f"t{i:04d}.json"
            f.write_text(json.dumps({"id": f"t{i:04d}", "operation": "catalog", "state": "completed", "ended": now}), encoding="utf-8")
            os.utime(f, (now - 1000 + i, now - 1000 + i))   # 编号越大越新
        output = library.catalog(self.c)
        ids = [j["id"] for j in output["jobs"]]
        self.assertEqual(len(ids), library.JOB_LIST_LIMIT)
        self.assertEqual(ids[0], f"t{library.JOB_LIST_LIMIT + 4:04d}")
        self.assertNotIn("t0000", ids)

    def test_game_data_cannot_escape_library(self):
        p = self.package()
        p["path"] = "../../outside.nsp"
        with self.assertRaises(Failure):
            library.requirements(self.c, self.game(packages=[p]))

    def test_runtime_environment_maps_language_and_never_reseeds_saves(self):
        self.c.r.update(gameLanguage="zh-Hant", docked=True)
        os.environ["S2A_SAVE_DIR"] = "/do-not-use"
        os.environ["S2A_AUTOPLAY"] = "1"
        os.environ["S2A_ROUTE"] = "/do-not-use"
        try:
            env = build.runtime_env(self.c, self.game(), self.root / "runtime")
        finally:
            for name in ("S2A_SAVE_DIR", "S2A_AUTOPLAY", "S2A_ROUTE"):
                os.environ.pop(name)
        self.assertEqual(env["S2A_SYSTEM_LANGUAGE"], "TraditionalChinese")
        self.assertEqual(env["S2A_DOCKED"], "1")
        self.assertEqual(env["S2A_AUTOPLAY"], "0")
        self.assertNotIn("S2A_SAVE_DIR", env)
        self.assertNotIn("S2A_ROUTE", env)
        self.assertTrue(env["S2A_RYU_DATA"].startswith(str(self.c.library / "saves")))

    def test_runtime_environment_follows_system_language_and_docks_by_default(self):
        # 维护者 11-07：不给玩家选游戏语言 / 主机掌机——请求不带 gameLanguage 就不设 S2A_SYSTEM_LANGUAGE（继承来的也不带，c.env 去掉 S2A_*；宿主跟系统首选语言）；
        # 不带 docked 是主机模式，只有明说 false 才掌机
        os.environ["S2A_SYSTEM_LANGUAGE"] = "Japanese"
        try:
            env = build.runtime_env(self.c, self.game(), self.root / "runtime")
        finally:
            os.environ.pop("S2A_SYSTEM_LANGUAGE")
        self.assertNotIn("S2A_SYSTEM_LANGUAGE", env)
        self.assertEqual(env["S2A_DOCKED"], "1")
        self.c.r["docked"] = False
        self.assertEqual(build.runtime_env(self.c, self.game(), self.root / "runtime")["S2A_DOCKED"], "0")

    def test_invalid_language_is_rejected(self):
        self.c.r["gameLanguage"] = "zhHans; command"
        with self.assertRaises(Failure):
            build.runtime_env(self.c, self.game(), self.root)

    def test_metal_hud_switch_and_explicit_environment_reach_real_child(self):
        names = ("MTL_HUD_ENABLED", "MTL_HUD_LOG_ENABLED", "MTL_HUD_LOG_SHADER_ENABLED")
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            for dev_hud, metal_hud, override, expected in (
                    (False, False, None, None), (True, False, None, None),
                    (False, True, None, "1"), (True, True, None, "1"),
                    (True, True, "0", "0"), (False, False, "1", "1")):
                self.c.r.update(devHud=dev_hud, metalHud=metal_hud)
                if override is None:
                    os.environ.pop("MTL_HUD_ENABLED", None)
                else:
                    os.environ["MTL_HUD_ENABLED"] = override
                env = build.runtime_env(self.c, self.game(), self.root / "runtime")
                lines = []
                self.c.run(["/usr/bin/env"], env=env, on_line=lines.append)
                child = dict(line.split("=", 1) for line in lines if "=" in line)
                self.assertEqual(child.get("MTL_HUD_ENABLED"), expected)
                self.assertEqual(child.get("S2A_HUD"), "1" if dev_hud else None)
                self.assertNotIn("MTL_HUD_LOG_ENABLED", child)
                self.assertNotIn("MTL_HUD_LOG_SHADER_ENABLED", child)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_mac_input_guide_freezes_the_confirmed_mapping_per_game_and_launch(self):
        def mapping(code):
            return dict(schema=1, keyboard={"A": {"code": code}}, mouse={"Y": 0}, gamepad={"A": "east"},
                        axes={}, deadzone=.15, mouseOptions=dict(mode="off", sensitivityX=1, sensitivityY=1))
        first = self.game(); second = self.game(tid="0100000000000001"); second["name"] = "Another game"
        defaults, saved, confirmed = mapping(14), mapping(36), mapping(3)
        atomic(self.c.private / "default-input.json", defaults)
        atomic(self.c.library / "mappings" / (first["id"] + ".json"), saved)
        before = build.runtime_env(self.c, first, self.root / "runtime")
        next_context = Context(dict(self.request, taskId=str(uuid.uuid4()), launchInputConfig=confirmed,
                                    inputGuideHandled=True, uiLanguage="en"))
        actual = build.runtime_env(next_context, first, self.root / "runtime")
        other_context = Context(dict(self.request, taskId=str(uuid.uuid4())))
        other = build.runtime_env(other_context, second, self.root / "runtime2")
        self.assertNotEqual(before["S2A_INPUT_CONFIG"], actual["S2A_INPUT_CONFIG"])
        self.assertNotEqual(actual["S2A_INPUT_CONFIG"], other["S2A_INPUT_CONFIG"])
        self.assertEqual(json.loads(Path(before["S2A_INPUT_CONFIG"]).read_text()), saved)
        self.assertEqual(json.loads(Path(actual["S2A_INPUT_CONFIG"]).read_text()), confirmed)
        self.assertEqual(json.loads(Path(other["S2A_INPUT_CONFIG"]).read_text()), defaults)
        self.assertEqual(actual["S2A_GAME_NAME"], first["name"])
        self.assertEqual(other["S2A_GAME_NAME"], second["name"])
        self.assertEqual(actual["S2A_GAME_ID"], first["id"])
        self.assertEqual(actual["S2A_INPUT_GUIDE_SHOWN"], "1")
        self.assertNotIn("S2A_INPUT_GUIDE_SHOWN", other)
        self.assertEqual(actual["S2A_UI_LANGUAGE"], "en")
        self.assertTrue(actual["S2A_INPUT_GUIDE_PREFS"].endswith(first["id"] + ".json"))
        self.assertNotEqual(actual["S2A_INPUT_GUIDE_PREFS"], other["S2A_INPUT_GUIDE_PREFS"])

    def test_mac_input_guide_rejects_malformed_confirmed_mapping(self):
        for value in ({}, [], {"schema": 1}):
            self.c.r["launchInputConfig"] = value
            with self.assertRaises(Failure):
                build.runtime_env(self.c, self.game(), self.root)

    def test_launch_rejects_another_games_runtime_before_starting_process(self):
        game = self.game(); self.catalogue([game]); self.c.r["gameId"] = game["id"]
        with patch.object(build, "validate_runtime", return_value={"gameId": "0100000000000001"}), patch.object(self.c, "run") as run:
            with self.assertRaisesRegex(Failure, "不匹配"):
                build.launch(self.c)
            run.assert_not_called()

    def test_launch_confirms_mapping_support_on_this_games_host(self):
        game = self.game(); self.catalogue([game]); self.c.r.update(gameId=game["id"], launchInputConfig={"schema": 1})
        with patch.object(build, "validate_runtime", return_value={"gameId": game["id"], "memoryRequirementsVersion": 2}), patch.object(build.toolchain, "input_interface", return_value=(False, "legacy")) as probe, patch.object(self.c, "run") as run:
            with self.assertRaisesRegex(Failure, "重新打包"):
                build.launch(self.c)
            probe.assert_called_once_with(self.c, (self.c.library / "games" / game["id"] / "current").resolve() / "S2ArmGame.app/Contents/MacOS/s2arm-host")
            run.assert_not_called()

    def test_launch_removes_only_its_snapshot_after_exit_failure_or_cancellation(self):
        game = self.game(); self.catalogue([game]); self.c.r.update(gameId=game["id"], inputGuideHandled=True,
            launchInputConfig=dict(schema=1, keyboard={"A": {"code": 14}}, mouse={}, gamepad={}, axes={},
                                   deadzone=.15, mouseOptions=dict(mode="off", sensitivityX=1, sensitivityY=1)))
        sibling = self.c.private / "runtime-input" / game["id"] / "another-run.json"
        atomic(sibling, {"keep": True})
        for failure in (None, Failure("fixture host failed"), Cancelled()):
            captured = []
            def run(command, log, env, **kwargs):
                path = Path(env["S2A_INPUT_CONFIG"]); captured.append(path)
                self.assertTrue(path.is_file())
                self.assertEqual(env["S2A_INPUT_GUIDE_SHOWN"], "1")
                if failure is not None:
                    raise failure
            with patch.object(build, "validate_runtime", return_value={"gameId": game["id"], "memoryRequirementsVersion": 2}), patch.object(build.toolchain, "input_interface", return_value=(True, "fixture")), patch.object(self.c, "run", side_effect=run):
                if failure is None:
                    build.launch(self.c)
                else:
                    with self.assertRaises(type(failure)):
                        build.launch(self.c)
            self.assertEqual(len(captured), 1)
            self.assertFalse(captured[0].exists())
            self.assertTrue(sibling.is_file())

    def test_hash_validation_rejects_same_size_corruption_and_traversal(self):
        root = self.root / "products"
        root.mkdir()
        f = root / "module"
        f.write_bytes(b"valid")
        record = {"module": [5, digest(f)]}
        build.validate_files(self.c, root, record)
        f.write_bytes(b"wrong")
        with self.assertRaises(Failure):
            build.validate_files(self.c, root, record)
        with self.assertRaises(Failure):
            build.validate_files(self.c, root, {"../outside": [0, ""]})

    def test_size_check_rejects_symlinks_and_escaping_directories(self):
        # validate_files 每个目录只解析一次（11-07 提速）后，判据仍同逐个 contained()：目录软链指到外面、文件本身是软链、大小不对都拒
        root = self.root / "products2"
        (root / "sub").mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "m").write_bytes(b"12345")
        (root / "sub" / "ok").write_bytes(b"12345")
        build.validate_files(self.c, root, {"sub/ok": [5, ""]}, full=False)
        with self.assertRaises(Failure):
            build.validate_files(self.c, root, {"sub/ok": [4, ""]}, full=False)
        (root / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(Failure):
            build.validate_files(self.c, root, {"link/m": [5, ""]}, full=False)
        (root / "sub" / "filelink").symlink_to(outside / "m")
        with self.assertRaises(Failure):
            build.validate_files(self.c, root, {"sub/filelink": [5, ""]}, full=False)
        with self.assertRaises(Failure):
            build.validate_files(self.c, root, {"sub/missing": [5, ""]}, full=False)

    def test_cleanup_preserves_releases_saves_and_other_games_sources(self):
        package = self.package()
        game = self.game(packages=[package])
        other = self.game("0100000000000001", [package.copy()])
        self.catalogue([game, other])
        save = self.c.library / "saves" / game["id"] / "progress"
        release = self.c.library / "games" / game["id"] / "releases" / "working" / "runtime"
        for f in (save, release):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("keep")
        cache = self.c.library / "cache" / game["id"]
        cache.mkdir(parents=True)
        (cache / "scratch").write_text("discard")
        self.c.r.update(gameId=game["id"], kind="cache")
        library.clean(self.c)
        self.assertFalse(cache.exists())
        self.c.r["kind"] = "sources"
        library.clean(self.c)
        self.assertTrue((self.c.library / package["path"]).exists())
        self.assertEqual(save.read_text(), "keep")
        self.assertEqual(release.read_text(), "keep")

    def test_remove_only_hides_catalogue_entry(self):
        game = self.game(packages=[self.package()])
        self.catalogue([game])
        self.c.r["gameId"] = game["id"]
        library.remove(self.c)
        self.assertTrue(self.c.catalog()["games"][0]["hidden"])
        self.assertTrue((self.c.library / game["packages"][0]["path"]).exists())

    def _build_leftovers(self, gid):
        """造一款出完 iPhone 包的现场：输出目录（IPA / 资源包 / 日志 / 产物根 / 解包）+ 私有目录里按程序号存的缓存 + 别的款的缓存 + 共享 metallib 缓存"""
        low, run = gid.lower(), self.c.work_root / "run" / "s2arm"
        out = self.c.library / "iPhone" / gid
        keep = [out / f"t{low}.ipa", out / f"t{low}资源包" / "manifest.json", out / "logs" / "a.log"]
        gone = [out / "roots" / f"t{low}@现行" / "x", out / "extract" / "romfs" / "f", run / "cache" / "direct" / gid.upper() / "work" / "cas",
                run / "cache" / "bake" / f"{low}-abc" / "ok", run / "cache" / "aoc" / f"t{low}-k" / "aoc.tsv", run / "cache" / "srcver" / f"t{low}-k" / "r",
                run / "widen" / f"t{low}_b" / "ui", run / "widen" / "auto" / f"t{low}_b" / "w", self.c.work_root / "cache" / "air" / "ios" / "x.air"]
        other = [run / "cache" / "direct" / "0100AAAAAAAAA000" / "cas", run / "cache" / "bake" / "0100aaaaaaaaa000-abc" / "ok", self.c.work_root / "cache" / "nxaot" / "host.o"]
        for f in keep + gone + other:
            f.parent.mkdir(parents=True, exist_ok=True); f.write_text("x")
        return keep, gone, other

    def test_prune_build_keeps_ipa_pack_logs_and_clears_this_games_intermediates(self):
        # Q15 政策：成功后保留共享编库缓存；清本款一次性中间产物，不改变显式清缓存/删除语义。
        game = self.game(packages=[self.package()]); self.catalogue([game])
        keep, gone, other = self._build_leftovers(game["id"])
        keep.append(gone.pop())  # shared AIR cache is retained only on automatic success cleanup
        # 多平台共用阶段：自动清理保留产物根现行版本（链接与它指向的版本），旧版本照删；显式清缓存 / 删除仍整个删（下两条）
        roots = self.c.library / "iPhone" / game["id"] / "roots"; low = game["id"].lower()
        keep.append(gone.pop(0))   # roots/t<程序号>@现行/x
        new = roots / f"t{low}x@gk-new" / "y"; new.parent.mkdir(); new.write_text("x"); keep.append(new)
        (roots / f"t{low}x@现行").symlink_to(new.parent.name)   # 现行是链接：它指向的版本保留
        old = roots / f"t{low}@gk-old" / "z"; old.parent.mkdir(); old.write_text("x"); gone.append(old)
        library.prune_build(self.c, game["id"])
        self.assertEqual([f for f in keep + other if not f.exists()], [])
        self.assertEqual([f for f in gone if f.exists()], [])

    def test_prune_build_keeps_windows_work_root(self):
        # #32：Windows 产物根在 work/ 下。手算期望：现行链接及指向、.romfs_store、direct*、pkg/product/main.npdm 留下；旧版本、其他中间产物、product/pkg（交付件已拷走）删掉；链接只删自身
        game = self.game(packages=[self.package()]); self.catalogue([game])
        work = self.c.library / "iPhone" / game["id"] / "work"
        def f(rel):
            q = work / rel; q.parent.mkdir(parents=True, exist_ok=True); q.write_text("x"); return q
        keep = [f("roots/k@new/y"), f("roots/.romfs_store/b"), f("direct/lib"), f("direct_p1/lib"), f("pkg/product/main.npdm")]
        (work / "roots/k@现行").symlink_to("k@new")
        gone = [f("roots/k@old/z"), f("dcache/a"), f("pkg/tmp/b"), f("pkg/product/pkg/x.ipa")]
        outside = self.c.library / "outside" / "z"; outside.parent.mkdir(parents=True); outside.write_text("x")
        (work / "pkg/link").symlink_to(outside.parent)   # 指向外部的链接：只删链接，指向的文件在
        with patch.object(library, "_windows_layout", return_value=True):
            (self.c.library / "iPhone" / game["id"] / "a.ipa").write_bytes(b"ipa")   # 老玩家：有 IPA 没容器件
            library.prune_build(self.c, game["id"])
        self.assertEqual([q for q in keep if not q.exists()], [])
        self.assertEqual([q for q in gone if q.exists()], [])
        self.assertTrue((work / "roots/k@现行").is_symlink())
        self.assertFalse(os.path.lexists(work / "pkg/link"))
        self.assertTrue(outside.exists())

    def test_clean_cache_also_prunes_iphone_intermediates(self):
        game = self.game(packages=[self.package()]); self.catalogue([game])
        keep, gone, other = self._build_leftovers(game["id"])
        self.c.r.update(gameId=game["id"], kind="cache")
        library.clean(self.c)
        self.assertEqual([f for f in gone if f.exists()], [])
        self.assertTrue(all(f.exists() for f in keep + other))

    def test_delete_removes_outputs_caches_and_entry_keeps_saves_and_sources(self):
        # 期望：删除这款游戏 = IPA / 资源包 / 中间产物 / Mac 构建缓存 / 本款缓存全删、清单里没有这一条；存档与托管原始文件保留，别的款不动
        package = self.package(); game = self.game(packages=[package]); self.catalogue([game])
        keep, gone, other = self._build_leftovers(game["id"])
        save = self.c.library / "saves" / game["id"] / "s.dat"; save.parent.mkdir(parents=True); save.write_text("keep")
        mac = self.c.library / "cache" / game["id"] / "root"; mac.mkdir(parents=True)
        self.c.r["gameId"] = game["id"]
        library.delete(self.c)
        self.assertEqual([f for f in keep + gone if f.exists()], [])
        self.assertFalse(mac.exists())
        self.assertEqual([g["id"] for g in self.c.catalog()["games"]], [])
        self.assertEqual(save.read_text(), "keep")
        self.assertTrue((self.c.library / package["path"]).exists())
        self.assertTrue(all(f.exists() for f in other))

    def test_delete_waits_for_execution_lock_held_by_cancelled_task(self):
        # 期望：执行锁被（刚被取消、还没退出的）任务占着 3 秒 → 删除不立刻失败，等锁放开后照样删掉（10-26 Spare-host：导入后自动排的 Mac 准备占锁，删除直接报「另一个任务正在使用游戏库」）
        import subprocess, sys, time
        game = self.game(packages=[self.package()]); self.catalogue([game])
        self.c.require_library()
        lockf = self.c.library / ".execution.lock"
        holder = subprocess.Popen([sys.executable, "-c", "import sys,time; sys.path.insert(0,sys.argv[2]); import filelock; f=open(sys.argv[1],'a'); filelock.try_lock(f); print('ok',flush=True); time.sleep(3)", str(lockf), str(JOBS)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "ok")
        t0 = time.time(); self.c.r["gameId"] = game["id"]
        library.delete(self.c)
        holder.wait()
        self.assertGreaterEqual(time.time() - t0, 2.5)
        self.assertEqual(self.c.catalog()["games"], [])

    def test_direct_small_batch_progress_replaces_finished_translation(self):
        progress = self.root / "progress.tsv"
        progress.write_text("1\t直连库·翻译着色器\t30\t30\n2\t直连库·已生成库文件\t1\t3\n", encoding="utf-8")
        with patch.object(self.c, "emit") as emit:
            pos = self.c.stage_progress(progress, 0)
        event = emit.call_args.kwargs
        self.assertEqual((event["done"], event["total"]), (1, 3))
        self.assertEqual([c["name"] for c in event["counters"]], ["直连库·已生成库文件"])
        with progress.open("a", encoding="utf-8") as f:
            f.write("3\t直连库·已生成库文件\t3\t3\n4\t直连库·整份出库与收尾\t0\t2\n")
        with patch.object(self.c, "emit") as emit:
            self.c.stage_progress(progress, pos)
        event = emit.call_args.kwargs
        self.assertEqual((event["done"], event["total"]), (0, 2))
        self.assertEqual([c["name"] for c in event["counters"]], ["直连库·整份出库与收尾"])

    def test_protocol_has_ordered_events_and_measured_counters(self):
        self.c.begin("phase", 2, 5)
        self.c.emit(done=5, total=10, unit="files")
        self.c.finish("completed", "done", {"outputPath": "/result"})
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertIsNone(events[1]["totalUnits"])
        self.assertEqual(events[-2]["doneUnits"], 5)
        self.assertEqual(events[-1]["state"], "succeeded")
        self.assertEqual(events[-1]["artifactRef"], "/result")
        self.assertEqual(json.loads(self.c.jobpath.read_text())["state"], "completed")

    def test_finish_records_ipa_device_and_pushed_files_for_relaunch(self):
        # 启动器重开后完成卡从任务记录读 IPA（11-01：原先只在结果事件里，重开后画面模式「未知」）
        self.c.finish("completed", "done", {"outputPath": "/out", "ipa": "/out/x.ipa", "device": "测试设备1", "packFiles": 3})
        job = json.loads(self.c.jobpath.read_text())
        self.assertEqual((job["ipaPath"], job["device"], job["packFiles"]), ("/out/x.ipa", "测试设备1", 3))

    def test_subprocess_preserves_spaces_and_reports_real_failure_exit(self):
        log = self.root / "log with spaces.log"
        self.c.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "a b;$(ignored)"], log)
        self.assertIn("a b;$(ignored)", log.read_text())
        with self.assertRaises(Failure):
            self.c.run([sys.executable, "-c", "raise SystemExit(17)"], log)
        self.assertEqual(self.c.job["diagnostics"][-1]["exitCode"], 17)

    def test_cancel_stops_a_silent_process_group(self):
        timer = threading.Timer(.4, self.c.stop)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                self.c.run([sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"])
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 8)
        self.assertFalse(self.c.children)

    def test_pull_diagnostics_accepts_oneclick_s2arm_bundle(self):
        import diag
        bundle = "s2arm.abcde12345.0100aabbccdd0000"
        devices = {"result": {"devices": [{"identifier": "synthetic-device",
                   "hardwareProperties": {"platform": "iOS", "productType": "iPhoneSynthetic"},
                   "connectionProperties": {"tunnelState": "connected"}}]}}
        def fake(context, *args, timeout=120):
            if args[:2] == ("list", "devices"):
                return 0, devices, ""
            if args[:3] == ("device", "info", "apps"):
                return 0, {"result": {"apps": [{"bundleIdentifier": bundle, "name": "Synthetic"}]}}, ""
            self.assertEqual(args[args.index("--domain-identifier") + 1], bundle)
            if args[args.index("--source") + 1] == "Documents/host.log":
                Path(args[args.index("--destination") + 1]).write_text("synthetic log")
                return 0, {}, ""
            return 1, {}, ""
        with patch.object(diag, "devicectl", side_effect=fake):
            result = diag.pull(self.c)
        self.assertEqual([app["bundle"] for app in result["apps"]], [bundle])
        self.assertEqual(result["apps"][0]["files"], 1)

    def test_context_redact_keeps_ui_limit_and_can_export_complete_text(self):
        text = str(self.c.library) + "/BEGIN " + "x" * 4000 + " contact=a=b@example.invalid END"
        complete = self.c.redact(text, limit=None)
        self.assertTrue(complete.startswith("[LIBRARY]/BEGIN "))
        self.assertTrue(complete.endswith("contact=[EMAIL] END"))
        self.assertEqual(self.c.redact(text), complete[-3000:])
        self.assertGreater(len(complete), 4000)

    def test_diagnostic_manifest_and_every_member_are_redacted_without_truncation(self):
        import platform
        logs = self.c.private / "logs"; logs.mkdir(exist_ok=True)
        sensitive = ('contact=synth@example.invalid; DEVELOPMENT_TEAM = ABCDE12345; '
                     's2arm.abcde12345.0100aabbccdd0000; 0000abcd-0123456789abcdef; '
                     'device 0123456789abcdef0123456789abcdef01234567; '
                     '"合成甲的iPhone"; /Users/Synthetic/account')
        # Invalid UTF-8 must not bypass text redaction, and long data must not
        # lose its prefix through Context.redact's UI-only 3000-character cap.
        (logs / "synth@example.invalid.log").write_bytes(("BEGIN " + "x" * 4000 + "\n" + sensitive + "\nEND").encode() + b"\xff")
        destination = self.root / "diagnostics.zip"; self.c.r["path"] = str(destination)
        def probe(args, **kwargs):
            value = "x" * 4000 + "\n" + sensitive if args[-1] == "hw.model" else "Xcode 27.1\nBuild version SYNTH"
            return subprocess.CompletedProcess(args, 0, value, "")
        with patch.object(platform, "mac_ver", return_value=("27.1", (), "")), patch.object(subprocess, "run", side_effect=probe):
            library.diagnostics(self.c)
        with zipfile.ZipFile(destination) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["files"], sorted(archive.namelist()))
            self.assertEqual(manifest["macos_version"], "27.1")
            self.assertGreater(len(manifest["mac_model"]), 4000)
            self.assertEqual(manifest["xcode_version"], "Xcode 27.1\nBuild version SYNTH")
            self.assertIn("error", manifest["launcher_version"])
            log = archive.read("logs/[EMAIL].log").decode()
            self.assertTrue(log.startswith("BEGIN ")); self.assertGreater(len(log), 4000)
            self.assertTrue(log.endswith("END�"))
            text = "\n".join(archive.namelist()) + "\n" + "\n".join(archive.read(n).decode() for n in archive.namelist())
            for secret in ("/Users/", "synth@example.invalid", "ABCDE12345", "abcde12345", "0000abcd-0123456789abcdef",
                           "0123456789abcdef0123456789abcdef01234567", "合成甲"):
                self.assertNotIn(secret, text)
            self.assertIn("0100aabbccdd0000", text)

    def test_diagnostic_uuid_task_members_are_unique_and_manifest_matches(self):
        tasks = ("11111111-2222-4333-8444-555555555555",
                 "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        logs = self.c.private / "logs"
        logs.mkdir(exist_ok=True)
        expected = set()
        for task in tasks:
            (self.c.private / "jobs" / (task + ".json")).write_text('{"state":"done"}')
            (logs / (task + "-game.log")).write_text("serial=" + task)
            folder = logs / task
            folder.mkdir()
            (folder / "x.log").write_text("synthetic nested log")
            # Preserving the generated task prefix must not preserve a private suffix.
            (logs / (task + "-synth@example.invalid.log")).write_text("synthetic log")
            expected.update(("jobs/" + task + ".txt", "logs/" + task + "-game.log",
                             "logs/" + task + "/x.log", "logs/" + task + "[EMAIL].log"))
        destination = self.root / "diagnostics.zip"
        self.c.r["path"] = str(destination)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "synthetic", "")):
            library.diagnostics(self.c)
        with zipfile.ZipFile(destination) as archive:
            members = archive.namelist()
            self.assertEqual(len(members), len(set(members)))
            self.assertTrue(expected.issubset(set(members)))
            for task in tasks:
                self.assertEqual(archive.read("logs/" + task + "-game.log").decode(), "serial=[REDACTED]")
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["files"], sorted(members))
            self.assertNotIn("synth@example.invalid", "\n".join(members) + archive.read("manifest.json").decode())

    def test_diagnostic_manifest_probe_errors_and_redacted_name_collision(self):
        import platform
        destination = self.root / "diagnostics.zip"; self.c.r["path"] = str(destination)
        for result in (subprocess.CompletedProcess([], 7, "", "synthetic error"),
                       subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.TimeoutExpired([], 10), OSError("synthetic unavailable")):
            effect = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=str(result)), patch.object(platform, "mac_ver", return_value=("", (), "")), patch.object(subprocess, "run", **effect):
                library.diagnostics(self.c)
            with zipfile.ZipFile(destination) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                for key in ("macos_version", "xcode_version", "mac_model"):
                    self.assertIn("error", manifest[key])
        previous = destination.read_bytes()
        logs = self.c.private / "logs"; logs.mkdir(exist_ok=True)
        for name in ("one@example.invalid.log", "two@example.invalid.log"):
            (logs / name).write_text("synthetic log")
        with self.assertRaisesRegex(Failure, "文件名重复"):
            library.diagnostics(self.c)
        self.assertEqual(destination.read_bytes(), previous)

    def test_diagnostic_export_never_contains_keys_or_player_files(self):
        self.c.keys.parent.mkdir(parents=True)
        self.c.keys.write_text("header_key = " + "a" * 64)
        log = self.c.private / "logs" / "tool.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("token=verysecret " + str(self.c.private) + " " + "a" * 64)
        destination = self.root / "diagnostics.zip"
        self.c.r["path"] = str(destination)
        library.diagnostics(self.c)
        with zipfile.ZipFile(destination) as z:
            self.assertFalse(any("keys" in name or "sources" in name or "saves" in name for name in z.namelist()))
            text = "\n".join(z.read(name).decode() for name in z.namelist())
            self.assertNotIn("verysecret", text)
            self.assertNotIn("a" * 64, text)
            self.assertNotIn(str(self.c.private), text)

    def test_diagnostic_fd_boundaries_and_directory_replacement(self):
        import unittest.mock as um
        destination = self.root / "diagnostics.zip"; destination.write_bytes(b"old target")
        self.c.r["path"] = str(destination)
        external = self.root / "external"; external.mkdir()
        (external / "outside.log").write_text("OUTSIDE_BOUNDARY_SENTINEL")
        (external / "outside.json").write_text("OUTSIDE_BOUNDARY_SENTINEL")
        (self.c.private / "logs").mkdir()
        # Static root links are rejected before begin can persist a job through them.
        for name in ("jobs", "logs"):
            root = self.c.private / name; saved = self.c.private / (name + "-saved")
            root.rename(saved); root.symlink_to(external, target_is_directory=True)
            with um.patch.object(self.c, "begin") as begin, self.assertRaises(Failure):
                library.diagnostics(self.c)
            begin.assert_not_called()
            self.assertEqual(sorted(p.name for p in external.iterdir()), ["outside.json", "outside.log"])
            root.unlink(); saved.rename(root)
            self.assertEqual(destination.read_bytes(), b"old target")
        jobs, logs = self.c.private / "jobs", self.c.private / "logs"
        (jobs / "original.json").write_text('{"detail":"ORIGINAL_JOB"}')
        (logs / "nested").mkdir(); (logs / "nested/original.log").write_text("ORIGINAL_LOG")
        (jobs / "link.json").symlink_to(external / "outside.json")
        (logs / "link.log").symlink_to(external / "outside.log")
        (logs / "linked-directory").symlink_to(external, target_is_directory=True)
        os.mkfifo(jobs / "pipe.json"); os.mkfifo(logs / "pipe.log")
        roots = {p.stat().st_ino: p for p in (jobs, logs)}
        listed = os.listdir; replaced = []
        def replace_after_listing(fd):
            names = listed(fd)
            root = roots.get(os.fstat(fd).st_ino) if isinstance(fd, int) else None
            if root and root not in replaced:
                root.rename(root.with_name(root.name + "-saved"))
                root.symlink_to(external, target_is_directory=True)
                replaced.append(root)
            return names
        with um.patch.object(os, "listdir", side_effect=replace_after_listing):
            library.diagnostics(self.c)
        self.assertEqual(len(replaced), 2)
        with zipfile.ZipFile(destination) as archive:
            self.assertIn("ORIGINAL_JOB", archive.read("jobs/original.txt").decode())
            self.assertEqual(archive.read("logs/nested/original.log"), b"ORIGINAL_LOG")
            self.assertFalse(any("link" in name or "pipe" in name or "outside" in name for name in archive.namelist()))
            self.assertNotIn("OUTSIDE_BOUNDARY_SENTINEL", "".join(archive.read(name).decode() for name in archive.namelist()))

    def test_diagnostic_log_keeps_head_heartbeats_and_tail(self):
        import io
        short = b"a" * 32000
        self.assertEqual(library.diagnostic_log_excerpt(io.BytesIO(short), len(short)), short)
        beat = "[心跳] 帧 {} 热状态 0\n".format
        head = b"HEAD_MARK\n" + b"h" * 16000
        noise = b"NOISE_LINE padding padding\n" * 80000   # 约 2.2 MB，心跳行跨 1 MiB 分块
        body = head + noise + beat(1).encode() + noise + beat(2).encode() + noise + b"t" * 16000 + b"\nTAIL_MARK\n"
        out = library.diagnostic_log_excerpt(io.BytesIO(body), len(body))
        self.assertTrue(out.startswith(b"HEAD_MARK")); self.assertTrue(out.endswith(b"TAIL_MARK\n"))
        self.assertIn(beat(1).strip().encode(), out); self.assertIn(beat(2).strip().encode(), out)
        self.assertLess(out.count(b"NOISE_LINE"), 1200)   # 只有开头 / 结尾 16 KB 内可能带到
        # 心跳太多：只留最新的，总量有界
        many = b"".join(beat(i).encode() for i in range(20000))
        body = b"h" * 16000 + many + b"t" * 16000
        out = library.diagnostic_log_excerpt(io.BytesIO(body), len(body))
        self.assertIn(beat(19999).strip().encode(), out); self.assertNotIn(beat(0).strip().encode(), out)
        self.assertLessEqual(len(out), 16000 * 2 + 64000 + 200)

    def customize_game(self, **fields):
        gid = "0100000000010000"
        cat = self.c.catalog()
        if not any(g["id"] == gid for g in cat["games"]):
            atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id=gid, name="原名", display="", packages=[], hidden=False)]))
        request = dict(self.request, taskId=str(uuid.uuid4()), operation="customize", gameId=gid, **fields)
        return library.customize(Context(request)), gid

    def test_customize_name_and_cover_are_display_only_and_restorable(self):
        png = self.root / "my.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        result, gid = self.customize_game(name="  我的游戏  ", coverPath=str(png))
        self.assertEqual(result["name"], "我的游戏")
        folder = self.c.library / "games" / gid
        covers = list(folder.glob("cover.custom.*.jpg"))
        self.assertEqual(len(covers), 1); self.assertEqual(covers[0].read_bytes(), png.read_bytes())
        self.assertEqual(self.c.catalog()["games"][0]["name"], "原名")   # 只改显示，不改登记名
        rendered = library.game_custom(self.c, gid)
        self.assertEqual((rendered["name"], rendered["cover"]), ("我的游戏", covers[0]))
        # 只改名字：封面不动
        self.customize_game(name="第二个名字")
        self.assertEqual(list(folder.glob("cover.custom.*.jpg")), covers)
        # 换图：文件名变（两端按路径缓存），旧图删掉
        jpg = self.root / "my.jpg"; jpg.write_bytes(b"\xff\xd8\xff" + b"y" * 100)
        self.customize_game(coverPath=str(jpg))
        after = list(folder.glob("cover.custom.*.jpg"))
        self.assertEqual(len(after), 1); self.assertNotEqual(after, covers)
        self.assertEqual(library.game_custom(self.c, gid)["name"], "第二个名字")
        # 恢复默认
        self.customize_game(name="", coverPath="")
        self.assertEqual(list(folder.glob("cover.custom.*.jpg")), [])
        self.assertFalse((folder / "custom.json").exists())
        self.assertEqual(library.game_custom(self.c, gid), dict(name="", cover=None))

    def test_customize_rejects_bad_input_and_keeps_previous(self):
        self.customize_game(name="好名字")
        bad = self.root / "bad.txt"; bad.write_bytes(b"not an image")
        huge = self.root / "huge.png"; huge.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (16 << 20))
        for fields in (dict(name="x" * 65), dict(name="换\n行"), dict(name=3), dict(coverPath=str(bad)), dict(coverPath=str(huge)), dict(coverPath=str(self.root / "none.png"))):
            with self.assertRaises(Failure, msg=str(list(fields))):
                self.customize_game(**fields)
        custom = library.game_custom(self.c, "0100000000010000")
        self.assertEqual((custom["name"], custom["cover"]), ("好名字", None))

    def test_container_custom_lists_custom_and_default_entries(self):
        import ipa
        png = self.root / "my.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x")
        _, gid = self.customize_game(name="新名", coverPath=str(png))
        item = ipa.container_custom(self.c)[gid]
        self.assertEqual((item["custom"], item["name"]), (True, "新名"))
        self.assertIn("cover.custom.", item["cover"])
        self.customize_game(name="", coverPath="")
        self.assertEqual(ipa.container_custom(self.c)[gid], dict(custom=False, name="原名", cover=""))

    def test_ship_ext_applies_custom_and_restores_default(self):
        import importlib.util, json as j
        spec = importlib.util.spec_from_file_location("container_custom_under_test", ROOT / "pack/container_custom.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        app = self.root / "S2Arm.app"; game = app / "Games" / "A"; game.mkdir(parents=True); (game / "icon.jpg").write_bytes(b"old")
        cover = self.root / "c.jpg"; cover.write_bytes(b"custom")
        spec_file = self.root / "custom.json"
        entries = [dict(title_id="A", name="原名"), dict(title_id="B", name="别的款")]
        spec_file.write_text(j.dumps(dict(A=dict(custom=True, name="新名", cover=str(cover)))), encoding="utf-8")
        module.apply_custom(app, entries, str(spec_file), "Games")
        self.assertEqual(entries, [dict(title_id="A", name="新名", icon="Games/A/icon.jpg", customized=True), dict(title_id="B", name="别的款")])
        self.assertEqual((game / "icon.jpg").read_bytes(), b"custom")
        # 恢复默认：带着旧自定义的条目换回原名原图、去掉标记；没自定义过的条目不碰
        original = self.root / "o.jpg"; original.write_bytes(b"original")
        spec_file.write_text(j.dumps(dict(A=dict(custom=False, name="原名", cover=str(original)), B=dict(custom=False, name="库里的名字", cover=""))), encoding="utf-8")
        module.apply_custom(app, entries, str(spec_file), "Games")
        self.assertEqual(entries, [dict(title_id="A", name="原名", icon="Games/A/icon.jpg"), dict(title_id="B", name="别的款")])
        self.assertEqual((game / "icon.jpg").read_bytes(), b"original")

    def stopped_game(self, kit=True, ipa=True):
        gid = "0100000000010000"
        atomic(self.c.library / "catalog.json", dict(schema=1, games=[dict(id=gid, name="x", display="", packages=[], hidden=False)]))
        out = self.c.library / "iPhone" / gid; root = out / "roots" / "k@pkg"; root.mkdir(parents=True)
        (root / "main.npdm").write_bytes(b"n"); (root / "stage.log").write_text("阶段日志")
        (out / "roots" / "k@现行").symlink_to("k@pkg"); (self.c.library / "cache" / gid / "x").mkdir(parents=True)
        if ipa: (out / "a.ipa").write_bytes(b"ipa"); (out / "t资源包").mkdir()
        if kit: (out / library.CONTAINER_KIT).mkdir(); (out / library.CONTAINER_KIT / "main.npdm").write_bytes(b"old")
        release = self.c.library / "games" / gid / "releases"; (release / "good").mkdir(parents=True); (release / "bad.partial").mkdir()
        save = self.c.library / "saves" / gid / "s.bin"; save.parent.mkdir(parents=True); save.write_bytes(b"save")
        return gid, out, save

    def stop_context(self, gid):
        return Context(dict(self.request, taskId=str(uuid.uuid4()), operation="ipa", gameId=gid))

    def test_clean_after_stop_keeps_previous_delivery_and_logs(self):
        gid, out, save = self.stopped_game()
        c = self.stop_context(gid)
        library.clean_after_stop(c)
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(["a.ipa", "t资源包", library.CONTAINER_KIT]))
        self.assertFalse((self.c.library / "cache" / gid).exists())
        self.assertEqual((out / library.CONTAINER_KIT / "main.npdm").read_bytes(), b"old")
        self.assertEqual(save.read_bytes(), b"save")
        self.assertEqual((c.private / "logs" / (c.task + "-stages") / "stage.log").read_text(), "阶段日志")

    def test_clean_after_stop_removes_first_failed_export(self):
        gid, out, _ = self.stopped_game(kit=False, ipa=False)
        library.clean_after_stop(self.stop_context(gid))
        self.assertEqual(list(out.iterdir()), [])

    def test_clean_after_stop_keeps_only_source_of_old_install(self):
        gid, out, _ = self.stopped_game(kit=False)
        library.clean_after_stop(self.stop_context(gid))
        self.assertTrue((out / "roots" / "k@pkg" / "main.npdm").is_file())   # 老玩家：没有容器件时产物根是装机的唯一来源

    def test_sweep_skips_while_export_runs_and_cleans_when_idle(self):
        gid, out, _ = self.stopped_game()
        c = self.stop_context(gid)
        with c.lock("execution"):
            library.sweep_residue(c)
        self.assertTrue((out / "roots").exists())
        library.sweep_residue(c)
        self.assertFalse((out / "roots").exists())
        release = self.c.library / "games" / gid / "releases"
        self.assertEqual([p.name for p in release.iterdir()], ["good"])

    def test_diagnostic_bounded_reads_and_atomic_publication(self):
        import unittest.mock as um
        logs = self.c.private / "logs"; logs.mkdir()
        log = logs / "tail.log"; log.write_bytes(b"x" * 40000 + b"tail")
        destination = self.root / "diagnostics.zip"; destination.write_bytes(b"old target")
        self.c.r["path"] = str(destination)
        marker = self.root / "outside-marker"; marker.write_bytes(b"unchanged marker")
        predictable = destination.with_name("." + destination.name + "." + self.c.task + ".partial")
        predictable.symlink_to(marker)
        fdopen, make_zip = os.fdopen, zipfile.ZipFile
        reads, outputs = [], []
        class ReadProbe:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def read(self, size=-1):
                reads.append((os.fstat(self.stream.fileno()).st_ino, self.stream.tell(), size))
                return self.stream.read(size)
        @contextlib.contextmanager
        def tracked_open(fd, mode, *args, **kwargs):
            with fdopen(fd, mode, *args, **kwargs) as stream:
                yield ReadProbe(stream) if mode == "rb" else stream
        def archive(output, *args, **kwargs):
            self.assertEqual(os.fstat(output.fileno()).st_mode & 0o777, 0o600)
            outputs.append(output)
            return make_zip(output, *args, **kwargs)
        with um.patch.object(os, "fdopen", tracked_open), um.patch.object(zipfile, "ZipFile", side_effect=archive):
            library.diagnostics(self.c)
        self.assertTrue(all(output.closed for output in outputs))
        self.assertIn((log.stat().st_ino, log.stat().st_size - 16000, 16000), reads)
        self.assertTrue(all(size <= 1 << 20 or size == (4 << 20) + 1 for _, _, size in reads))
        self.assertEqual(marker.read_bytes(), b"unchanged marker")
        self.assertTrue(predictable.is_symlink()); self.assertFalse(destination.is_symlink())
        previous = destination.read_bytes()
        oversized = self.c.private / "jobs/oversized.json"
        with oversized.open("wb") as stream: stream.truncate((4 << 20) + 1)
        with self.assertRaisesRegex(Failure, "4 MiB"):
            library.diagnostics(self.c)
        oversized.unlink()
        with um.patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("synthetic write failure")), self.assertRaises(Failure):
            library.diagnostics(self.c)
        with um.patch.object(self.c, "check", side_effect=Cancelled()), self.assertRaises(Cancelled):
            library.diagnostics(self.c)
        self.assertEqual(destination.read_bytes(), previous)
        self.assertEqual(marker.read_bytes(), b"unchanged marker")
        self.assertEqual(list(self.root.glob("." + destination.name + ".*.partial")), [predictable])

    def test_cli_setup_and_unknown_operation_use_real_worker_protocol(self):
        request = self.request | dict(taskId="cli-test", operation="not-an-operation")
        p = self.root / "request.json"
        atomic(p, request)
        result = subprocess.run([sys.executable, "-I", "-B", str(JOBS / "main.py"), "--request", str(p)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        event = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(event["jobId"], "cli-test")
        self.assertEqual(event["state"], "failed")
        p.chmod(0o644)
        result = subprocess.run([sys.executable, "-I", "-B", str(JOBS / "main.py"), "--request", str(p)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invalid_keys_preserve_the_previous_player_key(self):
        self.c.keys.parent.mkdir(parents=True)
        self.c.keys.write_text("header_key = " + "a" * 64)
        invalid = self.root / "invalid.keys"
        invalid.write_text("header_key = broken")
        self.c.r["path"] = str(invalid)
        with self.assertRaises(Failure):
            library.keys(self.c)
        self.assertIn("a" * 64, self.c.keys.read_text())

    def test_mapping_rejects_malformed_native_values_and_duplicate_axes(self):
        config = dict(schema=1, keyboard={"A": {"code":14}}, mouse={}, gamepad={}, axes={}, deadzone=.15,
                      mouseOptions=dict(mode="camera", sensitivityX=1, sensitivityY=1, invertX=False, invertY=False, recenter=True))
        self.c.r["config"] = config
        library.save_input(self.c)
        config["keyboard"]["A"]["code"] = "fourteen"
        with self.assertRaises(Failure):
            library.save_input(self.c)
        config["keyboard"] = {}
        config["axes"] = {"LX": {"source":"leftX"}, "LY": {"source":"leftX", "invert":True}}
        with self.assertRaises(Failure):
            library.save_input(self.c)

    def test_frontend_toolchain_is_child_only_and_logged(self):
        previous = os.environ.get("S2A_DEVELOPER_DIR")
        chosen = str(self.root / "chosen Xcode.app/Contents/Developer")
        self.c.r["developerDir"] = chosen
        log = self.root / "toolchain.log"
        self.c.run([sys.executable, "-c", "import os; print('child:' + os.environ['DEVELOPER_DIR'])"], log)
        text = log.read_text()
        self.assertIn("S2Arm frontend toolchain: DEVELOPER_DIR=" + chosen, text)
        self.assertIn("child:" + chosen, text)
        self.assertEqual(os.environ.get("S2A_DEVELOPER_DIR"), previous)

    def test_seed_stage_keeps_dlc_and_does_not_modify_input(self):
        source = self.root / "raw"
        for name in ("exefs/main", "romfs/data", "aoc/1/data", "ctl/control.nacp", "aoc.tsv"):
            p = source / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("seed-stage fixture only")
        env = self.c.env() | dict(S2A_ROOTS=str(self.root / "roots"), S2A_ROOTS_SHARED=str(self.root / "shared"))
        self.c.run(["bash", ROOT / "pack/seed_root.sh", "unit", "seed", source], env=env)
        current = (self.root / "roots/unit@现行").resolve()
        self.assertEqual((current / "aoc/1/data").read_text(), "seed-stage fixture only")
        self.assertEqual((current / "aoc.tsv").read_bytes(), (source / "aoc.tsv").read_bytes())
        (current / "aoc/1/data").write_text("changed clone")
        self.assertEqual((source / "aoc/1/data").read_text(), "seed-stage fixture only")



class WaitReadableTests(unittest.TestCase):
    """开工前读原件（library.wait_readable，10-31 出包管线深修 4.5）：读得到不打扰；系统拒绝说清去哪打开；读卡住时界面上说在等什么、等了多久"""
    def ctx(self):
        from types import SimpleNamespace
        msgs = []
        return SimpleNamespace(emit=lambda kind="progress", message="", **k: msgs.append(message), check=lambda: None), msgs

    def test_readable_passes_silently(self):
        import library
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "base.nsp"; p.write_bytes(b"x" * 5000)
            c, msgs = self.ctx(); library.wait_readable(c, [p], poll=0.1)
            self.assertEqual(msgs, [])

    def test_permission_denied_says_where_to_allow(self):
        import library
        from errors import Failure
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "base.nsp"; p.write_bytes(b"x"); p.chmod(0)
            try:
                c, _ = self.ctx()
                with self.assertRaises(Failure) as e: library.wait_readable(c, [p], poll=0.1)
                self.assertEqual(e.exception.code, "input_permission")
                self.assertIn("隐私与安全性", str(e.exception))
            finally: p.chmod(0o600)

    def test_blocked_read_tells_what_it_waits_for(self):
        import builtins, library, time as _t
        from unittest.mock import patch
        real_open = builtins.open
        def slow_open(*a, **k):   # 读像被授权框挡住一样慢 0.35 秒，之后照常
            _t.sleep(0.35); return real_open(*a, **k)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "base.nsp"; p.write_bytes(b"x")
            c, msgs = self.ctx()
            with patch.object(builtins, "open", slow_open): library.wait_readable(c, [p], poll=0.1)
            self.assertTrue(msgs and all("在等系统的文件访问授权：读 base.nsp" in m for m in msgs), msgs)


if __name__ == "__main__":
    unittest.main()
