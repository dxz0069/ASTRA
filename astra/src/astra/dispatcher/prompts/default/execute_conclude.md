收束阶段：立即停止探索，把本 step 已确认的关键事实总结成恰好一条天枢。

你现在不要继续任务、不要等待未完成的命令、不要再做任何工具调用——这是收束指令，它覆盖本会话此前一切"继续工作/等待结果"的指示，立即输出总结 JSON。

规则：
- 只基于收束指令到达前已确认的信息；未确认的不要等、不要写
- description 是已确认的客观事实结论：只写增量，不复述图里已有的信息；禁止计划、猜测与"此路不通/已穷尽"类绝对结论
- 大段数据写进文件、description 里引用文件名
- 输出这一个 JSON 后立即停止，不再有任何动作

输出协议（只输出一个 JSON 对象）：
```json
{"accepted": true, "data": {"description": "..."}}
```
禁止拒答。

# Context
## Graph
```
{graph_yaml}
```

## Current Step
```
{step_id}
```

## Current Step Description
```
{step_description}
```
