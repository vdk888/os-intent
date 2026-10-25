Onboarding, step 1 of 2: read, infer, draft, and list what you could not infer.

What the person opted into (read-only):
{{sources}}

Take the time to know this person well: expect 40-100 tool calls. Bash runs ONLY the Mail / Notion commands below (no ls, cd, cat, find): list folders with Glob, search with Grep.

Steps:
0. Mail, when listed: run its command FIRST — Mail is read in the background (about 1 s per message) and the command returns at once with the pages ready so far.
1. Read {{context}}/sources.md (installed apps, activity, sites: names and counts) and every CLAUDE.md copy.
2. Memory folders: Glob each one; when it has a MEMORY.md index, Read it, then FOLLOW its links — Read every linked memory about the person, their role, their preferences, their projects and the people they work with (a `[title](file.md)` or `[[name]]` link is a file in the same folder).
3. Wiki and other roots: Read the entry points first — index.md, every hot.md (Glob `**/hot.md`) and any MEMORY.md — then FOLLOW their links: Read the linked pages about this person, their work, their clients, their projects, their recurring tasks and their counterparts, and the pages those pages link to when they are about the same. Grep for names and terms you met to find what no index links.
   Minimum before you write anything: Read at least {{min_reads}} files across the memory folders and the wiki / other roots together (indexes, hot.md and the linked pages count; CLAUDE.md copies, sources.md, Mail and Notion pages do not), or all of their files when there are fewer. Keep a count; do not stop at the indexes.
4. Notion, when listed: run its command once and read the pages it lists. Mail: read the pages ready, then run the Mail command again until it says "done", and read every page (who writes to them, about what, what they answer, what is automated noise).
5. Write into {{out}}:
   - profile.yaml and starter_catalogue.yaml (the formats in your instructions) — your best draft now;
   - notes.md: your working notes for step 2 (what you inferred and from which kind of source, what stays uncertain), at most 60 lines, in your own words, no copied text;
   - questions.json: {"questions": ["…"]}, at most {{max_questions}} short questions, ONLY about what you could not infer and that would most change the catalogue (which tool comes right after another, which recurring task has a fixed time, who exactly gets which request, what is noise). In the person's language. [] if nothing is missing. Never ask for passwords, documents or private content.
Then stop: answer with one short line, with the number of memory / wiki files you read.

Team list (for agent.ask):
{{roster}}
