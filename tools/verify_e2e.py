# -*- coding: utf-8 -*-
"""按 mkfat32 生成的清单校验端到端恢复结果: 文件数/相对路径/大小/SHA256/目录。"""
import hashlib
import json
import os
import sys

BASE = r'C:\Users\admin\Desktop\freedmde'
OUT = os.path.join(BASE, 'rec_out')
MANIFEST = os.path.join(BASE, 'testimg64.manifest.json')


def main():
    with open(MANIFEST, encoding='utf-8') as f:
        man = json.load(f)
    ok = bad = missing = extra = 0
    for rel, info in man['files'].items():
        p = os.path.join(OUT, rel.replace('/', os.sep))
        if not os.path.exists(p):
            missing += 1
            if missing <= 15:
                print('MISSING:', rel)
            continue
        data = open(p, 'rb').read()
        if len(data) == info['size'] and hashlib.sha256(data).hexdigest() == info['sha256']:
            ok += 1
        else:
            bad += 1
            if bad <= 15:
                print('MISMATCH:', rel, 'got', len(data), 'expect', info['size'])
    for d in man['dirs']:
        if not os.path.isdir(os.path.join(OUT, d.replace('/', os.sep))):
            print('MISSING DIR:', d)
            missing += 1
    exp = set(man['files'])
    for root, ds, fs in os.walk(OUT):
        rel = os.path.relpath(root, OUT)
        for f in fs:
            if f.startswith('_dmde_'):
                continue
            r = os.path.normpath(os.path.join(rel, f)).replace(os.sep, '/') if rel != '.' else f
            if r not in exp:
                extra += 1
                if extra <= 15:
                    print('EXTRA:', r)
    print('OK=%d BAD=%d MISSING=%d EXTRA=%d (expected %d files, %d dirs)' % (
        ok, bad, missing, extra, len(man['files']), len(man['dirs'])))
    sys.exit(0 if (ok == len(man['files']) and bad == 0 and missing == 0 and extra == 0) else 1)


if __name__ == '__main__':
    main()
