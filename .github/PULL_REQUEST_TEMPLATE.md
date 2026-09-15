<!--
Prompts, not a checklist — replace each line with your answer and delete the ones that don't
apply. There is nothing here to tick.
-->

What changed, and why.

Anything a reviewer has to do by hand: schema SQL, new env vars, a backfill script.

Does this change the API contract? Then say whether the web or iOS client needs a matching change.

Did something non-obvious bite you? A one-line comment at the site, or a short `DECISIONS.md` section
if the code cannot carry the reason (the bar is in `AGENTS.md`).

Touches the schema? Every schema change ships an Alembic revision — name the one this PR adds.
