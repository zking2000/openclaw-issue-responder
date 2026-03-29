# OpenClaw Issue Responder

A small GitHub issue auto-responder built around `OpenClaw`.

It polls one or more repositories, gathers repository-specific context from local files, asks an `OpenClaw` agent to draft a reply, and can optionally publish that reply back to GitHub.

## Features

- Monitors new GitHub issues and issue comments
- Supports multiple repositories from one shared config
- Reads repo-specific context files from each local checkout
- Pulls related issue history to make replies less generic
- Can reply in Chinese or English based on the incoming issue/comment
- Supports `dry-run` preview before enabling auto-publish
- Stores cursors and processed events locally to avoid duplicate replies

## How It Works

1. Poll GitHub for newly created issues and issue comments.
2. Load the matching repository profile from `config.local.json`.
3. Read configured context files from that repository's local workspace.
4. Build a structured prompt with issue data and related history.
5. Call `openclaw agent` to generate a response.
6. Either print the reply (`--dry-run`) or publish it back to GitHub.

## Project Layout

- `issue_responder.py`: main CLI entrypoint
- `config.sample.json`: public-safe sample config
- `state/runtime.json`: local runtime cursor and processed-event state
- `.gitignore`: keeps local config and runtime state out of git

## Quick Start

1. Clone this project and create a local config:

```bash
cp config.sample.json config.local.json
```

2. Fill in at least:

- `github.token` or `github.token_env`
- `repositories[].repo`
- `repositories[].workspace_path`
- `repositories[].openclaw_agent_id`
- `repositories[].context_files`

3. Run one dry-run cycle:

```bash
python3 ./issue_responder.py --dry-run poll-once
```

4. If you want the first run to include recent history:

```bash
python3 ./issue_responder.py --dry-run --bootstrap-window-minutes 60 poll-once
```

5. After you trust the replies, turn on `auto_publish`.

## Configuration

The responder uses one shared codebase with one or more repository profiles. Each profile points to a GitHub repository plus a local checkout for context files.

Important fields in `config.sample.json`:

- `github.token`: optional GitHub token stored directly in local config
- `github.token_env`: environment variable name for the GitHub token
- `openclaw.bin`: path to the `openclaw` binary
- `repositories[].repo`: GitHub repository in `owner/name` format
- `repositories[].workspace_path`: local checkout path for repo context
- `repositories[].openclaw_agent_id`: `OpenClaw` agent used to draft replies
- `repositories[].context_files`: high-signal files injected into the prompt
- `repositories[].auto_publish`: whether generated replies are posted to GitHub
- `repositories[].labels_allowlist`: optional label filter; `[]` means reply to all eligible issues

Example token setup with an environment variable:

```bash
export GITHUB_TOKEN=your_token_here
```

## Running Modes

Run one poll cycle:

```bash
python3 ./issue_responder.py poll-once
```

Run one dry-run cycle:

```bash
python3 ./issue_responder.py --dry-run poll-once
```

Run continuously in a local loop:

```bash
python3 ./issue_responder.py daemon
```

Schedule `poll-once` every 4 hours with `OpenClaw cron`:

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

## Defaults And Safeguards

- The first run initializes the cursor and skips historical events unless you pass `--bootstrap-window-minutes`
- `auto_publish` defaults to `false`
- `labels_allowlist: []` means all eligible issues can be replied to
- processed issue/comment events are deduplicated in `state/runtime.json`
- bot accounts are skipped automatically
- repository context is truncated per file to avoid overly large prompts

## When To Use This

This project is a good fit if you want:

- lightweight issue auto-replies without standing up a webhook server
- one responder that can monitor multiple repositories
- repo-aware responses grounded in local files instead of generic canned text
- a simple polling workflow that is easy to run locally or through `OpenClaw cron`

## Notes

- This project currently uses polling, not GitHub webhooks
- Replies are only as good as the selected context files and agent behavior
- If you publish with your personal GitHub token, comments will appear as your own account
