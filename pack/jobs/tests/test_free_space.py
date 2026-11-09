# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""启动器「释放空间」+ 游戏库位置预检（11-07）：云盘同步目录、卷格式（Mac / Windows 同一套判法）、按部件统计与删除。
全为合成路径 / 合成文件，期望值按规则手写。"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from jobcase import JobCase, ROOT
from core import Context, Failure
import filelock
import libloc
import library

sys.path.insert(0, str(ROOT))
from pack.contracts.validate import validate, ContractError

MIB = 1 << 20
SLACK = 64 * 1024   # 目录元数据余量（APFS 目录 0 块）
GID = "0100F2C0115B6000"
OTHER = "0100000000000001"


def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))   # 随机内容：不让文件系统压缩 / 稀疏
    return path


class CloudFolder(unittest.TestCase):
    def test_mac(self):
        home = "/Users/player"
        cases = {
            "/Users/player/Library/Mobile Documents/com~apple~CloudDocs/S2Arm 游戏库": "iCloud 云盘",
            "/Users/player/Library/Mobile Documents": "iCloud 云盘",
            "/Users/player/Library/CloudStorage/OneDrive-Personal/库": "云盘（文件提供程序）",
            "/Users/player/Library/CloudStorage/Dropbox/库": "云盘（文件提供程序）",
            "/Users/player/Library/Mobile Documents2/库": None,   # 只是前缀相同，不是同一个目录
            "/Users/player/Library/Application Support/S2Arm/library": None,   # 缺省位置
            "/Volumes/Example/游戏库": None,
            "/Users/player/Documents/S2Arm": None,   # 没给 exists：不判「桌面与文稿」
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(libloc.cloud_folder(path, system="Darwin", home=home), want)

    def test_mac_desktop_documents_only_when_synced(self):
        home = "/Users/player"
        marker = "/Users/player/Library/Mobile Documents/com~apple~CloudDocs/Desktop"
        synced, local = (lambda p: p == marker), (lambda p: False)
        for path in ("/Users/player/Documents/S2Arm", "/Users/player/Desktop/库"):
            with self.subTest(path=path):
                self.assertEqual(libloc.cloud_folder(path, system="Darwin", home=home, exists=synced), "iCloud 云盘（桌面与文稿同步）")
                self.assertIsNone(libloc.cloud_folder(path, system="Darwin", home=home, exists=local))
        self.assertIsNone(libloc.cloud_folder("/Users/player/Downloads/库", system="Darwin", home=home, exists=synced))

    def test_windows(self):
        home = "C:\\Users\\player"
        env = {"OneDrive": "C:\\Users\\player\\OneDrive", "OneDriveCommercial": "D:\\OneDrive - 公司"}
        cases = {
            "C:\\Users\\player\\OneDrive\\S2Arm": "OneDrive",
            "c:\\users\\player\\onedrive\\s2arm": "OneDrive",   # Windows 路径不分大小写
            "C:/Users/player/OneDrive/S2Arm": "OneDrive",   # 正斜杠也认
            "D:\\OneDrive - 公司\\库": "OneDrive",
            "C:\\Users\\player\\OneDriveX\\库": None,
            "C:\\Users\\player\\iCloudDrive\\库": "iCloud 云盘",
            "C:\\Users\\player\\Dropbox\\库": "Dropbox",
            "C:\\Users\\player\\Google Drive\\库": "Google 云端硬盘",
            "D:\\S2Arm\\游戏库": None,
            "C:\\Users\\player\\AppData\\Local\\S2Arm\\library": None,
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(libloc.cloud_folder(path, system="Windows", home=home, env=env), want)
        self.assertIsNone(libloc.cloud_folder("C:\\Users\\player\\OneDrive\\x", system="Windows", home=home, env={}))   # 没装 OneDrive：缺省目录名不算

    def test_format_ok(self):
        for fmt, mac, win in (("apfs", True, False), ("APFS", True, False), ("exfat", False, False), ("msdos", False, False),
                              ("ntfs", False, True), ("NTFS", False, True), ("refs", False, True), ("fat32", False, False), ("", True, True)):
            with self.subTest(fmt=fmt):
                self.assertEqual(libloc.format_ok(fmt, system="Darwin"), mac)
                self.assertEqual(libloc.format_ok(fmt, system="Windows"), win)

    def test_problems_codes(self):
        self.assertEqual(libloc.problems("/Volumes/Example/库", system="Darwin", home="/Users/player", fmt="apfs"), [])
        got = libloc.problems("/Users/player/Library/Mobile Documents/x", system="Darwin", home="/Users/player", fmt="exfat")
        self.assertEqual([p["code"] for p in got], ["library_in_cloud", "library_not_apfs"])
        self.assertIn("iCloud 云盘", got[0]["message"]); self.assertIn("EXFAT", got[1]["message"])
        got = libloc.problems("E:\\库", system="Windows", home="C:\\Users\\player", fmt="exfat")
        self.assertEqual([p["code"] for p in got], ["library_bad_format"]); self.assertIn("NTFS", got[0]["message"])


class LibraryGate(JobCase):
    def test_setup_rejects_cloud_before_writing(self):
        lib = self.root / "云盘里的库"; self.c.library = lib
        with patch.object(libloc, "cloud_folder", return_value="iCloud 云盘"), self.assertRaises(Failure) as e:
            library.setup(self.c)
        self.assertEqual(e.exception.code, "library_in_cloud"); self.assertIn("iCloud 云盘", str(e.exception))
        self.assertFalse(lib.exists())   # 拦在建目录之前

    def test_require_apfs_windows_wants_ntfs(self):
        with patch.object(library.os, "name", "nt"):
            for fmt in ("exfat", "fat32"):
                with self.subTest(fmt=fmt), patch.object(library, "volume_format", return_value=fmt), self.assertRaises(Failure) as e:
                    library.require_apfs("E:\\库")
                self.assertEqual(e.exception.code, "library_bad_format"); self.assertIn(fmt.upper(), str(e.exception))
            for fmt in ("ntfs", "refs", ""):
                with self.subTest(fmt=fmt), patch.object(library, "volume_format", return_value=fmt):
                    self.assertEqual(library.require_apfs("D:\\库"), fmt)

    def test_mac_preflight_reports_cloud(self):
        import toolchain
        seen = []
        def run(path):
            seen.clear()
            self.c.r["library"] = path
            with patch.object(library, "volume_format", return_value="apfs"), \
                 patch.object(self.c, "emit", lambda kind, *a, **kw: seen.append(kw.get("check")) if kind == "check" else None):
                try:
                    toolchain.preflight(self.c)
                except Exception:
                    pass   # 后面的工具探测与本测无关
            return next((x for x in seen if x and x["name"] == "游戏库所在位置"), None)
        row = run(str(Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/S2Arm"))
        self.assertIsNotNone(row)
        self.assertEqual((row["ok"], row["group"], row["action"], row["diagnostic"]["code"]), (False, "storage", "library", "library_in_cloud"))
        row = run("/Volumes/Example/游戏库")
        self.assertTrue(row["ok"])


class FreeSpace(JobCase):
    def setUp(self):
        super().setUp()
        games = [dict(id=g, name=n, packages=[]) for g, n in ((GID, "异度神剑3"), (OTHER, "别款"))]
        library.atomic(self.c.library / "catalog.json", dict(schema=1, games=games))
        L, P, W = self.c.library, self.c.private, self.c.work_root
        self.files = {
            "legacy": [write(L / "cache" / GID / "extract" / "romfs.bin", 1 * MIB), write(L / "iPhone" / GID / "roots" / "r@1" / "a.bin", 2 * MIB), write(L / "iPhone" / GID / "build.tmp", 1 * MIB)],
            "shaders": [write(W / "run" / "s2arm" / "cache" / "direct" / GID / "lib.bin", 2 * MIB),
                        write(W / "run" / "s2arm" / "cache" / "bake" / f"{GID.lower()}-x" / "b.bin", 1 * MIB)],
            "mac": [write(L / "games" / GID / "app.bin", 1 * MIB)],
            "ipa": [write(L / "iPhone" / GID / "异度神剑3 S2Arm.ipa", 3 * MIB), write(L / "iPhone" / GID / "异度神剑3 资源包" / "r.bin", 1 * MIB)],
            "saves": [write(L / "saves" / GID / "slot0.bin", 1 * MIB)],
            "logs": [write(L / "iPhone" / GID / "logs" / "build.log", 1 * MIB)],
        }
        self.mib = {"legacy": 4, "shaders": 3, "mac": 1, "ipa": 4, "saves": 1, "logs": 1}
        self.shared = [write(W / "cache" / "air" / "shared.bin", 1 * MIB)]   # 各款共用的编库缓存：不归任何一款
        self.other = [write(L / "cache" / OTHER / "x.bin", 1 * MIB), write(L / "iPhone" / OTHER / "o.ipa", 1 * MIB)]

    def context(self, operation, **extra):
        self.sequence += 1
        return Context(self.request | dict(taskId=f"free-{self.sequence}", operation=operation) | extra)

    def assert_mib(self, got, mib):
        self.assertGreaterEqual(got, mib * MIB); self.assertLess(got, mib * MIB + SLACK)

    def test_survey_lists_each_part(self):
        out = library.space_survey(self.context("spaceSurvey", gameId=GID))
        self.assertEqual(len(out["games"]), 1)
        g = out["games"][0]
        self.assertEqual((g["gameId"], g["name"]), (GID, "异度神剑3"))
        self.assertEqual([p["id"] for p in g["parts"]], ["legacy", "shaders", "mac", "ipa"])
        self.assertEqual([k["id"] for k in g["kept"]], ["saves", "logs"])
        for row in g["parts"] + g["kept"]:
            with self.subTest(part=row["id"]):
                self.assert_mib(row["bytes"], self.mib[row["id"]])
        self.assertTrue(all(p["title"] and p["rebuild"] for p in g["parts"]))
        everyone = library.space_survey(self.context("spaceSurvey"))
        self.assertEqual([g["gameId"] for g in everyone["games"]], [GID, OTHER])
        other = everyone["games"][1]
        self.assert_mib(sum(p["bytes"] for p in other["parts"] if p["id"] == "legacy"), 1)
        self.assert_mib(sum(p["bytes"] for p in other["parts"] if p["id"] == "ipa"), 1)
        with self.assertRaises(Failure):
            library.space_survey(self.context("spaceSurvey", gameId="0100000000000FFF"))

    def test_free_deletes_only_chosen_parts(self):
        out = library.space_free(self.context("spaceFree", gameId=GID, library=str(self.c.library), parts=["ipa", "shaders"]))
        self.assertEqual(out["gameId"], GID)
        self.assertEqual([p["id"] for p in out["parts"]], ["shaders", "ipa"])   # 按部件表顺序
        self.assert_mib(out["parts"][0]["bytes"], 3); self.assert_mib(out["parts"][1]["bytes"], 4)
        self.assertEqual(out["freedBytes"], sum(p["bytes"] for p in out["parts"]))
        for k, paths in self.files.items():
            for p in paths:
                with self.subTest(part=k, path=p.name):
                    self.assertEqual(p.exists(), k not in ("ipa", "shaders"))
        for p in self.shared + self.other:
            self.assertTrue(p.exists(), p)   # 共用缓存、别款都不动
        self.assertTrue((self.c.library / "iPhone" / GID / "logs" / "build.log").exists())

    def test_free_everything_keeps_saves_and_logs(self):
        ids = [k for k, _, _ in library.SPACE_PARTS]
        out = library.space_free(self.context("spaceFree", gameId=GID, library=str(self.c.library), parts=ids))
        self.assert_mib(out["freedBytes"], sum(self.mib[k] for k in ids))
        for k, paths in self.files.items():
            for p in paths:
                self.assertEqual(p.exists(), k in ("saves", "logs"), p)

    def test_free_rejects_bad_parts_and_busy_library(self):
        for bad in (["saves"], ["logs"], ["ipa", "games"]):
            with self.subTest(parts=bad), self.assertRaises(Failure):
                library.space_free(self.context("spaceFree", gameId=GID, library=str(self.c.library), parts=bad))
        with (self.c.library / ".execution.lock").open("a") as f:   # 另一个任务在跑
            filelock.try_lock(f)
            try:
                with self.assertRaises(Failure) as e:
                    library.space_free(self.context("spaceFree", gameId=GID, library=str(self.c.library), parts=["ipa"]))
                self.assertIn("另一个任务", str(e.exception))
            finally:
                filelock.unlock(f)
        self.assertTrue(all(p.exists() for p in self.files["ipa"]))

    def test_request_contract(self):
        base = dict(schema=1, contract="s2a.job-request.v1", privateRoot="/p", library="/l", gameId=GID)
        validate("request", base | dict(operation="spaceFree", parts=["ipa"]))
        validate("request", base | dict(operation="spaceSurvey"))
        for bad in (dict(operation="spaceFree"), dict(operation="spaceFree", parts=[]), dict(operation="spaceFree", parts=["saves"])):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                validate("request", base | bad)

    def test_worker_end_to_end(self):
        # 真 worker 协议（main.py --request）：两项作业已登记，结果走终态事件
        result, event = self.worker("spaceSurvey", gameId=GID)
        self.assertEqual((result.returncode, event["state"]), (0, "succeeded"), result.stderr)
        self.assertEqual(event["result"]["games"][0]["gameId"], GID)
        result, event = self.worker("spaceFree", gameId=GID, parts=["legacy"])
        self.assertEqual((result.returncode, event["state"]), (0, "succeeded"), result.stderr)
        self.assert_mib(event["result"]["freedBytes"], 4)
        self.assertFalse(self.files["legacy"][0].exists()); self.assertTrue(self.files["ipa"][0].exists())


if __name__ == "__main__":
    unittest.main()
