# Contributing

Thanks for helping. OS-intent is a small macOS app: a Python package (`oi/`) and a few Swift packages (`hud/`,
`menubar/`, `settings/`, `ocr/`, `watchers/oi-watcher/`).

## Set up and test

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                       # Python tests
(cd hud && swift test)                    # Swift tests, per package you touch (hud, menubar, settings, ...)
```

With the Command Line Tools only (no Xcode), `swift test` may not find the swift-testing macros; point it at them:
`swift test -Xswiftc -plugin-path -Xswiftc /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing`.

There is no CI on this repository: run the tests locally and say in the pull request what you ran and the result.
Tests use synthetic data only and never send, open or capture anything for real; keep it that way.

## Pull requests

- One topic per pull request, the smallest change that does the job; no unrelated refactors.
- Add or update a test for the behaviour you change.
- Anything that leaves the Mac goes through the outward gate (`oi/gate.py`), and the privacy table in
  `README.md` / `README.fr.md` stays true: update it when you change what is read or sent.
- Never commit secrets, real names, real mail addresses or personal data: fixtures use fictional people and
  `example.com` / `*.example` domains.
- Security issues: see [SECURITY.md](SECURITY.md), never a public issue.

By contributing you agree that your contribution is licensed under the MIT License of this repository.
