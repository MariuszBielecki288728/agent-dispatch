"""Configuration loading and fail-fast validation.

Contract (from the approved design, ``docs/architecture.md`` §5):

1. **Allowlist.** Any repository not listed under ``[repos]`` is refused before
   any API call is made.
2. **No silent substitution.** An unknown driver, an unsupported effort, an
   unexpected permission flag or an inconsistent credential-helper pair is a
   startup error. The worker never falls back to a different paid model.
3. **State lives outside target repositories.** The state database, run logs,
   worktree root and lock file must not sit inside a configured repository or a
   future orchestrator-managed worktree, or ``git add -A`` could sweep
   orchestrator files into a commit.

Validation is performed against the approved ``config/config.schema.json``; this
module adds only the cross-field rules a schema cannot express.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schema as schema_mod
from .util import expand_path, is_repo_slug, is_within

DEFAULT_CONFIG_PATHS = (
    "~/.config/agent-dispatch/config.toml",
    "~/agent-dispatch/config.toml",
)

#: Helpers that would authenticate as something other than the approved wrapper.
_FORBIDDEN_HELPER_TOKENS = ("gh auth git-credential", "/usr/bin/gh ")


class ConfigError(Exception):
    """Configuration is missing, unreadable, or violates the approved contract."""


@dataclass(frozen=True)
class RuntimeConfig:
    driver: str
    model: str
    effort: str | None
    permission_mode: str
    permission_flag: str
    max_turns: int


@dataclass(frozen=True)
class RepoConfig:
    slug: str
    path: Path
    base_branch: str
    agents_file: str
    runtime: RuntimeConfig


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: int
    max_concurrent_tasks: int
    run_timeout_seconds: int
    max_attempts: int
    worktree_root: Path
    state_db: Path
    run_log_dir: Path
    run_log_keep: int
    lock_file: Path
    commandcode_path: str | None
    write_repo_local_credentials: bool
    commit_identity_name: str
    commit_identity_email: str


@dataclass(frozen=True)
class GithubConfig:
    command: str
    credential_helper: str
    credential_helper_reset: str
    fail_fast_on_denied_repo: bool
    trigger_label: str
    review_handoff_label: str


@dataclass(frozen=True)
class Config:
    worker: WorkerConfig
    github: GithubConfig
    repos: dict[str, RepoConfig]
    source_path: Path
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def repo(self, slug: str) -> RepoConfig:
        """Return the configuration for an allowlisted repo or fail loudly."""
        if not is_repo_slug(slug):
            raise ConfigError(f"{slug!r} is not a valid 'owner/name' repository slug")
        try:
            return self.repos[slug]
        except KeyError:
            known = ", ".join(sorted(self.repos)) or "<none configured>"
            raise ConfigError(
                f"repository {slug!r} is not in the configured allowlist ({known}). "
                'Add it under [repos."..."] before running the worker.'
            ) from None


def load_schema(schema_path: Path | None = None) -> dict[str, Any]:
    path = schema_path or default_schema_path()
    if not path.is_file():
        raise ConfigError(f"configuration schema not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration schema {path}: {exc}") from exc


def default_schema_path() -> Path:
    """Locate the approved ``config.schema.json``.

    Two copies exist deliberately:

    * ``config/config.schema.json`` in the repository is the reviewed source of
      truth that humans read and review;
    * ``agent_dispatch/data/config.schema.json`` is the same file shipped inside
      the package, so an installed CLI works without the checkout present.

    The offline suite asserts the two are byte-identical, so they cannot drift.
    """
    packaged = Path(__file__).resolve().parent / "data" / "config.schema.json"

    # In a source checkout prefer the reviewed repository copy.
    here = Path(__file__).resolve()
    if len(here.parents) > 2:
        repo_copy = here.parents[2] / "config" / "config.schema.json"
        if repo_copy.is_file():
            return repo_copy

    return packaged


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config file, honouring an explicit path then the usual locations."""
    if explicit is not None:
        path = expand_path(str(explicit))
        if not path.is_file():
            raise ConfigError(f"configuration file not found at {path}")
        return path

    for candidate in DEFAULT_CONFIG_PATHS:
        path = expand_path(candidate)
        if path.is_file():
            return path

    looked = ", ".join(str(expand_path(p)) for p in DEFAULT_CONFIG_PATHS)
    raise ConfigError(
        "no configuration file found. Copy the approved example and edit it:\n"
        "  mkdir -p ~/.config/agent-dispatch\n"
        "  cp config/agent-dispatch.example.toml ~/.config/agent-dispatch/config.toml\n"
        f"Looked in: {looked}"
    )


def load_config(
    explicit_path: str | Path | None = None, *, schema_path: Path | None = None
) -> Config:
    """Load, validate and freeze the configuration."""
    path = resolve_config_path(explicit_path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration {path} is not valid TOML: {exc}") from exc

    return build_config(raw, source_path=path, schema_path=schema_path)


def build_config(
    raw: dict[str, Any], *, source_path: Path, schema_path: Path | None = None
) -> Config:
    """Validate an already-parsed config document and convert it to dataclasses."""
    schema = load_schema(schema_path)
    schema_mod.set_root(schema)

    problems = schema_mod.validate(raw, schema)
    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)
        raise ConfigError(
            f"configuration {source_path} does not match config.schema.json:\n{detail}"
        )

    effective = schema_mod.apply_defaults(raw, schema)
    config = _build(effective, source_path)
    _check_cross_field_rules(config)
    return config


def _build(raw: dict[str, Any], source_path: Path) -> Config:
    worker_raw = raw.get("worker", {})
    github_raw = raw.get("github", {})
    labels_raw = github_raw.get("labels", {})

    worker = WorkerConfig(
        poll_interval_seconds=int(worker_raw.get("poll_interval_seconds", 120)),
        max_concurrent_tasks=int(worker_raw.get("max_concurrent_tasks", 1)),
        run_timeout_seconds=int(worker_raw.get("run_timeout_seconds", 3600)),
        max_attempts=int(worker_raw.get("max_attempts", 3)),
        worktree_root=expand_path(worker_raw.get("worktree_root", "~/agent-dispatch/worktrees")),
        state_db=expand_path(worker_raw.get("state_db", "~/.local/state/agent-dispatch/state.db")),
        run_log_dir=expand_path(
            worker_raw.get("run_log_dir", "~/.local/state/agent-dispatch/runs")
        ),
        run_log_keep=int(worker_raw.get("run_log_keep", 50)),
        lock_file=expand_path(
            worker_raw.get("lock_file", "~/.local/state/agent-dispatch/worker.lock")
        ),
        commandcode_path=worker_raw.get("commandcode_path"),
        # Default chosen so the common path is the safe one: a clone's own Git
        # config is NOT edited unless the operator asks, because a worktree shares
        # its clone's common config and that may be a personal checkout. The
        # credential env pairs cover orchestrator and agent Git without it.
        write_repo_local_credentials=bool(worker_raw.get("write_repo_local_credentials", False)),
        # Identity for commits the ORCHESTRATOR makes itself (committing what the
        # agent left uncommitted). Passed per-invocation with `-c`, so it never
        # depends on — or writes to — ambient Git config. Without this, a machine
        # with no `user.email` configured fails the commit with exit 128 and the
        # finished work never reaches the branch. Defaults are honest about being
        # machine commits; point them at your GitHub noreply address if you want
        # them attributed to you.
        commit_identity_name=worker_raw.get("commit_identity_name", "agent-dispatch"),
        commit_identity_email=worker_raw.get("commit_identity_email", "agent-dispatch@localhost"),
    )

    github = GithubConfig(
        command=github_raw["command"],
        credential_helper_reset=github_raw.get("credential_helper_reset", ""),
        credential_helper=github_raw["credential_helper"],
        fail_fast_on_denied_repo=bool(github_raw.get("fail_fast_on_denied_repo", True)),
        trigger_label=labels_raw.get("trigger", "take-it"),
        review_handoff_label=labels_raw.get("review_handoff", "agent:fix"),
    )

    repos: dict[str, RepoConfig] = {}
    for slug, repo_raw in raw.get("repos", {}).items():
        runtime_raw = repo_raw["runtime"]
        repos[slug] = RepoConfig(
            slug=slug,
            path=expand_path(repo_raw["path"]),
            base_branch=repo_raw.get("base_branch", "main"),
            agents_file=repo_raw.get("agents_file", "AGENTS.md"),
            runtime=RuntimeConfig(
                driver=runtime_raw["driver"],
                model=runtime_raw["model"],
                effort=runtime_raw.get("effort"),
                permission_mode=runtime_raw["permission_mode"],
                permission_flag=runtime_raw["permission_flag"],
                max_turns=int(runtime_raw.get("max_turns", 40)),
            ),
        )

    return Config(worker=worker, github=github, repos=repos, source_path=source_path, raw=raw)


def _check_cross_field_rules(config: Config) -> None:
    problems: list[str] = []

    if config.worker.max_concurrent_tasks != 1:
        problems.append(
            "worker.max_concurrent_tasks must be 1 in the MVP: the approved design allows "
            "exactly one active task total, not one per repository"
        )

    # Credential-helper contract: the empty reset entry must come first, and the
    # configured helper must be the approved wrapper rather than raw `gh`.
    if config.github.credential_helper_reset != "":
        problems.append(
            "github.credential_helper_reset must be empty — it resets the inherited helper list"
        )
    helper = config.github.credential_helper
    if not helper.startswith("!"):
        problems.append("github.credential_helper must start with '!' to run a command")
    if any(token in helper for token in _FORBIDDEN_HELPER_TOKENS):
        problems.append(
            "github.credential_helper must invoke the configured GitHub wrapper, not raw `gh auth git-credential`: "
            f"{helper!r}"
        )

    if not config.repos:
        problems.append('no repositories configured: add at least one [repos."owner/name"] entry')

    for slug, repo in config.repos.items():
        if not is_repo_slug(slug):
            problems.append(f"repository key {slug!r} is not 'owner/name'")
        if not repo.path.is_dir():
            problems.append(
                f"{slug}: repository path {repo.path} does not exist or is not a directory"
            )
        elif not (repo.path / ".git").exists():
            problems.append(f"{slug}: {repo.path} is not a Git checkout (no .git entry)")

    # State/log/lock/worktree locations must live outside every target repository.
    forbidden_roots = {slug: repo.path for slug, repo in config.repos.items()}
    placements = {
        "worker.state_db": config.worker.state_db,
        "worker.run_log_dir": config.worker.run_log_dir,
        "worker.lock_file": config.worker.lock_file,
        "worker.worktree_root": config.worker.worktree_root,
    }
    for name, location in placements.items():
        for slug, repo_path in forbidden_roots.items():
            if is_within(location, repo_path):
                problems.append(
                    f"{name} ({location}) must live OUTSIDE the target repository {slug} ({repo_path}); "
                    "a file written there can be swept into a commit by 'git add -A'"
                )

    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)
        raise ConfigError(f"configuration {config.source_path} is invalid:\n{detail}")
