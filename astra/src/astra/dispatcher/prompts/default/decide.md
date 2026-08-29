# Task
You will receive a YAML snapshot of the task graph. In this graph, facts represent confirmed objective facts (the current world state), goals represent the completion condition of the project (the termination condition of the search), and steps represent causal actions that are expected to produce new facts from existing ones. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
You need to decide:
1. Whether the current facts already satisfy Goal (the search may terminate)
2. If not, which steps should be added, closed, or replaced next

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

If Goal has been satisfied, return:
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```

If Goal has not been satisfied, return the next batch of graph operations. Every field is optional; an empty `data` object means no change for now:
```json
{"accepted": true, "data": {"steps": [{"from": ["f001"], "description": "...", "expect": "..."}], "close_steps": [{"id": "i001", "reason": "..."}], "subgoals": ["..."], "drop_subgoals": ["sg001"]}}
```

# Rules
- First determine whether the facts already satisfy Goal. If they do, `data.complete.from` must come from `Valid facts`, and `data.complete.description` must explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- Each new step is a causal action: `description` says what to do; `expect` says which new fact it is expected to produce. A step may originate from multiple facts.
- When proposing new steps, propose at most {max_steps} high-value and non-overlapping directions. Each step should be an independent, parallelizable path. Do not be overly broad, do not output redundant details that do not help advance Goal, and do not preset the whole execution route.
- If `Open Steps` is empty, you must propose new steps.
- Close a step (with a reason) when it is exhausted or clearly outperformed by a new direction. Closed steps stay on the graph as a record — do not propose a new step that reopens a closed direction.
- Each open step carries its dispatch count and last heartbeat. A step dispatched many times without producing facts is low-yield: close it or propose a replacement instead of continuing on it.
- Prefer parallel independent directions over a single deep chain; switch early when a direction stops producing.
- Sub goals are staged milestones toward Goal. Add one when progress warrants a checkpoint; drop it when it no longer applies. Do not accumulate stale sub goals.
- Different steps should cover different dimensions and avoid duplication or heavy overlap.

# Context
### Graph
```
{graph_yaml}
```

### Valid facts
```
{fact_ids}
```

### Open Steps
```
{open_steps}
```
