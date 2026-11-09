import tempfile
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 S2Arm contributors
"""出包空间按实测（11-07）：实测记账口径、预算三档依据、拦截文案、「仍然继续」放行。全为合成文件，期望值手算。"""
import collections
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from jobcase import JobCase, ROOT
from core import Context, Failure
import library

sys.path.insert(0, str(ROOT))
from pack.contracts.validate import validate, ContractError

MIB = 1 << 20
GID = "01007EF00011E000"
DU = collections.namedtuple("du", "total used free")


def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size))   # 随机内容：不让文件系统压缩 / 稀疏，1 MiB 就占 1 MiB 块
    return path


class SpaceMeasured(JobCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {})   # 只有直连（11-07）；实测账只认直连行
        env.start(); self.addCleanup(env.stop)

    def context(self, operation="ipa", **extra):
        self.sequence += 1
        return Context(self.request | dict(taskId=f"space-{self.sequence}", operation=operation, gameId=GID) | extra)

    def game(self, size, gid=GID, name="塞尔达传说 旷野之息"):
        pkg = dict(titleId=gid, baseId=gid, type="base", version=0, path="sources/x.nsp", sha256=gid.lower() * 4, size=size, guestIsa="aarch64")
        return dict(id=gid, name=name, packages=[pkg])

    def ledger(self, *rows):
        # time gameId target direct inputBytes footprintBytes name
        with (self.c.library / library.SPACE_LEDGER).open("a", encoding="utf-8") as f:
            for r in rows:
                f.write("\t".join(map(str, list(r) + [""] * (7 - len(r)) + [library.SPACE_LEDGER_FORMAT])) + "\n")

    def budget(self, c, game, free=45 * 10**9):
        with patch("shutil.disk_usage", return_value=DU(0, 0, free)):
            return library.space_budget(c, game)["volumes"][0]

    # —— 实测记账口径 ——
    def test_disk_bytes_skips_named_dirs_only_under_given_roots(self):
        root = Path(self.t if hasattr(self, "t") else tempfile.mkdtemp())
        keep = root / "cache" / "romfs"; clone = root / "iphone" / "romfs"
        keep.mkdir(parents=True); clone.mkdir(parents=True)
        (keep / "a").write_bytes(b"x" * MIB); (clone / "a").write_bytes(b"x" * MIB)
        dev = root.stat().st_dev
        both = library.disk_bytes([root], dev)
        one = library.disk_bytes([root], dev, skip_names=library.CLONED_DIRS, skip_under=[str(root / "iphone")])
        self.assertGreaterEqual(both - one, MIB)       # 克隆那份不算
        self.assertGreaterEqual(one, MIB)              # 解包那份还算

    def test_disk_bytes_counts_hardlink_once_skips_symlink_and_other_volume(self):
        root = self.root / "measure"
        a = write(root / "a.bin", MIB)
        base = library.disk_bytes([root], root.stat().st_dev)
        self.assertGreaterEqual(base, MIB); self.assertLess(base, MIB + 64 * 1024)   # 1 MiB 文件 + 目录元数据（APFS 目录 0 块）
        (root / "sub").mkdir()
        os.link(a, root / "sub" / "a-link.bin")
        (root / "a-sym.bin").symlink_to(a)
        sub = library.disk_bytes([root], root.stat().st_dev) - base   # 硬链接 + 软链：数据不重复算，只多一个子目录的元数据
        self.assertLess(sub, 64 * 1024)
        write(root / "b.bin", MIB)
        self.assertEqual(library.disk_bytes([root], root.stat().st_dev) - base - sub, MIB)   # 新增一份真数据 = 正好 1 MiB
        self.assertEqual(library.disk_bytes([root, root / "sub", a], root.stat().st_dev), base + sub + MIB)   # 根互相包含也只算一次
        self.assertEqual(library.disk_bytes([root], root.stat().st_dev + 1), 0)   # 别的卷上的不算

    def test_record_measures_library_dirs_before_prune(self):
        # 打 iPhone 包成功后的自动清理（prune_build）先记一行：要清掉的中间产物也算进去（量的是峰值，不是清完剩下的）
        c = self.context("ipa")
        game = self.game(10 * 10**9)
        library.atomic(self.c.library / "catalog.json", dict(schema=1, games=[game]))
        extract = write(self.c.library / "cache" / GID / "extract" / "romfs.bin", MIB)
        write(self.c.library / "iPhone" / GID / "junk.tmp", 2 * MIB)   # 成功后会被清掉的中间产物
        (self.c.library / "iPhone" / GID / "roots" / "r@1").mkdir(parents=True)
        os.link(extract, self.c.library / "iPhone" / GID / "roots" / "r@1" / "romfs.bin")   # 产物根硬链解包：不重复算
        write(self.c.library / "runtime-cache" / GID / "play.bin", MIB)   # 玩的时候才长的运行时缓存：不算
        write(self.c.library / "iPhone" / "0100000000000001" / "other.bin", MIB)   # 别款：不算
        library.prune_build(c, GID)
        self.assertFalse((self.c.library / "iPhone" / GID / "junk.tmp").exists())   # 清理照常
        lines = [l.split("\t") for l in (self.c.library / library.SPACE_LEDGER).read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
        self.assertEqual(len(lines), 1)
        r = dict(zip(library.SPACE_LEDGER_FIELDS, lines[0]))
        self.assertEqual((r["gameId"], r["target"], r["direct"], r["inputBytes"], r["name"]), (GID, "ipa", "1", str(10 * 10**9), "塞尔达传说 旷野之息"))
        fp = int(r["footprintBytes"])
        self.assertGreaterEqual(fp, 3 * MIB); self.assertLess(fp, 3 * MIB + 64 * 1024)   # 1 MiB 解包（硬链接那份不重算）+ 2 MiB 中间产物
        self.assertTrue((self.c.library / library.SPACE_LEDGER).read_text(encoding="utf-8").startswith("# time\tgameId"))

    # —— 预算三档依据 ——
    # 期望值手算：29.13 GB × 1.1 + 15 GB；验收数 火纹 ≈ 47、王国之泪 ≈ 34、小游戏 ≈ 8（维护者 11-08）
    def need(self, size, **kw):
        return library.pack_need_bytes([dict(size=size, sha256="a" * 64)], **kw)

    def test_formula_acceptance_numbers(self):
        self.assertEqual(self.need(29_130_000_000, windows=False), 32_043_000_000 * 2 + 15_000_000_000)
        self.assertEqual(self.need(17_480_000_000, windows=False), 19_228_000_000 * 2 + 15_000_000_000)
        self.assertEqual(self.need(2_700_000_000, windows=False), 2_970_000_000 * 2 + 5_000_000_000)

    def test_unpacked_counts_zero_and_windows_adds_a_copy(self):
        self.assertEqual(self.need(10_000_000_000, unpacked=True, windows=False), 11_000_000_000 + 15_000_000_000)
        self.assertEqual(self.need(10_000_000_000, windows=True), 11_000_000_000 * 3 + 15_000_000_000)

    def test_ledger_does_not_change_budget(self):
        c = self.context("ipa")
        self.ledger(("2026-11-07T10:00:00", GID, "ipa", "1", 16 * 10**9, 500 * 10**9, "旧账"))
        v = self.budget(c, self.game(16 * 10**9))
        self.assertEqual((v["basis"], v["needBytes"]), ("rule", 17_600_000_000 * 2 + 15_000_000_000))

    def test_rejection_names_need_and_free(self):
        with patch("shutil.disk_usage", return_value=DU(0, 0, 20 * 10**9)):
            with self.assertRaises(Failure) as e:
                library.check_pack_space(self.context("ipa"), self.game(16 * 10**9))
        self.assertEqual(e.exception.code, "disk_full")
        self.assertIn("要 50 GB", str(e.exception)); self.assertIn("只剩 20 GB", str(e.exception)); self.assertIn("「仍然继续」", str(e.exception))

    def test_space_override_passes_with_warning(self):
        c = self.context("ipa", spaceOverride=True)
        with patch("shutil.disk_usage", return_value=DU(0, 0, 20 * 10**9)):
            budget = library.check_pack_space(c, self.game(16 * 10**9))
        self.assertTrue(budget["override"]); self.assertFalse(budget["volumes"][0]["enough"])
        [d] = c.job["diagnostics"]
        self.assertEqual((d["code"], d["severity"]), ("disk_full", "warning"))
        self.assertIn("已按你的选择仍然继续", d["summary"])

    def test_space_override_request_contract(self):
        self.context("build", spaceOverride=True); self.context("ipa", spaceOverride=False)
        for op, value in (("catalog", True), ("ipa", "true"), ("build", 1), ("launch", True)):
            with self.subTest(op=op, value=value), self.assertRaises(Failure):
                self.context(op, spaceOverride=value)
        base = dict(schema=1, contract="s2a.job-request.v1", privateRoot="/p", library="/l", libraryId="x", gameId=GID)
        validate("request", base | dict(operation="build", spaceOverride=True))
        validate("request", base | dict(operation="ipa", spaceOverride=False))
        for bad in (dict(operation="catalog", spaceOverride=True), dict(operation="ipa", spaceOverride="true")):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                validate("request", base | bad)


if __name__ == "__main__":
    unittest.main()
