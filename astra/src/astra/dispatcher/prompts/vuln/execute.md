完成当前步骤并报告真实观察。只用显式受控请求工具；不要把 HTTP 5xx、200 或异常响应单独解释成漏洞。优先比较正常与异常条件的可观察差异。没有可复测证据时不要标 high_value。

最后只输出：
{"accepted":true,"data":{"description":"目标、动作、观察、证据哈希及限制"}}
若出现值得独立核验的候选，再附加：
"finding":{"description":"目标、位置、预期控制、实际差异、可重测条件及证据哈希","high_value":true}
这里的 Finding 仅为候选，不表示人工复现或 TSRC 确认。

## Graph
{graph_yaml}

## Step ID
{step_id}

## Step description
{step_description}
