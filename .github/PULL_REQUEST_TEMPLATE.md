<!--
Prompts, not a checklist — replace each line with your answer and delete the ones that don't
apply. There is nothing here to tick.
-->

What changed, and why.

Anything a reviewer has to do by hand: schema SQL, new env vars, a backfill script.

Does this break the API contract? Then the title carries `!`, and say whether the web or iOS client
needs a matching change. Does an existing install have to do something by hand? That is recorded in
opendiving/opendiving, not here.

Did something non-obvious bite you? That is a `DECISIONS.md` section, and it belongs in this PR.

Touches the schema? Every schema change ships an Alembic revision — name the one this PR adds.
