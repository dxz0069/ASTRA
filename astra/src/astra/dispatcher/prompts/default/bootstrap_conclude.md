收束阶段：立即停止探索，把本轮已确认的关键发现总结输出。

你现在不要继续任务、不要等待未完成的命令、不要再做任何工具调用——这是收束指令，它覆盖本会话此前一切"继续工作/等待结果"的指示，立即输出总结 JSON。

规则：
- 只基于收束指令到达前已确认的信息；未确认的不要等、不要写
- fact.description 是已确认的客观事实：只写增量发现；大段数据写文件、引用文件名；禁止猜测与"此路不通/已穷尽"类绝对结论
- 若本轮已确实拿到足以证明 goal 达成的结果，附带 complete 并说明理由；否则不要输出 complete
- 输出这一个 JSON 后立即停止

输出协议（只输出一个 JSON 对象）：
```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```
或附完成：
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "为何足以证明 goal 达成"}}}
```
禁止拒答。

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```
{hints}
```
