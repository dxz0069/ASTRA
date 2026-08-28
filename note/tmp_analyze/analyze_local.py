# -*- coding: utf-8 -*-
"""我方 local-run*.log 行为画像"""
import re, glob, collections, json
from datetime import datetime

def ts(s): return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')

files = sorted(glob.glob(r'E:\study\项目\Cairn-main\dist\local-run*.log'))
for f in files:
    lines = open(f, encoding='utf-8', errors='replace').read().splitlines()
    starts, completes = [], []
    ok_starts, conflict = 0, 0
    flags, flags_ok = [], []
    defer, hint_inj, deadline, starve = 0, 0, 0, 0
    slots_max = 0
    stamps = []
    for L in lines:
        m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', L)
        t = ts(m.group(1)) if m else None
        if t: stamps.append(t)
        if '并行窗口启动题目' in L:
            starts.append((t, re.search(r'code=(\S+)', L).group(1)))
            a = re.search(r'active=(\d+)/(\d+)', L)
            if a: slots_max = max(slots_max, int(a.group(2)))
        elif '并行窗口完成题目' in L:
            completes.append((t, re.search(r'code=(\S+)', L).group(1)))
        elif 'challenges/start' in L and '"HTTP/1.1 200 OK"' in L: ok_starts += 1
        elif 'challenges/start' in L and '409' in L: conflict += 1
        elif 'flag submitted' in L:
            flags.append(L)
            if 'correct=True' in L: flags_ok.append(L)
        elif 'project stopped (defer)' in L: defer += 1
        elif 'platform hint inje' in L or 'hint injected' in L: hint_inj += 1
        elif '时限已到' in L: deadline += 1
        elif 'starvation requeue' in L: starve += 1
    span = (stamps[-1]-stamps[0]).total_seconds()/60 if len(stamps) > 1 else 0
    codes = set(c for _, c in starts)
    print('%s 行数=%d 跨度=%.0f分 slots峰值=%d 启动事件=%d 完成事件=%d 涉及题数=%d' % (
        f.split('local-')[-1], len(lines), span, slots_max, len(starts), len(completes), len(codes)))
    print('   平台start成功=%d 409冲突=%d flag提交=%d correct=True=%d defer=%d hint注入=%d 时限停轮=%d starve=%d' % (
        ok_starts, conflict, len(flags), len(flags_ok), defer, hint_inj, deadline, starve))
    # 每题"窗口占用"时长(启动->完成 中位)
    dur = []
    pend = {}
    for t, c in starts + [(None, None)]:
        pass
    # 简化：把 starts/completes 按时间合并，同 code 间隔
    ev = sorted([(t, 0, c) for t, c in starts if t] + [(t, 1, c) for t, c in completes if t])
    open_at = {}
    durs = []
    for t, kind, c in ev:
        if kind == 0: open_at.setdefault(c, []).append(t)
        elif open_at.get(c): durs.append((t - open_at[c].pop(0)).total_seconds())
    durs.sort()
    if durs:
        print('   窗口占用时长(s): 中位=%.0f p75=%.0f p95=%.0f max=%.0f (n=%d)' % (
            durs[len(durs)//2], durs[int(len(durs)*.75)], durs[int(len(durs)*.95)], durs[-1], len(durs)))
