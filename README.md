# Scrum Dispatcher

A single-file Python dispatcher that runs a small company of AI coding agents from a
[Nextcloud Deck](https://apps.nextcloud.com/apps/deck) board and Nextcloud Talk rooms.
Cards move through a Scrum flow, each step handled by an agent or a gate:

    To spec -> To do -> Doing -> To review -> To test -> Done

- **Workers** (Claude Code, OpenCode, Qwen and similar CLIs) pick up cards from *To do*, work on a per-card branch and move the card to review.
- **Review** is a separate agent; failed reviews go back to the coder.
- **Definition-of-Done gate**: a card only reaches *Done* when its evidence (tests, review, deploy, external test) is on the card.
- **External tester ("Muse")**: after review, cards go to *To test*. A black-box tester sees only a sanitized, public-behaviour brief (no code, paths, credentials). If a card has no `TEST-BRIEF`, a small model writes one, or decides the card has nothing user-visible and moves it straight to Done.
- **Restart recovery**: in-flight cards are saved to disk. After a restart or crash, WIP is snapshotted on the card branch and the card is requeued at the top of *To do* so a worker resumes it.
- Limits handling (Claude usage windows), WIP limits, daily and weekly reports, human approvals over Telegram.

Status: extracted from a private, working deployment. It is opinionated and not yet a polished product; expect to adapt it.

## Requirements

- Linux host, Python 3.10+, `pip install requests`
- A Nextcloud instance with the **Deck** and **Talk** apps, plus one Nextcloud user per agent (app passwords)
- One Deck board per project with stacks `To spec`, `To do`, `Doing`, `To review`, `To test`, `Done` (the dispatcher creates a missing `To test`)
- `git` and a checkout per project
- The agent CLIs you configure, e.g. [Claude Code](https://claude.com/claude-code) (`claude`) logged in; others optional
- Per-agent env files holding `NC_USER` / `NC_PASSWORD` (and API keys where needed). These are never committed
- Optional: a Telegram bridge for approvals, an external web tester, a systemd unit

## Setup

1. `cp company.example.json company.json` and fill in your Nextcloud URL, agents, projects (Talk room token, repo path, Deck board ids) and `muse_targets` (public URL a tester may visit per project).
2. Create the env files referenced under `agents.*.env` with the Nextcloud credentials.
3. Put the files in a directory and set `COMPANY_HOME` to it (default `/opt/company`).
4. Run `python3 dispatcher.py`, preferably as a systemd service. Restart only when idle.

`company.json`, state files, databases and secrets are git-ignored on purpose.

## Known limitations

- Some agent names, roles and paths in `dispatcher.py` come from the original deployment and are hard-coded (the agent roster text, `/root/...` PATH entries, Hermes/Telegram helpers). Moving them into config is the first cleanup task.
- Tests live in `tests/` (unittest); some need the original layout.

## License

MIT, see `LICENSE`.

## Configuration (.env)

Deployment-specific values live in a git-ignored `.env` (copy `.env.example`) in `COMPANY_HOME`
(default: the directory of `dispatcher.py`). Real environment variables win. Project and board
settings stay in the git-ignored `company.json` (see `company.example.json`); credentials live in `secrets/`.
