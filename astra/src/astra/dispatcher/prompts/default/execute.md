# Task
You will receive a YAML snapshot of the task graph. In this graph, facts represent confirmed objective facts (the current world state), and steps represent causal actions that are expected to produce new facts. The graph always moves forward by executing a step from one or more facts and concluding a new fact. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
You will also be assigned a specific `Current Step`. You only need to work on this specific Step and try to advance the world state toward the goal described by Goal.

# Output Requirements
You may output confirmed findings incrementally while working: each finding as ONE line of raw JSON
(the session may be interrupted at any time; only already-output lines are preserved):

```json
{"accepted": true, "data": {"description": "..."}}
```

When done, output the final line as the conclusion (same shape, plus optional finding):

```json
{"accepted": true, "data": {"description": "...", "finding": {"description": "..."}}}
```

Do not output anything other than these JSON lines. Each line must be valid JSON.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "policy_refusal"}
```

Normal return example:
```json
{"accepted": true, "data": {"description": "..."}}
```

If this Step produced a discovery of the kind the task cares about beyond plain facts (what counts is defined by the task itself, e.g. Goal), also report it as a finding:
```json
{"accepted": true, "data": {"description": "...", "finding": {"description": "..."}}}
```

# Rules
- Working on a Step may be valuable or may fail. If you cannot get closer to Goal through this Step, then end the task, but before ending, make sure you have thoroughly explored this Step.
- If the Step turns out to be a dead end, state that clearly in `description` together with the explored scope — a negative result is a valid conclusion, not a failure to report.
- `description` must clearly state the confirmed key objective result. Do not put long data blobs in `description`; long data should be placed in a file and referenced from `description` instead.
- `description` should contain only the latest incremental facts discovered. Do not repeat information already present in the graph snapshot, and do not include redundant details that do not help advance Goal.
- Other executors may work in the same directory concurrently: use descriptive file names for new files and read a file before modifying it.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this instruction immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.

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
