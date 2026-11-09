using LibHac;
using LibHac.Common;
using LibHac.Fs;
using LibHac.Fs.Fsa;
using LibHac.Tools.FsSystem;
using LibHac.Tools.FsSystem.RomFs;
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Path = System.IO.Path;

namespace S2A.Services
{
    // 预建 RomFS 索引（出包时生成，起局直接读）：解开的 romfs/ 目录要现建 RomFS 镜像，就得枚举十几万个文件、逐个取长度、再由 LibHac 建目录表——
    // 动森 Mac 暖局 450 ms、iPhone 约 2.7 秒，占 S2A_Init 八成。索引记下 LibHac 建出的镜像布局：每个文件在镜像里的偏移 / 长度 / 相对路径，
    // 以及文件数据以外的全部字节（头、目录 / 文件哈希与元数据表；全零的对齐填充不存）。起局读这一个文件即可拼出同一镜像，文件读到时才打开。
    //
    // 格式（小端）："S2ARFS01" | i64 镜像长 | i32 文件数 | 文件数 × {i64 偏移, i64 长度, i32 路径偏移, i32 路径长} | i32 路径区长 | 路径区（UTF-8，相对 romfs/，/ 分隔）
    //              | i32 段数 | 段数 × {i64 偏移, i32 长度} | 各段字节依次
    public static class RomFsIndex
    {
        public const string FileName = "romfs.s2aidx";
        public const string ImageName = "romfs.img";
        public const string ImageMapName = "romfs.imgmap";
        public const string ImageOverlayDir = "romfs_overlay";
        private static ReadOnlySpan<byte> ImageMagic => "S2ARIM01"u8;
        private static ReadOnlySpan<byte> Magic => "S2ARFS01"u8;

        public static void Write(string romfsDir, string outPath) => Write(romfsDir, outPath, null);

        // listPath：出包期 RomFS 清单（graphics/nvn/tools/romfs_list.py sizes 写出：每行「字节数\t相对路径」，普通文件、/ 分隔）。
        //   给了就不再枚举目录、逐个打开取长度（火纹 27 万文件约 20 秒）：仓库里 RomFS 目录遍历只在 romfs_list.py 一处（tools/romfs_walk_lint.py 守）。
        //   清单与目录同一时刻盘点（产物根里没有软链，roots.sh 拒收），集合与长度同 EnumerateFiles + OpenRead().Length；没给照旧现枚举（运行期 / 单测）。
        public static void Write(string romfsDir, string outPath, string listPath)
        {
            string root = Path.GetFullPath(romfsDir);
            List<string> rel = new();
            List<long> size = new();
            Dictionary<string, long> listed = null;

            if (listPath != null)
            {
                listed = new(StringComparer.Ordinal);

                foreach (string line in File.ReadLines(listPath, Encoding.UTF8))
                {
                    int tab = line.IndexOf('\t');

                    if (tab <= 0 || !long.TryParse(line.AsSpan(0, tab), out long n) || n < 0 || !listed.TryAdd(line[(tab + 1)..], n))
                    {
                        throw new InvalidDataException($"RomFS 索引：清单行不认识：{line}");
                    }
                }

                rel.AddRange(listed.Keys);
            }
            else
            {
                foreach (string f in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories))
                {
                    if (IsHostJunk(f)) continue;
                    rel.Add(Path.GetRelativePath(root, f).Replace('\\', '/'));
                }
            }

            rel.Sort(StringComparer.Ordinal);
            RomFsBuilder builder = new();

            foreach (string r in rel)
            {
                long n;

                if (listed != null)
                {
                    n = listed[r];
                }
                else
                {
                    // Measure the same target that IndexedStorage/LazyHostFile reads.
                    using FileStream content = File.OpenRead(Path.Combine(root, r));
                    n = content.Length;
                }

                size.Add(n);
                builder.AddFile("/" + r, new SizeOnlyFile(n));
            }

            IStorage image = builder.Build();
            image.GetSize(out long total).ThrowIfFailure();
            RomFsFileSystem fs = new(image);
            long dataOffset = fs.Header.DataOffset;

            List<(long Off, long Len, int Idx)> files = new(rel.Count);

            for (int i = 0; i < rel.Count; i++)
            {
                if (!fs.FileTable.TryOpenFile("/" + rel[i], out RomFileInfo info) || info.Length != size[i])
                {
                    throw new InvalidDataException($"RomFS 索引：建出的镜像里找不到 /{rel[i]} 或长度不符");
                }

                // 零长度文件没有数据可读，不进区间表（它的偏移与下一个文件相同）
                if (info.Length > 0)
                {
                    files.Add((dataOffset + info.Offset, info.Length, i));
                }
            }

            files.Sort((a, b) => a.Off.CompareTo(b.Off));

            // 文件数据以外的区间：从镜像里读出（文件是只给长度的空文件，这些区间不含文件数据），全零的不存
            List<(long Off, byte[] Data)> segs = new();
            long pos = 0;

            void Gap(long from, long to)
            {
                const int Chunk = 1 << 20;

                for (long o = from; o < to; o += Chunk)
                {
                    byte[] b = new byte[(int)Math.Min(Chunk, to - o)];
                    image.Read(o, b).ThrowIfFailure();

                    if (Array.Exists(b, x => x != 0))
                    {
                        segs.Add((o, b));
                    }
                }
            }

            foreach ((long off, long len, _) in files)
            {
                if (off < pos)
                {
                    throw new InvalidDataException("RomFS 索引：文件区间重叠");
                }

                Gap(pos, off);
                pos = off + len;
            }

            Gap(pos, total);

            using BinaryWriter w = new(File.Create(outPath + ".tmp"));
            w.Write(Magic);
            w.Write(total);
            w.Write(files.Count);
            MemoryStream paths = new();

            foreach ((long off, long len, int idx) in files)
            {
                byte[] p = Encoding.UTF8.GetBytes(rel[idx]);
                w.Write(off);
                w.Write(len);
                w.Write((int)paths.Length);
                w.Write(p.Length);
                paths.Write(p);
            }

            w.Write((int)paths.Length);
            w.Write(paths.GetBuffer(), 0, (int)paths.Length);
            w.Write(segs.Count);

            foreach ((long off, byte[] data) in segs)
            {
                w.Write(off);
                w.Write(data.Length);
            }

            foreach ((_, byte[] data) in segs)
            {
                w.Write(data);
            }

            w.Close();
            File.Move(outPath + ".tmp", outPath, overwrite: true);
        }

        public static IStorage Open(string romfsDir, string indexPath, string overlayDir = null) =>
            new IndexedStorage(Path.GetFullPath(romfsDir), File.ReadAllBytes(indexPath), indexPath, OverlayFiles(overlayDir), null, null);

        // 单文件镜像（pack/romfs_image.py）：romfs.img + romfs.imgmap + 索引齐全且镜像长度对得上才用；没有或长度不符（推到一半）→ null，调用方退回目录布局或报红
        public static IStorage OpenImage(string programDir)
        {
            string image = Path.Combine(programDir, ImageName), map = Path.Combine(programDir, ImageMapName), index = Path.Combine(programDir, FileName);
            if (!File.Exists(image) || !File.Exists(map) || !File.Exists(index)) return null;
            byte[] raw = File.ReadAllBytes(map);
            if (raw.Length < 20 || !raw.AsSpan(0, 8).SequenceEqual(ImageMagic)) throw new InvalidDataException($"RomFS 镜像对照表格式不对：{map}");
            long length = BitConverter.ToInt64(raw, 8);
            int count = BitConverter.ToInt32(raw, 16);
            if (raw.Length != 20 + count * 8L) throw new InvalidDataException($"RomFS 镜像对照表长度不对：{map}");
            var handle = File.OpenHandle(image, FileMode.Open, FileAccess.Read, FileShare.Read, FileOptions.RandomAccess);
            if (RandomAccess.GetLength(handle) != length)
            {
                handle.Dispose();
                return null;
            }
            long[] offsets = new long[count];
            Buffer.BlockCopy(raw, 20, offsets, 0, count * 8);
            return new IndexedStorage(Path.GetFullPath(Path.Combine(programDir, "romfs")), File.ReadAllBytes(index), index,
                OverlayFiles(Path.Combine(programDir, ImageOverlayDir)), handle, offsets);
        }

        // macOS 垃圾（同 pack/pack.py、graphics/nvn/tools/romfs_list.py）：不进资源包，也就不能进镜像，否则游戏枚举到就读不到
        internal static bool IsHostJunk(string path)
        {
            string name = Path.GetFileName(path);
            return name == ".DS_Store" || name.StartsWith("._", StringComparison.Ordinal);
        }

        // 界面布局覆盖层 <产物根>/ui_overlay/romfs/（Windows 编排 uilayout，pack/orch/stages.py）：读方覆盖层优先——
        //   同相对路径的文件读覆盖层那份（索引里的长度本就按覆盖层算）。只有几十个文件，起局一次盘点成表；没有覆盖层 → null
        internal static Dictionary<string, string> OverlayFiles(string overlayDir)
        {
            if (string.IsNullOrEmpty(overlayDir) || !Directory.Exists(overlayDir)) return null;
            string full = Path.GetFullPath(overlayDir);
            Dictionary<string, string> map = new(StringComparer.Ordinal);
            foreach (string f in Directory.EnumerateFiles(full, "*", SearchOption.AllDirectories))
            {
                if (IsHostJunk(f)) continue;
                map[Path.GetRelativePath(full, f).Replace('\\', '/')] = f;
            }
            return map.Count == 0 ? null : map;
        }

        // 镜像 = 有序区间表（文件 / 元数据段），区间之间是全零填充
        private sealed class IndexedStorage : IStorage
        {
            private readonly string _root;
            private readonly byte[] _idx;
            private readonly long _size;
            private readonly long[] _start;     // 各区间起点（升序）
            private readonly long[] _len;
            private readonly int[] _ref;        // ≥ 0：文件号；< 0：~段号
            private readonly int[] _pathOff, _pathLen;
            private readonly int _pathBase;
            private readonly int[] _segData;    // 段字节在 _idx 里的位置
            private readonly LazyHostFile[] _files;
            private readonly long[] _fileLen;

            private readonly Dictionary<string, string> _overlay;   // RomFS 相对路径 → 覆盖层文件（OverlayFiles），null = 无覆盖层
            private readonly Microsoft.Win32.SafeHandles.SafeFileHandle _image;   // 单文件镜像；null = 目录布局
            private readonly long[] _imageOffset;   // 文件号 → 镜像偏移，-1 = 在覆盖层

            public IndexedStorage(string root, byte[] idx, string where, Dictionary<string, string> overlay, Microsoft.Win32.SafeHandles.SafeFileHandle image, long[] imageOffset)
            {
                _root = root;
                _overlay = overlay;
                _image = image;
                _imageOffset = imageOffset;
                _idx = idx;

                if (idx.Length < 24 || !idx.AsSpan(0, 8).SequenceEqual(Magic))
                {
                    throw new InvalidDataException($"RomFS 索引格式不对：{where}");
                }

                int p = 8;
                _size = BitConverter.ToInt64(idx, p); p += 8;
                int nf = BitConverter.ToInt32(idx, p); p += 4;
                long[] fOff = new long[nf], fLen = new long[nf];
                _pathOff = new int[nf];
                _pathLen = new int[nf];

                for (int i = 0; i < nf; i++)
                {
                    fOff[i] = BitConverter.ToInt64(idx, p);
                    fLen[i] = BitConverter.ToInt64(idx, p + 8);
                    _pathOff[i] = BitConverter.ToInt32(idx, p + 16);
                    _pathLen[i] = BitConverter.ToInt32(idx, p + 20);
                    p += 24;
                }

                int pathBytes = BitConverter.ToInt32(idx, p); p += 4;
                _pathBase = p;
                p += pathBytes;
                int ns = BitConverter.ToInt32(idx, p); p += 4;
                long[] sOff = new long[ns];
                int[] sLen = new int[ns];

                for (int i = 0; i < ns; i++)
                {
                    sOff[i] = BitConverter.ToInt64(idx, p);
                    sLen[i] = BitConverter.ToInt32(idx, p + 8);
                    p += 12;
                }

                _segData = new int[ns];

                for (int i = 0; i < ns; i++)
                {
                    _segData[i] = p;
                    p += sLen[i];
                }

                // 合并成一张升序区间表（文件与段互不重叠，两表各自已升序）
                int n = nf + ns;
                _start = new long[n];
                _len = new long[n];
                _ref = new int[n];

                for (int i = 0, a = 0, b = 0; i < n; i++)
                {
                    if (b >= ns || (a < nf && fOff[a] < sOff[b]))
                    {
                        (_start[i], _len[i], _ref[i]) = (fOff[a], fLen[a], a);
                        a++;
                    }
                    else
                    {
                        (_start[i], _len[i], _ref[i]) = (sOff[b], sLen[b], ~b);
                        b++;
                    }
                }

                _files = new LazyHostFile[nf];
                _fileLen = fLen;

                if (imageOffset != null && imageOffset.Length != nf)
                {
                    throw new InvalidDataException($"RomFS 镜像对照表 {imageOffset.Length} 项，索引 {nf} 个文件：{where}");
                }
            }

            public override Result Read(long offset, Span<byte> destination)
            {
                if (offset < 0 || offset + destination.Length > _size)
                {
                    return ResultFs.OutOfRange.Value;
                }

                // 第一个可能相交的区间：起点 ≤ offset 的最后一个
                int i = Array.BinarySearch(_start, offset);
                i = i >= 0 ? i : Math.Max(0, ~i - 1);

                while (!destination.IsEmpty)
                {
                    if (i >= _start.Length || offset < _start[i])
                    {
                        // 区间之前（或之间）的填充
                        long next = i < _start.Length ? _start[i] : _size;
                        int z = (int)Math.Min(destination.Length, next - offset);
                        destination[..z].Clear();
                        destination = destination[z..];
                        offset += z;
                        continue;
                    }

                    long end = _start[i] + _len[i];

                    if (offset >= end)
                    {
                        i++;
                        continue;
                    }

                    int count = (int)Math.Min(destination.Length, end - offset);
                    long inner = offset - _start[i];

                    if (_ref[i] >= 0 && _image != null && _imageOffset[_ref[i]] >= 0)
                    {
                        // 读不满就是镜像被截短，回失败，不补零当完整（同下面目录布局）
                        long at = _imageOffset[_ref[i]] + inner;
                        Span<byte> left = destination[..count];

                        while (!left.IsEmpty)
                        {
                            int got = RandomAccess.Read(_image, left, at);
                            if (got <= 0) return ResultFs.HostFileDataCorrupted.Value;
                            left = left[got..];
                            at += got;
                        }
                    }
                    else if (_ref[i] >= 0)
                    {
                        Result r = File(_ref[i], out LazyHostFile file);

                        if (r.IsSuccess())
                        {
                            r = file.Read(out long got, inner, destination[..count], ReadOption.None);

                            // 区间按索引长度划定：读不满就是宿主文件与镜像对不上（审计 #1534 P1-6），回失败，不补零当完整
                            if (r.IsSuccess() && got != count)
                            {
                                r = ResultFs.HostFileDataCorrupted.Value;
                            }
                        }

                        if (r.IsFailure())
                        {
                            return r;
                        }
                    }
                    else
                    {
                        _idx.AsSpan(_segData[~_ref[i]] + (int)inner, count).CopyTo(destination);
                    }

                    destination = destination[count..];
                    offset += count;
                    i++;
                }

                return Result.Success;
            }

            private Result File(int f, out LazyHostFile h)
            {
                h = _files[f];

                if (h == null)
                {
                    // 多个 IStorage 会话并发读（各自加锁，HleTarget）：只让一个新建的进表，输的那份丢掉（还没打开过，无句柄）
                    string rel = Encoding.UTF8.GetString(_idx, _pathBase + _pathOff[f], _pathLen[f]);
                    string full = Path.GetFullPath(Path.Combine(_root, rel));

                    // 拼完必须仍在 romfs/ 下（审计 #1534 P1-5）：索引被改成 ../ 或绝对路径就拒绝这次打开，
                    //   码同 LibHac 路径规范化里 .. 越过根（DirectoryUnobtainable）
                    if (!full.StartsWith(Path.TrimEndingDirectorySeparator(_root) + Path.DirectorySeparatorChar, StringComparison.Ordinal))
                    {
                        Red.Report("RomFS 索引里的路径走出游戏 romfs/，拒绝打开", $"{rel} → {full}");
                        return ResultFs.DirectoryUnobtainable.Value;
                    }

                    if (_overlay != null && _overlay.TryGetValue(rel, out string over)) full = over;

                    h = System.Threading.Interlocked.CompareExchange(ref _files[f], new LazyHostFile(full, _fileLen[f]), null) ?? _files[f];
                }

                return Result.Success;
            }

            public override void Dispose()
            {
                _image?.Dispose();
                base.Dispose();
            }

            public override Result GetSize(out long size)
            {
                size = _size;
                return Result.Success;
            }

            public override Result Write(long offset, ReadOnlySpan<byte> source) => ResultFs.WriteUnpermitted.Value;
            public override Result Flush() => Result.Success;
            public override Result SetSize(long size) => ResultFs.WriteUnpermitted.Value;

            public override Result OperateRange(Span<byte> outBuffer, OperationId operationId, long offset, long size, ReadOnlySpan<byte> inBuffer)
                => ResultFs.NotImplemented.Value;
        }

        // 只给长度的文件（建镜像布局用，读出全零）
        private sealed class SizeOnlyFile : IFile
        {
            private readonly long _size;

            public SizeOnlyFile(long size) => _size = size;

            protected override Result DoRead(out long bytesRead, long offset, Span<byte> destination, in ReadOption option)
            {
                int n = (int)Math.Max(0, Math.Min(destination.Length, _size - offset));
                destination[..n].Clear();
                bytesRead = n;
                return Result.Success;
            }

            protected override Result DoGetSize(out long size)
            {
                size = _size;
                return Result.Success;
            }

            protected override Result DoWrite(long offset, ReadOnlySpan<byte> source, in WriteOption option) => ResultFs.WriteUnpermitted.Value;
            protected override Result DoFlush() => Result.Success;
            protected override Result DoSetSize(long size) => ResultFs.WriteUnpermitted.Value;
            protected override Result DoOperateRange(Span<byte> outBuffer, OperationId operationId, long offset, long size, ReadOnlySpan<byte> inBuffer)
                => ResultFs.NotImplemented.Value;
        }
    }
}
