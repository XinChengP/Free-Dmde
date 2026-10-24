# -*- coding: utf-8 -*-
"""构造一个 FAT32 测试镜像, 用于安全地调试 DMDE 自动恢复脚本。

用法: python mkfat32.py <镜像路径> [--verify]
"""
import struct
import sys

SECTOR = 512
SPC = 1
CLUSTER = SECTOR * SPC
TOTAL_SECTORS = 131072  # 64 MB
RESERVED = 32
NUM_FATS = 2
ROOT_CLUSTER = 2
VALID = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&()-@^_`{}~',=")


def dos_datetime():
    return (12 << 11), ((46 << 9) | (1 << 5) | 1)  # 2026-01-01 12:00


def sfn_of(name, used):
    if '.' in name[1:]:
        base, ext = name.rsplit('.', 1)
    else:
        base, ext = name, ''
    sb = ''.join(c.upper() if c.upper() in VALID else '_' for c in base)[:8] or '_'
    se = ''.join(c.upper() if c.upper() in VALID else '_' for c in ext)[:3]
    short = sb.ljust(8) + se.ljust(3)
    if short in used:
        for n in range(1, 100):
            cand = (sb[:6] + '~%d' % n).ljust(8) + se.ljust(3)
            if cand not in used:
                short = cand
                break
    used.add(short)
    need_lfn = True   # 一律写 LFN, 保证 DMDE 显示与原始名一致
    return short, need_lfn


def lfn_checksum(short11):
    s = 0
    for c in short11.encode('ascii'):
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


def lfn_entry_blocks(longname, short11):
    cps = [ord(c) for c in longname] + [0x0000]
    while len(cps) % 13:
        cps.append(0xFFFF)
    blocks = [cps[i:i + 13] for i in range(0, len(cps), 13)]
    ck = lfn_checksum(short11)
    n = len(blocks)
    ents = []
    for i, blk in enumerate(reversed(blocks), 1):
        seq = n - i + 1
        if i == 1:
            seq |= 0x40  # 第一个存储的条目含名字末尾, 带终止标志
        ents.append(struct.pack('<B5HBBB6HH2H', seq, *blk[:5], 0x0F, 0, ck,
                                *blk[5:11], 0, *blk[11:13]))
    return ents


def short_entry(short11, attr, cluster, size):
    t, d = dos_datetime()
    return struct.pack('<11sBBBHHHHHHHI', short11.encode('ascii'), attr, 0, 0,
                       t, d, d, cluster >> 16, t, d, cluster & 0xFFFF, size)


class Node:
    def __init__(self, name, is_dir):
        self.name = name
        self.is_dir = is_dir
        self.children = []
        self.files = []
        self.first = 0
        self.nclusters = 0
        self.content = b''
        self.size = 0


class Fat32:
    def __init__(self, total_sectors):
        fat_secs = 1024
        for _ in range(4):
            data_secs = total_sectors - RESERVED - fat_secs * NUM_FATS
            clusters = data_secs // SPC
            fat_secs = ((clusters + 2) * 4 + 511) // 512
        self.fat_secs = fat_secs
        self.max_cluster = (data_secs // SPC) + 1
        self.fat = [0] * (self.max_cluster + 2)
        self.fat[0] = 0x0FFFFFF8
        self.fat[1] = 0xFFFFFFFF
        self.next_free = 2  # 首个分配的簇即根目录(簇 2)
        self.data = bytearray(data_secs * SECTOR)

    def alloc(self, count):
        first = self.next_free
        assert first + count - 1 <= self.max_cluster, 'image too small'
        for i in range(count):
            self.fat[first + i] = first + i + 1 if i < count - 1 else 0x0FFFFFFF
        self.next_free += count
        return first

    def clus_off(self, c):
        return (c - 2) * CLUSTER


def measure_bytes(node):
    """本目录需要的目录项字节数(含 '.' '..'), used 集与填表阶段保持一致。"""
    used = set()
    total = 2 * 32

    def one(name):
        short, need = sfn_of(name, used)
        return (len(lfn_entry_blocks(name, short)) if need else 0) * 32 + 32

    for ch in node.children:
        total += one(ch.name)
    for f in node.files:
        total += one(f.name)
    return total


def alloc_tree(b, node):
    node.nclusters = max(1, (measure_bytes(node) + CLUSTER - 1) // CLUSTER)
    node.first = b.alloc(node.nclusters)
    for ch in node.children:
        alloc_tree(b, ch)
    for f in node.files:
        f.size = len(f.content)
        f.first = b.alloc(max(1, (f.size + CLUSTER - 1) // CLUSTER))


def fill_tree(b, node, parent_first):
    used = set()
    blob = bytearray()
    blob += short_entry('.          ', 0x10, node.first, 0)
    blob += short_entry('..         ', 0x10, 0 if parent_first == ROOT_CLUSTER else parent_first, 0)

    def entries_of(name):
        short, need = sfn_of(name, used)
        return (b''.join(lfn_entry_blocks(name, short)) if need else b''), short

    for ch in node.children:
        lfn, short = entries_of(ch.name)
        blob += lfn
        blob += short_entry(short, 0x10, ch.first, 0)
    for f in node.files:
        lfn, short = entries_of(f.name)
        blob += lfn
        blob += short_entry(short, 0x20, f.first, f.size)
    off = b.clus_off(node.first)
    b.data[off:off + len(blob)] = blob

    for f in node.files:
        off = b.clus_off(f.first)
        b.data[off:off + f.size] = f.content
    for ch in node.children:
        fill_tree(b, ch, node.first)


def build(path):
    import hashlib
    import json

    def D(name):
        return Node(name, True)

    def F(name, content):
        n = Node(name, False)
        n.content = content
        return n

    root = D('$Root')
    docs, cn, onlydirs = D('docs'), D('中文'), D('onlydirs')
    empty, sub, deep = D('empty'), D('sub'), D('deep')
    ox, oy = D('x'), D('y')
    root.children = [docs, cn, onlydirs]
    docs.children = [empty, sub]
    sub.children = [deep]
    onlydirs.children = [ox, oy]

    root.files = [F('readme.txt', b'DMDE minimal test image\n' * 50)]
    docs.files = [F('a1.txt', b'A1 ' * 100),
                  F('a2.txt', b'A2 ' * 100)]
    sub.files = [F('b1.txt', b'B1 ' * 100)]
    deep.files = [F('c1.txt', b'C1 ' * 100)]
    cn.files = [F('图片001.jpg', bytes((i * 31 + 7) % 256 for i in range(4096))),
                F('说明.txt', ('中文内容测试\n' * 20).encode('utf-8'))]

    b = Fat32(TOTAL_SECTORS)
    alloc_tree(b, root)
    fill_tree(b, root, ROOT_CLUSTER)

    # 生成校验清单
    manifest = {'dirs': [], 'files': {}}

    def collect(node, prefix):
        for ch in node.children:
            rel = (prefix + '/' + ch.name) if prefix else ch.name
            manifest['dirs'].append(rel)
            collect(ch, rel)
        for f in node.files:
            rel = (prefix + '/' + f.name) if prefix else f.name
            manifest['files'][rel] = {
                'size': len(f.content),
                'sha256': hashlib.sha256(f.content).hexdigest(),
            }

    collect(root, '')
    mpath = path.rsplit('.', 1)[0] + '.manifest.json'
    with open(mpath, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print('manifest -> %s (%d files, %d dirs)' % (mpath, len(manifest['files']), len(manifest['dirs'])))

    img = bytearray(TOTAL_SECTORS * SECTOR)
    boot = bytearray(SECTOR)
    boot[0:3] = b'\xEB\x3C\x90'
    boot[3:11] = b'MSWIN4.1'
    struct.pack_into('<HBHBHHBHHHII', boot, 11, SECTOR, SPC, RESERVED, NUM_FATS,
                     0, 0, 0xF8, b.fat_secs, 63, 255, 0, TOTAL_SECTORS)
    struct.pack_into('<IHHIHH', boot, 36, b.fat_secs, 0, 0, ROOT_CLUSTER, 1, 6)
    boot[64] = 0x80
    boot[66] = 0x29
    struct.pack_into('<I', boot, 67, 0x20260101)
    boot[71:82] = b'FREEDMDE   '
    boot[82:90] = b'FAT32   '
    boot[510:512] = b'\x55\xAA'
    img[0:SECTOR] = boot
    img[6 * SECTOR:7 * SECTOR] = boot

    fsinfo = bytearray(SECTOR)
    struct.pack_into('<I', fsinfo, 0, 0x41615252)
    struct.pack_into('<I', fsinfo, 484, 0x61417272)
    struct.pack_into('<I', fsinfo, 488, 0xFFFFFFFF)
    struct.pack_into('<I', fsinfo, 492, 0xFFFFFFFF)
    struct.pack_into('<I', fsinfo, 508, 0xAA550000)
    img[SECTOR:2 * SECTOR] = fsinfo
    img[7 * SECTOR:8 * SECTOR] = fsinfo

    fat = struct.pack('<%dI' % len(b.fat), *b.fat)
    o1 = RESERVED * SECTOR
    o2 = (RESERVED + b.fat_secs) * SECTOR
    img[o1:o1 + len(fat)] = fat
    img[o2:o2 + len(fat)] = fat
    img[(RESERVED + 2 * b.fat_secs) * SECTOR:] = b.data

    with open(path, 'wb') as f:
        f.write(img)
    print('written %s (%.0f MB), fat_secs=%d, clusters used=%d' %
          (path, len(img) / 1048576, b.fat_secs, b.next_free - 3))


def verify(path):
    img = open(path, 'rb').read()
    fat_secs = struct.unpack_from('<I', img, 0x24)[0]
    fat = struct.unpack_from('<%dI' % (fat_secs * SECTOR // 4), img, RESERVED * SECTOR)
    data_off = (RESERVED + 2 * fat_secs) * SECTOR

    def chain(first):
        out = []
        c = first
        while 2 <= c < 0x0FFFFFF8:
            out.append(c)
            c = fat[c]
        return out

    def read_dir(cfirst):
        return b''.join(img[data_off + (c - 2) * CLUSTER:data_off + (c - 1) * CLUSTER]
                        for c in chain(cfirst))

    stats = {'f': 0, 'd': 0}

    def walk(cfirst, prefix, verbose=True):
        blob = read_dir(cfirst)
        i = 0
        lfn = ''
        while i < len(blob) and blob[i] != 0:
            e = blob[i:i + 32]
            attr = e[11]
            if attr == 0x0F:
                chars = (struct.unpack_from('<5H', e, 1) + struct.unpack_from('<6H', e, 14)
                         + struct.unpack_from('<2H', e, 28))
                if e[0] & 0x40:
                    lfn = ''
                lfn = ''.join(chr(x) for x in chars if x not in (0xFFFF, 0)) + lfn
                i += 32
                continue
            name = lfn or e[0:11].decode('ascii', 'replace').rstrip()
            lfn = ''
            cl = struct.unpack_from('<H', e, 26)[0]
            sz = struct.unpack_from('<I', e, 28)[0]
            if not (attr & 0x08) and name not in ('.', '..'):
                if attr & 0x10:
                    stats['d'] += 1
                    if verbose:
                        print(prefix + name + '/')
                    walk(cl, prefix + '  ', verbose and len(prefix) < 24)
                else:
                    stats['f'] += 1
                    if verbose:
                        print(prefix + name + '  (%d B)' % sz)
            i += 32

    walk(ROOT_CLUSTER, '')
    print('TOTAL files=%d dirs(excl root)=%d' % (stats['f'], stats['d']))


if __name__ == '__main__':
    p = sys.argv[1] if len(sys.argv) > 1 else 'testimg64.img'
    if '--verify' in sys.argv:
        verify(p)
    else:
        build(p)
