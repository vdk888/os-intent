# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability** (GitHub private vulnerability reporting / security advisories). Do not open a public
issue, pull request or discussion for a vulnerability.

Include what you found, the steps to reproduce it, the version or commit, and the impact you expect. We aim to
acknowledge a report within a few working days and will keep you informed while a fix is prepared. Once it is
fixed, we publish an advisory and credit you if you wish.

## Scope and threat model

OS-intent runs on your own Mac. In scope:

- anything that runs code, opens an app, a URL or a file, or sends a message without the user's confirmation
  (the outward gate, `oi/gate.py`, must stand in front of every action that leaves the Mac);
- untrusted content (mail bodies, web pages, screenshot text, messages, notes) that can steer an action through a
  model answer (prompt injection reaching a deeplink, a file open, a page press, a send);
- data that leaves the Mac when it should not: Claude calls (off by default, opt-in in Settings), the optional
  dashboard connector, the relay command;
- local files and sockets readable by other users (`~/.local/share/os-intent` is 0700, its files 0600, the HUD
  socket 0600);
- secrets handling (tokens are read from the macOS Keychain or the environment and must never be logged, written
  or passed in argv).

Out of scope: issues that need an attacker who already runs code as your user, local services that are
unauthenticated by design and bound to 127.0.0.1 (ActivityWatch, the optional Laya server, Ollama), and
third-party tools OS-intent calls (Claude Code, `gh`).

## Supported versions

Only the latest release and `main` receive security fixes.
