# -*- coding: utf-8 -*-
"""榜首 Cairn_X (agent 7082) 全量会话画像分析，数据源 dist/rivals/7082_all_sessions.json"""
import json, re, collections, statistics as st
from datetime import datetime, timezone

D = json.load(open(r'E:\study\项目\Cairn-main\dist\rivals\7082_all_sessions.json', encoding='utf-8'))
print('total sessions:', len(D))

def code(t):
    m = re.search(r'"unique_code":\s*"([^"]+)"', t or '')
    return m.group(1) if m else '(no-code)'

def ts(s):
    return datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

for x in D:
    x['_code'] = code(x['title'])
    x['_dur'] = (ts(x['last_active_at']) - ts(x['first_captured_at'])).total_seconds()
    x['_start'] = ts(x['first_captured_at'])

# ---- 1. 按题分布 ----
byc = collections.Counter(x['_code'] for x in D)
print('\n== 按题(unique_code)会话数 ==  题目数:', len(byc))
for c, n in byc.most_common():
    print('%-8s %4d' % (c, n))

# ---- 2. 时间线 ----
lo = min(x['_start'] for x in D); hi = max(x['_start'] for x in D)
print('\n== 时间线 == 首:', lo, '末:', hi, '跨度(h):', round((hi-lo).total_seconds()/3600,1))
hour = collections.Counter(x['_start'].strftime('%m-%d %H') for x in D)
for h in sorted(hour):
    print(h, hour[h], '#'*(hour[h]//5))

# ---- 3. event_count / 时长 / usage ----
ev = sorted(x['event_count'] for x in D)
dur = sorted(x['_dur'] for x in D)
def pct(a, p):
    return a[int(len(a)*p)] if len(a)*p < len(a) else a[-1]
print('\n== event_count == min %d p25 %d 中位 %d p75 %d p95 %d max %d 均值 %.1f' % (
    ev[0], pct(ev,.25), pct(ev,.5), pct(ev,.75), pct(ev,.95), ev[-1], sum(ev)/len(ev)))
print('== 会话时长(s) == min %d p25 %d 中位 %d p75 %d p95 %d max %d 均值 %.1f' % (
    dur[0], pct(dur,.25), pct(dur,.5), pct(dur,.75), pct(dur,.95), dur[-1], sum(dur)/len(dur)))
print('时长分桶:', collections.Counter('<30s' if d<30 else '30-60s' if d<60 else '1-2m' if d<120 else '2-5m' if d<300 else '5-10m' if d<600 else '>=10m' for d in dur))

tu = collections.defaultdict(int)
for x in D:
    for k, v in (x.get('total_usage') or {}).items():
        tu[k] += v or 0
tot_all = sum(tu.values())
print('\n== total_usage 汇总 ==', dict(tu))
print('cache_read 占全部token比: %.1f%%' % (100*tu['cache_read']/tot_all))
print('cache_read/(cache_read+input+output) = %.1f%%' % (100*tu['cache_read']/(tu['cache_read']+tu['input']+tu['output'])))
print('输出token合计: %d  输入(非cache)合计: %d  reasoning: %d' % (tu['output'], tu['input'], tu['reasoning']))
print('全会话轮次(call_count)合计:', sum(x.get('range_call_count') or 0 for x in D))

print('\n== closed_reason ==', collections.Counter(x['closed_reason'] for x in D))
print('== model ==', collections.Counter(x['model'] for x in D))
print('== group ==', collections.Counter(x['group_id'] for x in D))

# group 之间的差异（判定两组角色）
for g in (37435, 37436):
    sub = [x for x in D if x['group_id'] == g]
    e = sorted(x['event_count'] for x in sub); d = sorted(x['_dur'] for x in sub)
    u = sum((x.get('total_usage') or {}).get('cache_read', 0) for x in sub)
    print('group %d: n=%d event中位=%d 时长中位=%ds cache_read均=%.0f cr分布=%s' % (
        g, len(sub), pct(e,.5), pct(d,.5), u/len(sub),
        collections.Counter('<60s' if x['_dur']<60 else '1-3m' if x['_dur']<180 else '>=3m' for x in sub)))

# ---- 4. 每题的会话数/事件/时长 ----
print('\n== 每题统计 (题: 会话数/事件中位/时长中位s/cache_read均值) ==')
rows = []
for c in byc:
    sub = [x for x in D if x['_code'] == c]
    e = sorted(x['event_count'] for x in sub); d = sorted(x['_dur'] for x in sub)
    u = sum((x.get('total_usage') or {}).get('cache_read', 0) for x in sub)/len(sub)
    rows.append((c, len(sub), pct(e,.5), pct(d,.5), round(u)))
for r in sorted(rows, key=lambda r: -r[1])[:30]:
    print('%-8s n=%3d ev中位=%3d dur中位=%5ds cr均=%d' % r)
