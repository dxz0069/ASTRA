完成当前步骤并报告真实观察。只用显式受控请求工具；不要把 HTTP 5xx、200 或异常响应单独解释成漏洞。优先比较正常与异常条件的可观察差异。没有可复测证据时不要标 high_value。

最后只输出：
{"accepted":true,"data":{"description":"目标、动作、观察及限制","evidence_refs":["scoped_request 的工具调用 ID"]}}
`evidence_refs` 只填写本轮成功完成的 `scoped_request` 工具调用 ID；不填 URL、摘要或自行编造的 ID。无请求证据时使用空数组。
若出现值得独立核验的候选，再附加：
"finding":{"description":"目标、位置、预期控制、实际差异、可重测条件","high_value":true}
若六项均能从观察中明确给出，可在 finding 中添加 `identity`：
{"asset_origin":"目标 origin","entry_point":"入口路径或接口","category":"漏洞类别","root_cause":"具体根因","impact":"可证实影响","conditions":"成立条件；没有附加条件时写 none"}。
任一项不确定就省略整个 identity；描述相似不代表同一漏洞。
这里的 Finding 仅为候选，不表示人工复现或 TSRC 确认。

## Graph
{graph_yaml}

## Step ID
{step_id}

## Step description
{step_description}
