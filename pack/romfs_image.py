#!/usr/bin/env python3
"""把资源包的 romfs/ 换成单文件镜像：romfs_image.py <按文件的资源包> <输出资源包> --root <产物根>
镜像 romfs.img 按 romfs.s2aidx 的文件顺序，把除界面覆盖层外的文件首尾相接放在一起；romfs.imgmap 记索引里每个文件在镜像里的偏移。
界面覆盖层（产物根 ui_overlay.tsv 列的文件）不进镜像，放 romfs_overlay/ 按文件推：覆盖层变了不用重推镜像。
程序合集的 programs/<n>/romfs 同样处理。其余文件在输出目录里硬链接原文件。
romfs.imgmap（小端）："S2ARIM01" | i64 镜像长 | i32 文件数 | 文件数 × i64 偏移（-1 = 在覆盖层），顺序同 romfs.s2aidx 的文件表。"""
import hashlib, json, os, random, shutil, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))
import sha256_fast

MAGIC = b'S2ARIM01'
CHUNK = 1 << 22
VERSION = 'romfs-image-v1'


class Red(Exception):
    pass


def parse_index(path):
    b = open(path, 'rb').read()
    if b[:8] != b'S2ARFS01': raise Red(f'RomFS 索引格式不对：{path}')
    total, nf = struct.unpack_from('<qi', b, 8); p = 20
    rows = [struct.unpack_from('<qqii', b, p + 24 * i) for i in range(nf)]; p += 24 * nf
    p += 4
    return total, [(off, n, b[p + po:p + po + pl].decode('utf-8')) for off, n, po, pl in rows]


def overlay_names(root):
    try:
        with open(os.path.join(root, 'ui_overlay.tsv'), encoding='utf-8') as f: return {l.strip() for l in f if l.strip()}
    except FileNotFoundError:
        return set()


def link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try: os.link(os.path.realpath(src), dst)
    except OSError: shutil.copy2(src, dst)


def prefixes(rows):
    return sorted({r[0][:r[0].index('romfs/')] for r in rows if r[0].startswith('romfs/') or (r[0].startswith('programs/') and '/romfs/' in r[0])})


def write_image(src_dir, tmp, prefix, rows, overlay):
    """→ 新索引行。romfs 文件必须和索引一一对应，对不上就是索引过期，不出半份镜像"""
    have = {r[0][len(prefix) + 6:]: r for r in rows if r[0].startswith(prefix + 'romfs/')}
    idx_path = os.path.join(src_dir, prefix + 'romfs.s2aidx')
    if not os.path.isfile(idx_path): raise Red(f'{prefix}romfs 没有索引 romfs.s2aidx')
    _, files = parse_index(idx_path)
    listed = {rel for _, n, rel in files}
    zero = {rel for rel, r in have.items() if int(r[1]) == 0}
    if set(have) - zero != listed: 
        extra, lost = sorted(set(have) - zero - listed), sorted(listed - set(have))
        raise Red(f'{prefix}romfs 文件与索引对不上：多 {len(extra)} 个（如 {extra[:3]}），缺 {len(lost)} 个（如 {lost[:3]}）；索引过期，重出 RomFS 索引')
    os.makedirs(os.path.join(tmp, prefix or '.'), exist_ok=True)
    h, pos, offsets, out = sha256_fast.new(), 0, [], []
    with open(os.path.join(tmp, prefix + 'romfs.img'), 'wb') as w:
        for _, n, rel in files:
            if int(have[rel][1]) != n: raise Red(f'{prefix}romfs/{rel} 大小与索引不符')
            if rel in overlay: offsets.append(-1); continue
            offsets.append(pos)
            with open(os.path.join(src_dir, prefix, 'romfs', rel), 'rb') as f:
                left = n
                while left:
                    b = f.read(min(CHUNK, left))
                    if not b: raise Red(f'{prefix}romfs/{rel} 读不满')
                    h.update(b); w.write(b); left -= len(b); pos += len(b)
    with open(os.path.join(tmp, prefix + 'romfs.imgmap'), 'wb') as m:
        m.write(MAGIC + struct.pack('<qi', pos, len(offsets))); m.write(struct.pack(f'<{len(offsets)}q', *offsets))
    out.append((prefix + 'romfs.img', pos, h.hexdigest()))
    mp = os.path.join(tmp, prefix + 'romfs.imgmap')
    out.append((prefix + 'romfs.imgmap', os.path.getsize(mp), hashlib.sha256(open(mp, 'rb').read()).hexdigest()))
    for rel in sorted(overlay & set(have)):
        r = have[rel]; link(os.path.join(src_dir, r[0]), os.path.join(tmp, prefix, 'romfs_overlay', rel)); out.append((prefix + 'romfs_overlay/' + rel, int(r[1]), r[2]))
    # 抽查：按偏移从镜像读回，和原文件逐字节比
    bad = 0; sample = [(o, r) for o, r in zip(offsets, files) if o >= 0]
    with open(os.path.join(tmp, prefix + 'romfs.img'), 'rb') as f:
        for o, (_, n, rel) in random.Random(1).sample(sample, min(200, len(sample))):
            f.seek(o); bad += f.read(n) != open(os.path.join(src_dir, prefix, 'romfs', rel), 'rb').read()
    if bad: raise Red(f'{prefix}镜像抽查 {min(200, len(sample))} 个文件，{bad} 个与原文件不同')
    return out


def build(src_dir, out, root):
    text = open(os.path.join(src_dir, 'index.tsv'), encoding='utf-8').read()
    rows = [l.split('\t') for l in text.splitlines() if l]
    pres = prefixes(rows)
    overlays = {p: overlay_names(os.path.join(root, p)) for p in pres}
    key = hashlib.sha256('\n'.join([VERSION, text, *(f'{p}\t{n}' for p in pres for n in sorted(overlays[p]))]).encode()).hexdigest()
    key_file = out.rstrip('/\\') + '.image.key'
    try:
        same = open(key_file, encoding='utf-8').read().strip() == key and json.load(open(os.path.join(out, 'manifest.json'), encoding='utf-8')).get('version') == 2
    except (OSError, ValueError):
        same = False
    if same:
        print(f'镜像未变，沿用 {out}'); return
    tmp = out.rstrip('/\\') + '.tmp'; shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    new = []
    for p in pres: new += write_image(src_dir, tmp, p, rows, overlays[p])
    for r in rows:
        if any(r[0].startswith(p + 'romfs/') for p in pres): continue
        link(os.path.join(src_dir, r[0]), os.path.join(tmp, r[0])); new.append((r[0], int(r[1]), r[2]))
    ix = ''.join(f'{p}\t{n}\t{s}\n' for p, n, s in new).encode('utf-8')
    with open(os.path.join(tmp, 'index.tsv'), 'wb') as f: f.write(ix)
    with open(os.path.join(tmp, 'manifest.json'), 'w', encoding='utf-8', newline='\n') as f:
        json.dump({'version': 2, 'files': len(new), 'total': sum(n for _, n, _ in new), 'index_sha256': hashlib.sha256(ix).hexdigest()}, f, indent=1)
    shutil.rmtree(out, ignore_errors=True); os.replace(tmp, out)
    with open(key_file + '.tmp', 'w', encoding='utf-8', newline='\n') as f: f.write(key + '\n')
    os.replace(key_file + '.tmp', key_file)
    img = sum(n for p, n, _ in new if p.endswith('romfs.img'))
    print(f'🟢 单文件镜像 {img / 1e9:.2f} GB；资源包 {len(rows)} → {len(new)} 个文件，{out}')


def main(argv):
    if len(argv) != 4 or argv[2] != '--root': sys.exit(__doc__)
    try: build(argv[0], argv[1], argv[3])
    except Red as e: sys.exit(f'【红】{e}')


if __name__ == '__main__':
    main(sys.argv[1:])
