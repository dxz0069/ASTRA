# -*- coding: utf-8 -*-
"""从 local-run*.log 提取真实工作窗口(平台start成功->窗口完成)、首次正确flag延迟、卡题分布"""
import re, glob, collections
from datetime import datetime

def ts(s): return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
files = sorted(glob.glob(r'E:\study\项目\Cairn-main\dist\local-run*.log'))
all_engaged = []          # (file, code, dur_s)
first_ok = []             # (file, code, minutes-from-run-start)
for f in files:
    lines = open(f, encoding='utf-8', errors='replace').read().splitlines()
    run_t0 = None; ok_starts = {}; done_ev = []; ok_flags = []
    for L in lines:
        m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', L)
        if not m: continue
        t = ts(m.group(1))
        if run_t0 is None: run_t0 = t
        if 'challenges/start' in L and '"HTTP/1.1 200 OK"' in L:
            c = re.search(r'unique_code=(\S+?)\s*"', L)
            if c: ok_starts.setdefault(c.group(1), []).append(t)
        elif '并行窗口完成题目' in L:
            c = re.search(r'code=(\S+)', L)
            if c: done_ev.append((t, c.group(1)))
        elif 'flag submitted' in L and 'correct=True' in L:
            c = re.search(r'code=(\S+)', L)
            if c: ok_flags.append((t, c.group(1)))
    # engaged windows: 每个成功start与其后最近一次同code完成事件配对(仅计正时长)
    used = set()
    flat = []
    for c, lst in ok_starts.items():
        for t in lst:
            flat.append((t, c))
    for t0, c in sorted(flat):
        for i, (t1, c1) in enumerate(done_ev):
            if i not in used and c1 == c and t1 >= t0:
                used.add(i)
                all_engaged.append((f.split('local-')[-1].replace('.log',''), c, (t1-t0).total_seconds()/60))
                break
    for t, c in ok_flags:
        first_ok.append((f.split('local-')[-1].replace('.log',''), c, (t-run_t0).total_seconds()/60))

print('== 真实工作窗口 (start成功->窗口完成) n=%d ==' % len(all_engaged))
d = sorted(x[2] for x in all_engaged)
if d:
    print('时长(分): 中位=%.1f p75=%.1f p95=%.1f max=%.1f' % (d[len(d)//2], d[int(len(d)*.75)], d[int(len(d)*.95)], d[-1]))
bycode = collections.defaultdict(float)
for _, c, m in all_engaged: bycode[c] += m
top = sorted(bycode.items(), key=lambda kv: -kv[1])[:15]
print('累计投入最多的题(分):', [(c, round(m)) for c, m in top])
print('有工作窗口的题数:', len(bycode), '累计工作时长(分):', round(sum(bycode.values())))
print()
print('== correct=True 首次出现延迟(相对各run起始,分) n=%d ==' % len(first_ok))
for f, c, m in first_ok:
    print('%-6s %-6s +%.1f分' % (f, c, m))
