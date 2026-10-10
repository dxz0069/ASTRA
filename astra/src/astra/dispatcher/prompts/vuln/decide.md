只根据事实图规划下一步，不调用目标。优先选择低影响、短预算、可差分比较的独立步骤。重复线索不要重开；没有新证据的方向应关闭。高价值 Finding 仍是待核验线索，不能推出 complete。

若已有事实直接证明 goal，输出：
{"accepted":true,"data":{"complete":{"from":["f001"],"description":"事实如何证明 goal"}}}
否则输出：
{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"一个具体且在 scope 内的任务","expect":"预期可观察证据"}],"close_steps":[]}}
新步骤最多 {max_steps} 个；无 open steps 时至少给一个新步骤或明确关闭条件。只输出 JSON。

## Graph
{graph_yaml}

## Valid facts
{fact_ids}

## Open steps
{open_steps}
