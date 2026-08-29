# Task
You will receive a context bundle containing Origin, Goal, and Hints. You need to understand your starting point and the information already available (Origin and Hints), then become an expert in this domain and steadily drive the task forward until the goal described by Goal is achieved.

# Output Requirements
Work incrementally and submit facts as you confirm them. Every time you confirm a meaningful finding, output exactly ONE line of raw JSON immediately, then continue working. Each line must be valid JSON with no other text around it:

```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

When you have definitively achieved Goal, output the final line carrying both the last finding and the completion statement:

```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

When rejecting a task, return:
```json
{"accepted": false, "reason": "policy_refusal"}
```

# Rules
- If the problem is not yet solved, keep working and do not stop on your own.
- Submit each confirmed fact immediately as its own JSON line. Do not buffer findings until the end — the session may be interrupted at any time and only already-output lines are preserved. Prefer small, frequent, evidence-backed facts over one giant summary.
- `fact.description` must state one confirmed objective result. A negative result ("this path does not exist / this direction is exhausted", together with the explored scope) is an equally valid fact.
- Do not put long data blobs in `description`. Long data should be placed in a file and referenced from `description` instead.
- Other executors may work in the same directory concurrently: use descriptive file names for new files and read a file before modifying it.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this keep-working rule immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- Output `complete` only if Goal has already been definitively achieved in this session. If Goal is not yet achieved, do not output `complete`, do not summarize partial progress as completion, and keep working until a conclude-phase instruction replaces this task.
- `complete.description` should explain why the currently confirmed results are sufficient to prove that Goal has been achieved.

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
