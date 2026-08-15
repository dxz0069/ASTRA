# Task
You will receive a context bundle containing Origin, Goal, and Hints. You need to understand your starting point and the information already available (Origin and Hints), then become an expert in this domain and steadily drive the task forward until the goal described by Goal is achieved.

# Output Requirements
Work incrementally. Every time you confirm a meaningful finding (open port, service fingerprint, credential, vulnerability, flag, permission, etc.), output exactly ONE line of raw JSON immediately, then continue working. Each line must be valid JSON with no other text around it:

```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

When you have definitively achieved Goal, output the final line carrying both the last finding and the completion statement:

```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

You may output many fact lines over time — every one of them is preserved even if the session is interrupted later, so prefer small, frequent, evidence-backed facts over one giant summary.

When rejecting a task, return:
```json
{"accepted": false, "reason": "policy_refusal"}
```

# Rules
- If the problem is not yet solved, keep working and do not stop on your own.
- If you later receive a conclude-phase instruction in the same session, that newer conclude instruction overrides this keep-working rule immediately. In conclude phase, you must stop exploring, stop waiting, stop running or planning further actions, and return the required summary JSON right away.
- Output `complete` only if Goal has already been definitively achieved in this session. If Goal is not yet achieved, do not output `complete`, do not summarize partial progress as completion, and keep working until a conclude-phase instruction replaces this task.
- `fact.description` must clearly state a confirmed key objective result. For example, in a CTF scenario, it may include a confirmed port, fingerprint, credential, flag, shell, privilege proof, key exploitation result, and similar evidence.
- `complete.description` should explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- Do not put long data blobs in `description`. Long data should be placed in a file and referenced from `description` instead.
- Each fact line must be a separate valid JSON object on its own line. Do not wrap multiple lines in an array or add markdown code fences around the stream.

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
