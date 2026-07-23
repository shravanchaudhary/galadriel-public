# Jobs

Job cookbooks contain the repeatable steps for recurring work. The Replika reads the
matching cookbook before starting a job; `config/JOBS.md` decides which jobs are active
and when they run.

Create one Markdown file per job. Include its goal, prerequisites, ordered steps,
approval boundaries, success evidence, and blocker behavior. Do not put personal
history or credentials in a cookbook.
