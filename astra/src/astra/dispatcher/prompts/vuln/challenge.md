只审查事实图中的结论是否被可复核证据支持，不调用目标。候选 Finding 或 AI Strike 结论不能代替人工复现和 TSRC 确认。

仅输出 JSON：证据充分时 {"accepted":true,"data":{"verdict":"uphold"}}；缺少具体关键核验时 {"accepted":true,"data":{"verdict":"refute","reason":"未证明之处与所需检查"}}。

## Graph
{graph_yaml}

## Claim
{claim}

## Context
{claim_context}
