# -*- coding: utf-8 -*-
r"""exfat_recover.py — 损坏 exFAT 镜像直接恢复 (纯文件系统解析, 不经 DMDE)。

几何推导 (v3):
  1. FAT 签名定位 FAT 起点;
  2. 已知目录名条目 (0xC1+UTF-16) 定位根目录簇内位置;
  3. 簇大小: 根目录里某目录的流条目 size 恰为一簇 (实为 128KB);
  4. 位图条目 (0x81) 锚点: 根簇起点 = 位图条目位置 - 32/64;
  5. 堆基址 = 根簇起点 - (位图簇号-2)*簇大小; 根目录簇号 = 位图簇号 - 1;
  6. 验证: 根目录含已知名 / Mach3CN 目录含 OxygenBlogPlatform / FAT 条目。

用法:
  python exfat_recover.py --image E:/xxx.bin --lba 3712 --out E:/recover_out
      [--list-only] [--deleted] [--limit N] [--dump-root]
"""
import argparse
import os
import struct
import sys
import time

EOC_MIN = 0xFFFFFFF8
SIG = b'\xF8\xFF\xFF\xFF\xFF\xFF\xFF\xFF'
IN_USE = 0x80
CS = 131072                      # 簇大小: 由 Mach3CN 目录流 size=131072 实测得出


class ExFATVol:
    def __init__(self, f, vol_base, fat_off, heap_off, root_clus, vol_len):
        self.f = f
        self.vol_base = vol_base
        self.fat_off = fat_off
        self.heap_off = heap_off
        self.cs = CS
        self.root = root_clus
        self.vol_len = vol_len

    def fat(self, n):
        try:
            self.f.seek(self.fat_off + n * 4)
            return struct.unpack('<I', self.f.read(4))[0]
        except Exception:
            return 0xFFFFFFF7

    def clus_off(self, n):
        return self.heap_off + (n - 2) * self.cs

    def read_clus(self, n):
        try:
            self.f.seek(self.clus_off(n))
            return self.f.read(self.cs)
        except Exception:
            return b'?' * self.cs

    def read_chain(self, first, size, contig):
        out = bytearray()
        bad = 0
        c = first
        n = (size + self.cs - 1) // self.cs
        for k in range(n):
            want = min(self.cs, size - len(out))
            if want <= 0:
                break
            if not contig and (c < 2 or c > self.nclus + 2):
                contig = True
                c = first + k
            try:
                self.f.seek(self.clus_off(c))
                out += self.f.read(want)
            except Exception:
                out += b'?' * want
                bad += 1
            if not contig:
                nxt = self.fat(c)
                if nxt < EOC_MIN:
                    c = nxt
                    continue
                if len(out) < size:
                    contig = True
                    c = first + k + 1
                    continue
                break
            c = first + k + 1
        return bytes(out), bad

    def parse_dir_cluster(self, data):
        ents = []
        n = len(data)
        i = 0
        while i + 64 <= n:
            t = data[i]
            if t == 0:
                i += 32
                continue
            base = t & 0x7F
            deleted = not (t & IN_USE)
            if base == 0x05:
                d = data[i:i + 32]
                sec_cnt = d[1]
                attr = struct.unpack_from('<H', d, 4)[0]
                stream = data[i + 32:i + 64]
                name = ''
                sfc = sdl = 0
                contig = False
                ok = False
                if len(stream) == 32 and (stream[0] & 0x7F) == 0x40:
                    nlen = stream[3]
                    sfc = struct.unpack_from('<I', stream, 20)[0]
                    sdl = struct.unpack_from('<Q', stream, 24)[0]
                    contig = not (stream[0] & 0x01)
                    for j in range(1, sec_cnt + 1):
                        ne = data[i + 32 * (j + 1):i + 32 * (j + 2)]
                        if len(ne) == 32 and (ne[0] & 0x7F) == 0x41:
                            seg = ne[2:32].decode('utf-16-le', errors='replace')
                            name += seg[:max(0, nlen - len(name))]
                    ok = True
                if ok and name.strip():
                    kind = 'dir' if attr & 0x10 else 'file'
                    ents.append(dict(kind=kind, name=name, fc=sfc, size=sdl,
                                     attr=attr, deleted=deleted, contig=contig))
                i += 32 * (sec_cnt + 1)
                continue
            i += 32
        return ents

    def parse_dir(self, first, size=None, contig=False):
        if size is None:
            return self.parse_dir_cluster(self.read_clus(first))
        data, _ = self.read_chain(first, size, contig)
        return self.parse_dir_cluster(data)

    def walk(self, collect=None, on_error=None):
        seen = set()

        def rec(cluster, prefix):
            if cluster in seen:
                return
            seen.add(cluster)
            try:
                ents = self.parse_dir(cluster)
            except Exception as e:
                if on_error:
                    on_error(prefix, e)
                return
            for e in ents:
                if not e['name'].strip():
                    continue
                rel = prefix + os.sep + e['name'] if prefix else e['name']
                if len(rel) > 160:
                    continue
                if collect is not None:
                    collect.append((rel, e))
                if e['kind'] == 'dir' and e['fc'] >= 2:
                    rec(e['fc'], rel)
        rec(self.root, '')


def find_geometry(f, img_path, vol_lba, known_name='Mach3CN'):
    """定位 FAT/堆基址/根目录簇号。cs 固定 131072 (由实测得出)。"""
    import mmap
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    vol_base = vol_lba * 512
    name_pat = b'\xC1\x00' + known_name.encode('utf-16-le')

    # 1) FAT 起点 (卷内 1MB 处命中)
    fat_off = None
    pos = vol_base
    while pos < vol_base + 8 * 2**30:
        seg = mm[pos:min(pos + 64 * 2**20 + 8, pos + 8 * 2**30)]
        i = seg.find(SIG)
        if i >= 0:
            fat_off = pos + i
            break
        pos += 64 * 2**20
    if fat_off is None:
        raise RuntimeError('未找到 FAT 签名')
    print('[1] FAT 起点: 绝对 %d (卷内 %d)' % (fat_off, fat_off - vol_base))

    # 2) 已知名条目 → 根目录簇内位置
    hits = []
    pos = vol_base
    while pos < vol_base + 8 * 2**30:
        seg = mm[pos:min(pos + 64 * 2**20 + 64, pos + 8 * 2**30)]
        i = seg.find(name_pat)
        while i >= 0:
            hits.append(pos + i)
            i = seg.find(name_pat, i + 1)
        pos += 64 * 2**20
        if hits:
            break
    if not hits:
        raise RuntimeError('未找到已知目录名条目')
    hit = hits[0]
    print('[2] 已知名条目: 绝对 %d' % hit)

    # 3) 位图条目 (0x81): fc@+20 ∈ [2,64], size@+24 ∈ [8K, 256K]
    bmp = None
    fc_bmp = None
    back_start = max(vol_base, hit - 65536)
    seg = mm[back_start:hit + 32]
    for i in range(0, len(seg) - 32, 32):
        e = seg[i:i + 32]
        if e[0] == 0x81:
            fc = struct.unpack_from('<I', e, 20)[0]
            sz = struct.unpack_from('<Q', e, 24)[0]
            if 2 <= fc <= 64 and 8192 <= sz <= 262144:
                bmp = back_start + i
                fc_bmp = fc
                break
    if bmp is None:
        raise RuntimeError('未找到位图条目 (0x81)')
    print('[3] 位图条目: 绝对 %d (fc=%d, size=%d)' % (
        bmp, fc_bmp, struct.unpack_from('<Q', mm, bmp + 24)[0]))

    # 4) 根簇起点: 位图条目前的 512 对齐位置 (回退 32/64/96 字节)
    R = None
    for back in (32, 64, 96):
        cand = bmp - back
        if cand >= vol_base and cand % 512 == 0:
            R = cand
            break
    if R is None:
        raise RuntimeError('根簇起点定位异常')

    # 5) 读取根簇头部条目: 位图(0x81, fc=2)与大写表(0x82, fc=3)的簇号
    fc_uc = None
    up_entry = None
    for k in range(6):
        e = mm[R + k * 32: R + (k + 1) * 32]
        if len(e) == 32 and e[0] == 0x82:
            fc_uc = struct.unpack_from('<I', e, 20)[0]
            up_entry = k
            break
    if fc_uc is None:
        raise RuntimeError('根簇中未找到 UpCase 条目 (0x82)')

    # 6) 搜索 UpCase 表内容 → 解堆基址与根目录簇号
    U = None
    lo = max(vol_base, R - 4 * 2**20)
    hi = R + 4 * 2**20
    pos = lo
    UPCASE_SIG = b'\x00\x00\x01\x00\x02\x00\x03\x00\x04\x00\x05\x00\x06\x00\x07\x00'
    while pos < hi:
        seg = mm[pos:min(pos + 1024 * 1024 + 16, hi)]
        i = seg.find(UPCASE_SIG)
        while i >= 0:
            cand = pos + i
            tbl = mm[cand:cand + 5856]
            if len(tbl) == 5856 and tbl[0x61 * 2:0x61 * 2 + 2] == b'A\x00'                     and tbl[0x7A * 2:0x7A * 2 + 2] == b'Z\x00':
                U = cand
                break
            i = seg.find(UPCASE_SIG, i + 1)
        if U is not None:
            break
        pos += 1024 * 1024
    if U is None:
        raise RuntimeError('未找到 UpCase 表')
    heap = U - (fc_uc - 2) * CS
    if heap <= fat_off or (R - heap) % CS:
        raise RuntimeError('堆基址解算异常: heap=%d' % heap)
    root_clus = 2 + (R - heap) // CS
    print('[4] UpCase 表=%d (簇%d), 堆基址=%d, 根目录簇=%d' % (
        U, (U - heap) // CS + 2, heap, root_clus))

    vol = ExFATVol(f, vol_base, fat_off, heap, root_clus, 0)

    # 7) 验证: 根目录含已知名; Mach3CN 目录含 OxygenBlogPlatform; FAT 条目
    ents = vol.parse_dir(vol.root)
    names = [e['name'] for e in ents]
    if not any(known_name in n for n in names):
        raise RuntimeError('根目录无 %s (几何错误): %r' % (known_name, names[:8]))
    mach = None
    for e in ents:
        if known_name in e['name']:
            mach = e
    if mach['kind'] != 'dir':
        raise RuntimeError('%s 不是目录' % known_name)
    ments = vol.parse_dir(mach['fc'])
    mnames = [e['name'] for e in ments]
    if not mnames:
        raise RuntimeError('Mach3CN 目录为空 (几何错误)')
    feat = vol.fat(root_clus)
    print('[5] 验证通过: 根条目 %d, Mach3CN 子项 %d, FAT[root]=%x' % (
        len(ents), len(ments), feat))
    return vol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', required=True)
    ap.add_argument('--lba', type=int, default=3712)
    ap.add_argument('--name', default='Mach3CN', help='根目录下确定的文件夹名')
    ap.add_argument('--out', default=None)
    ap.add_argument('--list-only', action='store_true')
    ap.add_argument('--deleted', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--dump-root', action='store_true')
    args = ap.parse_args()

    f = open(args.image, 'rb')
    vol = find_geometry(f, args.image, args.lba, args.name)

    if args.dump_root:
        for e in vol.parse_dir(vol.root):
            print('%s %-42s %12d 首簇=%d%s' % ('D' if e['kind'] == 'dir' else 'F',
                  e['name'][:42], e['size'], e['fc'], ' [已删除]' if e['deleted'] else ''))
        return

    t0 = time.time()
    collect = []
    vol.walk(collect=collect)
    ndir = sum(1 for _, e in collect if e['kind'] == 'dir')
    nfile = sum(1 for _, e in collect if e['kind'] == 'file')
    ndel = sum(1 for _, e in collect if e['deleted'])
    print('遍历: 目录 %d, 文件 %d (已删除 %d), 耗时 %.1f 秒' % (
        ndir, nfile, ndel, time.time() - t0))

    if args.list_only:
        for rel, e in collect:
            if e['kind'] == 'file':
                print('%s%s %10d' % ('[D] ' if e['deleted'] else '    ', rel, e['size']))
        return

    out = args.out
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    ok = bad = skip = 0
    badlist = []
    for rel, e in collect:
        if e['kind'] != 'file' or not e['name'].strip():
            continue
        if args.limit and ok >= args.limit:
            break
        if e['deleted'] and not args.deleted:
            skip += 1
            continue
        dst = os.path.join(out, rel.replace('/', os.sep))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            data, badn = vol.read_chain(e['fc'], e['size'], e['contig'])
            with open(dst, 'wb') as fh:
                fh.write(data)
            ok += 1
            if badn:
                bad += 1
                badlist.append((rel, '坏簇 %d' % badn))
        except Exception as ex:
            bad += 1
            badlist.append((rel, str(ex)))
        if ok % 1000 == 0:
            print('  已恢复 %d ... (%.1f 秒)' % (ok, time.time() - t0))
    print('完成: 成功 %d, 损坏 %d, 跳过(未勾选已删除) %d, 耗时 %.1f 秒' % (
        ok, bad, skip, time.time() - t0))
    if badlist:
        with open(os.path.join(out, '_损坏文件清单.txt'), 'w', encoding='utf-8') as fh:
            for rel, why in badlist:
                fh.write('%s\t%s\n' % (rel, why))
        print('损坏清单: %s' % os.path.join(out, '_损坏文件清单.txt'))


if __name__ == '__main__':
    main()
