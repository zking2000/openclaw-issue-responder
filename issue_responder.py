#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SCRIPT_DIR = Path(__file__).resolve().parent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def looks_like_github_token(value: str) -> bool:
    prefixes = ("ghp_", "github_pat_", "ghu_", "ghs_", "ghr_")
    return any(value.startswith(prefix) for prefix in prefixes)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config(config_path: Path) -> dict[str, Any]:
    raw = load_json(config_path, {})
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a JSON object: {config_path}")
    return raw


def resolve_state_path(config: dict[str, Any], config_path: Path) -> Path:
    state_value = config.get("runtime", {}).get("state_path", "state/runtime.json")
    path = Path(str(state_value)).expanduser()
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def tokenize(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-zA-Z0-9_./-]+", text.lower()) if len(token) >= 3}


def keyword_score(a: str, b: str) -> int:
    left = tokenize(a)
    right = tokenize(b)
    if not left or not right:
        return 0
    return len(left & right)


def count_cjk_characters(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def count_english_words(text: str) -> int:
    return len(re.findall(r"\b[a-zA-Z]{2,}\b", text))


def detect_reply_language(profile: RepoProfile, issue: dict[str, Any], comment: dict[str, Any] | None) -> str:
    samples = []
    if comment is not None:
        samples.append(normalize_text(comment.get("body")))
    samples.extend(
        [
            normalize_text(issue.get("title")),
            normalize_text(issue.get("body")),
        ]
    )
    text = "\n".join(part for part in samples if part)
    if not text:
        return profile.language
    cjk_count = count_cjk_characters(text)
    english_count = count_english_words(text)
    if cjk_count > 0 and english_count == 0:
        return "zh-CN"
    if english_count > 0 and cjk_count == 0:
        return "en"
    if cjk_count >= english_count * 2 and cjk_count > 0:
        return "zh-CN"
    if english_count >= max(cjk_count * 2, 2):
        return "en"
    return profile.language


@dataclass
class RepoProfile:
    profile_id: str
    repo: str
    workspace_path: Path
    openclaw_agent_id: str
    language: str
    auto_publish: bool
    labels_allowlist: list[str]
    context_files: list[str]
    issue_fetch_limit: int
    history_limit: int
    max_context_chars_per_file: int
    max_reply_chars: int
    skip_authors: set[str]

    @classmethod
    def from_dict(cls, item: dict[str, Any], config_dir: Path) -> "RepoProfile":
        workspace_value = Path(str(item.get("workspace_path", "."))).expanduser()
        if not workspace_value.is_absolute():
            workspace_value = (config_dir / workspace_value).resolve()
        return cls(
            profile_id=str(item.get("id") or item.get("repo") or "default"),
            repo=str(item.get("repo") or "").strip(),
            workspace_path=workspace_value,
            openclaw_agent_id=str(item.get("openclaw_agent_id") or "main").strip(),
            language=str(item.get("language") or "en").strip(),
            auto_publish=bool(item.get("auto_publish", False)),
            labels_allowlist=[str(label).strip().lower() for label in item.get("labels_allowlist", []) if str(label).strip()],
            context_files=[str(path).strip() for path in item.get("context_files", []) if str(path).strip()],
            issue_fetch_limit=max(int(item.get("issue_fetch_limit", 20)), 1),
            history_limit=max(int(item.get("history_limit", 12)), 1),
            max_context_chars_per_file=max(int(item.get("max_context_chars_per_file", 12000)), 1000),
            max_reply_chars=max(int(item.get("max_reply_chars", 2200)), 200),
            skip_authors={str(name).strip().lower() for name in item.get("skip_authors", []) if str(name).strip()},
        )


class GitHubClient:
    def __init__(self, config: dict[str, Any]) -> None:
        github_cfg = config.get("github", {})
        token = normalize_text(github_cfg.get("token"))
        token_env = normalize_text(github_cfg.get("token_env")) or "GITHUB_TOKEN"
        if not token:
            token = os.environ.get(token_env, "").strip()
        if not token and looks_like_github_token(token_env):
            # Backward compatibility for local configs that accidentally stored the raw token in token_env.
            token = token_env
        if not token:
            raise RuntimeError(
                "Missing GitHub token. Set github.token in config or provide it via "
                f"environment variable: {token_env}"
            )
        self.token = token
        self.api_base = str(github_cfg.get("api_base", "https://api.github.com")).rstrip("/")
        self.user_agent = str(github_cfg.get("user_agent", "openclaw-issue-responder/1.0"))
        self.timeout_seconds = int(config.get("runtime", {}).get("http_timeout_seconds", 20))

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_base}{path}"
        if params:
            query = parse.urlencode({key: value for key, value in params.items() if value is not None})
            if query:
                url = f"{url}?{query}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url=url, method=method.upper(), data=data, headers=headers)
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {exc.code} {method.upper()} {path}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"GitHub API request failed: {exc}") from exc
        if not body.strip():
            return None
        return json.loads(body)

    def fetch_recent_issues(self, repo: str, *, since: str | None, limit: int) -> list[dict[str, Any]]:
        payload = self.request_json(
            "GET",
            f"/repos/{repo}/issues",
            params={
                "state": "open",
                "sort": "created",
                "direction": "desc",
                "per_page": min(limit, 100),
                "since": since,
            },
        )
        return payload if isinstance(payload, list) else []

    def fetch_recent_comments(self, repo: str, *, since: str | None, limit: int) -> list[dict[str, Any]]:
        payload = self.request_json(
            "GET",
            f"/repos/{repo}/issues/comments",
            params={
                "sort": "created",
                "direction": "desc",
                "per_page": min(limit, 100),
                "since": since,
            },
        )
        return payload if isinstance(payload, list) else []

    def fetch_issue(self, repo: str, issue_number: int) -> dict[str, Any]:
        payload = self.request_json("GET", f"/repos/{repo}/issues/{issue_number}")
        return payload if isinstance(payload, dict) else {}

    def fetch_issue_by_url(self, issue_url: str) -> dict[str, Any]:
        path = issue_url.replace(self.api_base, "")
        payload = self.request_json("GET", path)
        return payload if isinstance(payload, dict) else {}

    def fetch_recent_history(self, repo: str, *, limit: int) -> list[dict[str, Any]]:
        payload = self.request_json(
            "GET",
            f"/repos/{repo}/issues",
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": min(limit, 100),
            },
        )
        return payload if isinstance(payload, list) else []

    def post_issue_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        payload = self.request_json(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            payload={"body": body},
        )
        return payload if isinstance(payload, dict) else {}


def is_bot_login(login: str, skip_authors: set[str]) -> bool:
    value = login.strip().lower()
    return not value or value.endswith("[bot]") or value in skip_authors


def has_allowed_label(issue: dict[str, Any], allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    labels = issue.get("labels", [])
    names = {str(item.get("name") or "").strip().lower() for item in labels if isinstance(item, dict)}
    return bool(names & set(allowlist))


def read_repo_context(profile: RepoProfile) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for rel_path in profile.context_files:
        file_path = (profile.workspace_path / rel_path).resolve()
        if not file_path.exists() or not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if len(text) > profile.max_context_chars_per_file:
            text = text[: profile.max_context_chars_per_file] + "\n... [truncated]"
        results.append({"path": rel_path, "content": text})
    return results


def build_related_history(
    issue: dict[str, Any],
    history_items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    current_title = normalize_text(issue.get("title"))
    current_body = normalize_text(issue.get("body"))
    target_text = f"{current_title}\n{current_body}"
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in history_items:
        if item.get("pull_request"):
            kind = "pull_request"
        else:
            kind = "issue"
        if item.get("number") == issue.get("number"):
            continue
        score = keyword_score(target_text, f"{normalize_text(item.get('title'))}\n{normalize_text(item.get('body'))}")
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                {
                    "kind": kind,
                    "number": item.get("number"),
                    "title": normalize_text(item.get("title")),
                    "state": normalize_text(item.get("state")),
                    "url": normalize_text(item.get("html_url")),
                    "updated_at": normalize_text(item.get("updated_at")),
                    "score": score,
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def build_prompt(
    profile: RepoProfile,
    reply_language: str,
    event_type: str,
    issue: dict[str, Any],
    comment: dict[str, Any] | None,
    repo_context: list[dict[str, str]],
    related_history: list[dict[str, Any]],
) -> str:
    event_payload = {
        "event_type": event_type,
        "repository": profile.repo,
        "language": reply_language,
        "default_language": profile.language,
        "issue": {
            "number": issue.get("number"),
            "title": normalize_text(issue.get("title")),
            "body": normalize_text(issue.get("body")),
            "url": normalize_text(issue.get("html_url")),
            "author": normalize_text(issue.get("user", {}).get("login")),
            "labels": [str(item.get("name") or "") for item in issue.get("labels", []) if isinstance(item, dict)],
        },
        "comment": None
        if comment is None
        else {
            "id": comment.get("id"),
            "author": normalize_text(comment.get("user", {}).get("login")),
            "body": normalize_text(comment.get("body")),
            "url": normalize_text(comment.get("html_url")),
        },
        "repo_context": repo_context,
        "related_history": related_history,
    }
    return "\n".join(
        [
            "You are the GitHub issue responder for this repository.",
            "Write a helpful, repository-specific reply for the issue or issue comment below.",
            "Use only the provided repository facts and related history.",
            "Do not invent commands, files, features, or guarantees that are not supported by the provided context.",
            "Prefer concrete guidance over generic support language.",
            "If evidence is incomplete, say what is unclear and ask for the smallest missing detail.",
            "Match the user's language when it is clearly Chinese or clearly English.",
            f"For this reply, write in {reply_language}.",
            f"Keep the final reply under {profile.max_reply_chars} characters.",
            "Do not use markdown code fences.",
            "If you reference files or commands, make them specific to this repository.",
            "",
            json.dumps(event_payload, ensure_ascii=False, indent=2),
        ]
    )


def extract_openclaw_text(payload: dict[str, Any]) -> str:
    result = payload.get("result", {})
    payloads = result.get("payloads", [])
    texts: list[str] = []
    for item in payloads:
        if not isinstance(item, dict):
            continue
        text = normalize_text(item.get("text"))
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def call_openclaw_agent(config: dict[str, Any], agent_id: str, prompt: str) -> str:
    openclaw_cfg = config.get("openclaw", {})
    command = [
        str(openclaw_cfg.get("bin", "openclaw")),
        "agent",
        "--agent",
        agent_id,
        "--message",
        prompt,
        "--json",
        "--thinking",
        str(openclaw_cfg.get("thinking", "minimal")),
        "--timeout",
        str(int(openclaw_cfg.get("timeout_seconds", 120))),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=int(openclaw_cfg.get("timeout_seconds", 120)) + 30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "openclaw agent failed"
        raise RuntimeError(detail)
    payload = json.loads(result.stdout)
    text = extract_openclaw_text(payload)
    if not text:
        raise RuntimeError("openclaw returned empty text")
    return text


def should_bootstrap(state: dict[str, Any], profile_id: str) -> bool:
    repo_state = state.setdefault("repositories", {}).setdefault(profile_id, {})
    return not bool(repo_state.get("last_cursor"))


def get_repo_state(state: dict[str, Any], profile_id: str) -> dict[str, Any]:
    repositories = state.setdefault("repositories", {})
    repo_state = repositories.setdefault(profile_id, {})
    repo_state.setdefault("processed", [])
    return repo_state


def mark_processed(repo_state: dict[str, Any], event_key: str) -> None:
    processed = [str(item) for item in repo_state.get("processed", []) if str(item)]
    processed.append(event_key)
    repo_state["processed"] = processed[-400:]


def is_processed(repo_state: dict[str, Any], event_key: str) -> bool:
    return event_key in {str(item) for item in repo_state.get("processed", [])}


def print_reply_preview(repo: str, issue_number: int, event_type: str, body: str) -> None:
    divider = "=" * 72
    print(divider)
    print(f"[dry-run] {repo} #{issue_number} ({event_type})")
    print(divider)
    print(body.strip())
    print()


def issue_ref(issue: dict[str, Any]) -> str:
    return f"issue #{issue.get('number')}"


def comment_ref(comment: dict[str, Any], issue: dict[str, Any] | None = None) -> str:
    if issue and issue.get("number") is not None:
        return f"comment {comment.get('id')} on issue #{issue.get('number')}"
    return f"comment {comment.get('id')}"


def log_skip(repo: str, target: str, reason: str) -> None:
    print(f"[skip] {repo} {target}: {reason}")


def process_issue_event(
    github: GitHubClient,
    config: dict[str, Any],
    profile: RepoProfile,
    repo_state: dict[str, Any],
    issue: dict[str, Any],
    *,
    dry_run: bool,
) -> bool:
    if issue.get("pull_request"):
        log_skip(profile.repo, issue_ref(issue), "pull request")
        return False
    login = normalize_text(issue.get("user", {}).get("login"))
    if is_bot_login(login, profile.skip_authors):
        log_skip(profile.repo, issue_ref(issue), f"author skipped ({login or 'unknown'})")
        return False
    if not has_allowed_label(issue, profile.labels_allowlist):
        label_names = [str(item.get("name") or "") for item in issue.get("labels", []) if isinstance(item, dict)]
        log_skip(
            profile.repo,
            issue_ref(issue),
            f"labels {label_names or ['<none>']} do not match allowlist {profile.labels_allowlist}",
        )
        return False
    event_key = f"issue:{issue.get('id')}"
    if is_processed(repo_state, event_key):
        log_skip(profile.repo, issue_ref(issue), f"already processed ({event_key})")
        return False
    history_items = github.fetch_recent_history(profile.repo, limit=profile.history_limit * 2)
    repo_context = read_repo_context(profile)
    related_history = build_related_history(issue, history_items, limit=profile.history_limit)
    reply_language = detect_reply_language(profile, issue, None)
    print(f"[reply] {profile.repo} {issue_ref(issue)}: language={reply_language}, source=issue_opened")
    prompt = build_prompt(profile, reply_language, "issue_opened", issue, None, repo_context, related_history)
    reply = call_openclaw_agent(config, profile.openclaw_agent_id, prompt)
    if dry_run or not profile.auto_publish:
        print_reply_preview(profile.repo, int(issue.get("number")), "issue_opened", reply)
    else:
        github.post_issue_comment(profile.repo, int(issue.get("number")), reply)
        print(f"Published reply to {profile.repo} #{issue.get('number')}")
    mark_processed(repo_state, event_key)
    repo_state["last_reply"] = {
        "issue_number": issue.get("number"),
        "event_type": "issue_opened",
        "at": utc_now_iso(),
    }
    return True


def process_comment_event(
    github: GitHubClient,
    config: dict[str, Any],
    profile: RepoProfile,
    repo_state: dict[str, Any],
    comment: dict[str, Any],
    *,
    dry_run: bool,
) -> bool:
    login = normalize_text(comment.get("user", {}).get("login"))
    if is_bot_login(login, profile.skip_authors):
        log_skip(profile.repo, comment_ref(comment), f"author skipped ({login or 'unknown'})")
        return False
    event_key = f"comment:{comment.get('id')}"
    if is_processed(repo_state, event_key):
        log_skip(profile.repo, comment_ref(comment), f"already processed ({event_key})")
        return False
    issue = github.fetch_issue_by_url(str(comment.get("issue_url") or ""))
    if not issue or issue.get("pull_request"):
        log_skip(profile.repo, comment_ref(comment, issue if issue else None), "missing issue or pull request")
        return False
    if not has_allowed_label(issue, profile.labels_allowlist):
        label_names = [str(item.get("name") or "") for item in issue.get("labels", []) if isinstance(item, dict)]
        log_skip(
            profile.repo,
            comment_ref(comment, issue),
            f"issue labels {label_names or ['<none>']} do not match allowlist {profile.labels_allowlist}",
        )
        return False
    history_items = github.fetch_recent_history(profile.repo, limit=profile.history_limit * 2)
    repo_context = read_repo_context(profile)
    related_history = build_related_history(issue, history_items, limit=profile.history_limit)
    reply_language = detect_reply_language(profile, issue, comment)
    print(f"[reply] {profile.repo} {comment_ref(comment, issue)}: language={reply_language}, source=issue_comment")
    prompt = build_prompt(profile, reply_language, "issue_comment", issue, comment, repo_context, related_history)
    reply = call_openclaw_agent(config, profile.openclaw_agent_id, prompt)
    if dry_run or not profile.auto_publish:
        print_reply_preview(profile.repo, int(issue.get("number")), "issue_comment", reply)
    else:
        github.post_issue_comment(profile.repo, int(issue.get("number")), reply)
        print(f"Published reply to {profile.repo} #{issue.get('number')}")
    mark_processed(repo_state, event_key)
    repo_state["last_reply"] = {
        "issue_number": issue.get("number"),
        "event_type": "issue_comment",
        "at": utc_now_iso(),
    }
    return True


def sort_by_created_at(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: normalize_text(item.get("created_at")))


def bootstrap_cursor(
    state: dict[str, Any],
    profile: RepoProfile,
    *,
    bootstrap_window_minutes: int | None,
) -> None:
    repo_state = get_repo_state(state, profile.profile_id)
    if repo_state.get("last_cursor"):
        return
    if bootstrap_window_minutes is None:
        repo_state["last_cursor"] = utc_now_iso()
        print(f"Bootstrapped {profile.repo} at current time. No historical events were processed.")
        return
    cursor = utc_now() - timedelta(minutes=max(bootstrap_window_minutes, 1))
    repo_state["last_cursor"] = cursor.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    print(f"Bootstrapped {profile.repo} with a {bootstrap_window_minutes}-minute lookback window.")


def run_poll_once(config_path: Path, *, dry_run: bool, bootstrap_window_minutes: int | None) -> int:
    config = load_config(config_path)
    state_path = resolve_state_path(config, config_path)
    state = load_json(state_path, {"repositories": {}})
    github = GitHubClient(config)
    config_dir = config_path.parent
    profiles = [
        RepoProfile.from_dict(item, config_dir)
        for item in config.get("repositories", [])
        if isinstance(item, dict) and item.get("enabled", True)
    ]
    if not profiles:
        raise RuntimeError("No enabled repositories found in config.")

    total_processed = 0
    for profile in profiles:
        repo_state = get_repo_state(state, profile.profile_id)
        if should_bootstrap(state, profile.profile_id):
            bootstrap_cursor(state, profile, bootstrap_window_minutes=bootstrap_window_minutes)
            continue

        cursor = normalize_text(repo_state.get("last_cursor")) or None
        print(f"[repo] {profile.repo}: polling since {cursor or '<start>'}")
        issues = github.fetch_recent_issues(profile.repo, since=cursor, limit=profile.issue_fetch_limit)
        comments = github.fetch_recent_comments(profile.repo, since=cursor, limit=profile.issue_fetch_limit)

        cursor_dt = parse_iso8601(cursor) if cursor else None
        fresh_issues = []
        for issue in issues:
            created_at = parse_iso8601(normalize_text(issue.get("created_at")))
            if created_at and cursor_dt and created_at <= cursor_dt:
                continue
            fresh_issues.append(issue)

        fresh_comments = []
        for comment in comments:
            created_at = parse_iso8601(normalize_text(comment.get("created_at")))
            if created_at and cursor_dt and created_at <= cursor_dt:
                continue
            fresh_comments.append(comment)

        print(
            f"[repo] {profile.repo}: fetched issues={len(issues)} comments={len(comments)} "
            f"fresh_issues={len(fresh_issues)} fresh_comments={len(fresh_comments)}"
        )

        for issue in sort_by_created_at(fresh_issues):
            if process_issue_event(github, config, profile, repo_state, issue, dry_run=dry_run):
                total_processed += 1

        for comment in sort_by_created_at(fresh_comments):
            if process_comment_event(github, config, profile, repo_state, comment, dry_run=dry_run):
                total_processed += 1

        repo_state["last_cursor"] = utc_now_iso()

    save_json(state_path, state)
    print(f"Completed poll cycle. Events handled: {total_processed}")
    return 0


def run_daemon(config_path: Path, *, dry_run: bool, bootstrap_window_minutes: int | None) -> int:
    config = load_config(config_path)
    poll_seconds = max(int(config.get("runtime", {}).get("poll_interval_seconds", 300)), 10)
    while True:
        try:
            run_poll_once(config_path, dry_run=dry_run, bootstrap_window_minutes=bootstrap_window_minutes)
        except Exception as exc:  # pragma: no cover - daemon guard
            print(f"[error] {exc}", file=sys.stderr)
        time.sleep(poll_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll GitHub issues and reply with OpenClaw.")
    parser.add_argument(
        "--config",
        default=str(SCRIPT_DIR / "config.local.json"),
        help="Path to the responder config file. Defaults to openclaw_issue_responder/config.local.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate replies without publishing them to GitHub.",
    )
    parser.add_argument(
        "--bootstrap-window-minutes",
        type=int,
        default=None,
        help="Optional lookback window for the first run. Without this flag, the first run only initializes the cursor.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("poll-once", help="Poll GitHub once and handle new issues/comments.")
    subparsers.add_parser("daemon", help="Run the GitHub poller continuously.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found: {config_path}. Copy config.sample.json to config.local.json and fill in your repo settings."
        )
    if args.command == "poll-once":
        return run_poll_once(
            config_path,
            dry_run=bool(args.dry_run),
            bootstrap_window_minutes=args.bootstrap_window_minutes,
        )
    if args.command == "daemon":
        return run_daemon(
            config_path,
            dry_run=bool(args.dry_run),
            bootstrap_window_minutes=args.bootstrap_window_minutes,
        )
    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
