# OpenClaw Issue Responder

This directory is intentionally isolated from the existing ETH watcher code.
All GitHub issue auto-reply logic lives here so it can evolve independently.

## What It Does

- Polls GitHub for newly created issues and issue comments
- Reads repository-specific context files from the configured workspace
- Pulls recent issue/PR history to make replies more repository-aware
- Calls `openclaw agent` to generate a reply
- Optionally publishes the reply back to GitHub

## Directory Layout

- `issue_responder.py`: main CLI entrypoint
- `config.sample.json`: sample multi-repo config
- `state/runtime.json`: runtime cursor and processed-event state
- `.gitignore`: keeps local config and runtime state out of git

## Why This Is Separate

The existing repository is focused on ETH analysis and OpenClaw iMessage flows.
This directory keeps GitHub issue automation physically separate so you can:

- reuse the responder across repositories
- keep GitHub bot logic out of the trading codepath
- version the responder independently if needed

## Config Model

The responder uses one shared codebase with one or more repository profiles.
Each profile can point to a different repository and workspace path.

Key fields in `config.sample.json`:

- `github.token`: optional GitHub token stored directly in local config
- `github.token_env`: environment variable that stores the GitHub token
- `openclaw.bin`: path to the `openclaw` binary
- `repositories[].repo`: GitHub repository in `owner/name` format
- `repositories[].workspace_path`: local path to the repository checkout
- `repositories[].openclaw_agent_id`: dedicated OpenClaw agent for that repo
- `repositories[].context_files`: high-signal files used as reply context
- `repositories[].auto_publish`: whether replies should be posted automatically

## First-Time Setup

1. Create a local config:

```bash
cd /path/to/openclaw-issue-responder
cp config.sample.json config.local.json
```

2. Fill in:

- `repositories[].repo`
- `repositories[].openclaw_agent_id`
- `repositories[].workspace_path`
- `repositories[].context_files`

3. Configure a GitHub token using either approach:

- Preferred for local-only setup: set `github.token` in `config.local.json`
- Alternative: export the environment variable named by `github.token_env`

Environment variable example:

```bash
export GITHUB_TOKEN=your_token_here
```

## Usage

Dry run one poll cycle:

```bash
python3 ./issue_responder.py --dry-run poll-once
```

Bootstrap the first run with a lookback window:

```bash
python3 ./issue_responder.py --dry-run --bootstrap-window-minutes 60 poll-once
```

Run continuously:

```bash
python3 ./issue_responder.py daemon
```

Schedule one poll every 4 hours with OpenClaw cron:

```bash
openclaw cron add \
  --name github-issue-responder-4h \
  --agent main \
  --every 4h \
  --message "Use the exec tool exactly once. Run this command on the gateway host and do nothing else: /bin/zsh -lc 'python3 /path/to/openclaw-issue-responder/issue_responder.py --config /path/to/openclaw-issue-responder/config.local.json poll-once'. After the command finishes, emit one short plain-text status line with the exit code and the key stdout or stderr result." \
  --thinking off \
  --light-context \
  --no-deliver
```

## Safe Defaults

- On the first run, the responder does not reply to historical issues unless you pass `--bootstrap-window-minutes`
- `auto_publish` defaults to `false`
- `labels_allowlist: []` means all issues are eligible for reply
- processed issue/comment events are deduplicated in `state/runtime.json`
- bot accounts are skipped automatically

## Suggested Next Step

After you confirm the flow in `--dry-run`, turn on `auto_publish` for the target repository profile and schedule `poll-once` with `OpenClaw cron`.
