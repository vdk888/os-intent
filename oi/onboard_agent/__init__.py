"""W22b: the agentic `oi onboard`. One headless Claude Code agent (`claude -p`, the subscription seat the
oi/claude_actions.py way) READS the person's own context — Claude memory on this Mac, the wiki or any memory root
they point to, the last mails (content excerpts), the Notion desktop cache, the existing source summaries — and
writes profile.yaml + starter_catalogue.yaml, then validated by oi/onboard.py's validators before install.

The user: "it should read email topics and content … the onboarding should be with an
actual claude reading the claude memory files on this pc + my wiki or any pointed custom memory system. it should
be power by claude, not deterministic". The titles-only design of W22 stays as `oi onboard --offline`.

  readers.py  settings (`onboarding.*`), the memory roots, Mail (AppleScript) and Notion (local SQLite) readers;
  tools.py    the two CLI tools the agent may run (exact, argument-free Bash commands): oi-mail, oi-notion;
  agent.py    the sandboxed `claude -p` command, the two agent phases, the session (opening screen, questions,
              last edit) and the validation + install.
"""
