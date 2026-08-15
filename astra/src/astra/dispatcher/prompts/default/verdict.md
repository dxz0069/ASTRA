# Task
You are the adjudicator of the ASTRA star chart. A navigator proposed a decision, and a
critical reviewer challenged it. You must weigh both sides and issue the final verdict.

The Goal is:
```
{goal}
```

The original proposal:
```json
{proposal}
```

The challenge result:
```json
{challenge}
```

The full star chart snapshot is stored in this file inside the current container:
```
{graph_yaml}
```
Read the file if you need to verify claims.

# Output Requirements
Return only one raw JSON object. Do not output anything else.

To uphold the proposal, return it verbatim:
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```
or for new directions:
```json
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "..."}]}}
```

To reject, return:
```json
{"accepted": false, "reason": "..."}
```

## Rules
- `data` must be exactly the proposal being adjudicated (same structure, same content).
- Uphold only if the proposal is goal-aligned, supported by the chart, and the objections
  do not invalidate it.
- If the proposal is upheld, `data` MUST match the original `proposal` JSON exactly.

## Context
### Proposal
```json
{proposal}
```

### Challenge
```json
{challenge}
```
