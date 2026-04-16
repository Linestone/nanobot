Compare conversation history against current memory files. Also scan memory files for stale content — even if not mentioned in history.

Output one line per finding:
[FILE] atomic fact (not already in memory)
[FILE-REMOVE] reason for removal
[SKILL] kebab-case-name: one-line description of the reusable pattern
[TASK:<task_id>] atomic fact specific to that task (not already in task memory)

Files: USER (identity, preferences), SOUL (bot behavior, tone), MEMORY (knowledge, project context)
Task memory: task-specific facts, requirements, or progress tied to a particular task

Rules:
- Atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [USER] location is Tokyo, not Osaka
- Capture confirmed approaches the user validated
- Task facts: only output [TASK:<id>] when the fact is clearly scoped to that task and would not be useful outside of it
- Global facts: output [USER]/[MEMORY] for preferences, system info, or cross-task knowledge

Staleness — flag for [FILE-REMOVE]:
- Time-sensitive data older than 14 days: weather, daily status, one-time meetings, passed events
- Completed one-time tasks: triage, one-time reviews, finished research, resolved incidents
- Resolved tracking: merged/closed PRs, fixed issues, completed migrations
- Detailed incident info after 14 days — reduce to one-line summary
- Superseded: approaches replaced by newer solutions, deprecated dependencies

Skill discovery — flag [SKILL] when ALL of these are true:
- A specific, repeatable workflow appeared 2+ times in the conversation history
- It involves clear steps (not vague preferences like "likes concise answers")
- It is substantial enough to warrant its own instruction set (not trivial like "read a file")
- Do not worry about duplicates — the next phase will check against existing skills

Do not add: current weather, transient status, temporary errors, conversational filler.

[SKIP] if nothing needs updating.
