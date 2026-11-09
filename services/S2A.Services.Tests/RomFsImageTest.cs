// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 S2Arm contributors
using LibHac;
using LibHac.Fs;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Path = System.IO.Path;

namespace S2A.Services.Tests
{
    // 单文件镜像（pack/romfs_image.py 的读方）：同一份索引，目录布局和镜像读出整份 RomFS 逐字节相同；覆盖层文件读覆盖层；镜像被截短时不用它
    static class RomFsImageTest
    {
        [SoloTest("--romfs-image")]
        public static int Run()
        {
            string root = Path.Combine(Path.GetTempPath(), $"s2a_romfs_image_{Environment.ProcessId}");
            string program = Path.Combine(root, "game"), rom = Path.Combine(program, "romfs"), overlay = Path.Combine(program, RomFsIndex.ImageOverlayDir);
            int failures = 0;
            void Check(string name, string expected, string actual)
            {
                bool ok = expected == actual;
                if (!ok) failures++;
                Console.WriteLine($"{name}：期望 {expected} → 实 {actual} {(ok ? "✓" : "✗")}");
            }
            static byte[] All(IStorage s)
            {
                s.GetSize(out long n).ThrowIfFailure();
                byte[] b = new byte[n];
                s.Read(0, b).ThrowIfFailure();
                return b;
            }
            try
            {
                Directory.CreateDirectory(Path.Combine(rom, "Data"));
                Directory.CreateDirectory(Path.Combine(overlay, "Data"));
                Dictionary<string, byte> files = new() { ["a.bin"] = 0x11, ["Data/b.bin"] = 0x22, ["Data/ui.zs"] = 0x33 };
                foreach ((string rel, byte v) in files)
                {
                    byte[] data = new byte[1000 + v];
                    Array.Fill(data, v);
                    File.WriteAllBytes(Path.Combine(rom, rel), data);
                }
                File.Copy(Path.Combine(rom, "Data/ui.zs"), Path.Combine(overlay, "Data/ui.zs"));
                string index = Path.Combine(program, RomFsIndex.FileName);
                RomFsIndex.Write(rom, index);
                byte[] want;
                using (IStorage byDir = RomFsIndex.Open(rom, index)) want = All(byDir);

                // 按索引格式手工拼镜像（期望值来自目录布局，不跑镜像写入方）；覆盖层文件不进镜像
                byte[] idx = File.ReadAllBytes(index);
                int nf = BitConverter.ToInt32(idx, 16), p = 20;
                int pathBase = 20 + nf * 24 + 4;
                long[] offsets = new long[nf];
                using (FileStream image = File.Create(Path.Combine(program, RomFsIndex.ImageName)))
                {
                    for (int i = 0; i < nf; i++, p += 24)
                    {
                        string rel = Encoding.UTF8.GetString(idx, pathBase + BitConverter.ToInt32(idx, p + 16), BitConverter.ToInt32(idx, p + 20));
                        if (rel == "Data/ui.zs") { offsets[i] = -1; continue; }
                        offsets[i] = image.Position;
                        image.Write(File.ReadAllBytes(Path.Combine(rom, rel)));
                    }
                }
                long imageLength = new FileInfo(Path.Combine(program, RomFsIndex.ImageName)).Length;
                using (BinaryWriter map = new(File.Create(Path.Combine(program, RomFsIndex.ImageMapName))))
                {
                    map.Write("S2ARIM01"u8);
                    map.Write(imageLength);
                    map.Write(nf);
                    foreach (long o in offsets) map.Write(o);
                }
                Directory.Delete(rom, true);   // 镜像布局下机上没有 romfs/ 目录

                using (IStorage byImage = RomFsIndex.OpenImage(program))
                {
                    Check("镜像 + 覆盖层读出整份 RomFS 与目录布局相同", "True", (byImage != null && want.AsSpan().SequenceEqual(All(byImage))).ToString());
                }
                using (FileStream cut = File.OpenWrite(Path.Combine(program, RomFsIndex.ImageName))) cut.SetLength(imageLength - 1);
                Check("镜像被截短（推到一半）不使用", "null", RomFsIndex.OpenImage(program) == null ? "null" : "已打开");
                File.Delete(Path.Combine(program, RomFsIndex.ImageName));
                Check("没有镜像（旧格式资源包）", "null", RomFsIndex.OpenImage(program) == null ? "null" : "已打开");
            }
            finally { Directory.Delete(root, true); }
            return failures == 0 ? 0 : 1;
        }
    }
}
