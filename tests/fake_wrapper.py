#!/usr/bin/env python3
"""A fake GitHub wrapper, used only by the offline test-suite.

This is **not** part of the service. It exists so the tests exercise the real
subprocess path, the real argument construction, the real pagination loop and the
real error classification in :mod:`agent_dispatch.github` — instead of a parallel
mock client that could drift from production behaviour.

It is deliberately executable and speaks the same CLI surface as the approved
wrapper (``gh api ...``) for the endpoints the service uses:

    <wrapper> api user --jq .login
    <wrapper> api repos/<slug> --jq .full_name
    <wrapper> api -X GET repos/<slug>/issues -f state=open -f labels=... -f per_page=N -f page=P
    <wrapper> api -X GET repos/<slug>/pulls   -f state=all -f per_page=N -f page=P
    <wrapper> api -X GET repos/<slug>/labels  -f per_page=N -f page=P
    <wrapper> api repos/<slug>/issues/<N>
    <wrapper> api -X POST repos/<slug>/labels -f name=... -f color=... -f description=...

The "world" (repositories, issues, PRs, labels and injected failures) is read
from the JSON file named by ``FAKE_GH_WORLD``. Failures are injected per endpoint
so tests can prove that a rate limit, a denied repo or a network error is reported
honestly and never marks a task complete.

No token, credential or network access is involved.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def die(message: str, code: int = 1) -> "None":
    print(message, file=sys.stderr)
    sys.exit(code)


def load_world() -> dict:
    path = os.environ.get("FAKE_GH_WORLD")
    if not path:
        die("FAKE_GH_WORLD is not set", 2)
    world_path = Path(path)
    if not world_path.is_file():
        die(f"FAKE_GH_WORLD does not exist: {world_path}", 2)
    try:
        world = json.loads(world_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read FAKE_GH_WORLD: {exc}", 2)
    # Persist state changes (e.g. created labels) and failure consumption so
    # repeated invocations within one test see consistent behaviour.
    world["_path"] = str(world_path)
    return world


def save_world(world: dict) -> None:
    path = world.pop("_path", None)
    if not path:
        return
    try:
        Path(path).write_text(json.dumps(world, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    world["_path"] = path


def emit(value: object, jq: str | None) -> None:
    """Print a response, applying the tiny subset of ``--jq`` the service uses.

    The service only ever asks for a single top-level field of a scalar response
    (``--jq .login``, ``--jq .full_name``), which ``gh`` prints as a bare value.
    """
    if jq:
        key = jq.strip().lstrip(".")
        if isinstance(value, dict):
            if key in value:
                print(value[key])
                return
            die(f"fake wrapper: no field {key!r} in response", 2)
        if isinstance(value, str):
            # A scalar response served through a field accessor prints as-is.
            print(value)
            return
        die(f"fake wrapper: unsupported --jq expression {jq!r}", 2)
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value))


def consume_failure(world: dict, kind: str) -> dict | None:
    """Return the configured failure for ``kind``, honouring ``fail_once``."""
    failures = world.get("failures") or {}
    spec = failures.get(kind)
    if not spec:
        return None
    if spec.get("fail_once"):
        # One-shot failures (including auth problems) are cleared on use so a
        # later poll can prove recovery.
        remaining = int(spec.get("remaining", 1))
        if remaining <= 1:
            failures.pop(kind, None)
            save_world(world)
        else:
            spec["remaining"] = remaining - 1
            save_world(world)
    return spec


def fail_with(spec: dict) -> None:
    """Emit a configured failure.

    ``stderr`` + non-zero exit models a real wrapper failure. A ``stdout`` entry
    with exit 0 models a wrapper that "succeeds" while emitting something that
    cannot be parsed — the service must treat that as a hard error too.
    """
    if "stdout" in spec:
        print(spec["stdout"])
        sys.exit(int(spec.get("exit", 0)))
    die(spec.get("stderr", "fake wrapper failure"), int(spec.get("exit", 1)))


def parse_flags(args: list[str]) -> tuple[str | None, str | None, dict[str, str]]:
    method: str | None = None
    endpoint: str | None = None
    fields: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-X" and index + 1 < len(args):
            method = args[index + 1]
            index += 2
            continue
        if token == "-f" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            fields[key] = value
            index += 2
            continue
        if token == "--jq" and index + 1 < len(args):
            fields["__jq__"] = args[index + 1]
            index += 2
            continue
        if endpoint is None:
            endpoint = token
        index += 1
    return method, endpoint, fields


def main(argv: list[str]) -> int:
    if not argv:
        die("fake wrapper: no arguments", 2)
    if argv[0] in {"--version", "version"}:
        print("gh version 2.100.0 (fake)")
        return 0

    world = load_world()

    if argv[0] == "auth" and len(argv) > 1 and argv[1] == "git-credential":
        # The real wrapper answers Git credential requests. Tests never need real
        # credentials; the offline header proves the helper was invoked at all.
        print("username=x-access-token")
        print("password=offline-fake")
        return 0

    if argv[0] != "api":
        die(f"fake wrapper: unsupported command {argv[0]!r}", 2)

    method, endpoint, fields = parse_flags(argv[1:])
    jq = fields.pop("__jq__", None)
    method = (method or "GET").upper()
    if endpoint is None:
        die("fake wrapper: no endpoint", 2)

    # A configurable cap models a listing that ends before the data does, which is
    # how a truncated scan is simulated: every page up to the cap comes back full.
    page = int(fields.get("page", "1"))
    per_page = int(fields.get("per_page", "100"))

    # `FAKE_GH_PAD_FULL_PAGES=N` makes pages 1..N return exactly `per_page`
    # synthetic entries, so the client always believes there is another page and
    # runs until its own page cap — the only way to simulate a truncated listing
    # without inventing thousands of real fixtures.
    pad_pages = int(os.environ.get("FAKE_GH_PAD_FULL_PAGES", "0") or 0)

    if endpoint == "user":
        spec = consume_failure(world, "user")
        if spec:
            fail_with(spec)
        # The service reads a field from the JSON object, exactly as the real
        # `gh api user` response would be parsed.
        emit({"login": world.get("identity", "fake-user")}, jq)
        return 0

    parts = endpoint.split("/")
    if len(parts) < 2 or parts[0] != "repos":
        die(f"fake wrapper: unsupported endpoint {endpoint!r}", 2)

    slug = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else die("fake wrapper: bad repo endpoint")
    repos = world.get("repos") or {}
    if slug not in repos:
        # Exactly what a denied/unknown repository looks like through the wrapper.
        die(
            f"gh: Not Found (HTTP 404) — repository {slug} is not accessible with this credential",
            1,
        )
    repo = repos[slug]

    if not repo.get("accessible", True):
        spec = consume_failure(world, f"{slug}:repo")
        stderr = (spec or {}).get("stderr") or (
            f"gh: Resource not accessible by personal access token (HTTP 403) for {slug}"
        )
        exit_code = int((spec or {}).get("exit", 1))
        die(stderr, exit_code)

    tail = parts[3:]

    if not tail:
        spec = consume_failure(world, f"{slug}:repo")
        if spec:
            fail_with(spec)
        emit({"full_name": slug, "default_branch": repo.get("default_branch", "main")}, jq)
        return 0

    resource = tail[0]

    if resource == "labels" and method == "POST":
        spec = consume_failure(world, f"{slug}:label_create")
        if spec:
            fail_with(spec)
        name = fields.get("name", "")
        labels = repo.setdefault("labels", [])
        record = {
            "name": name,
            "color": fields.get("color", ""),
            "description": fields.get("description", ""),
        }
        if name in {item["name"] for item in labels}:
            # Mirrors GitHub's 422 already_exists for a duplicate label.
            die('gh: Validation Failed (HTTP 422) {"errors":[{"code":"already_exists"}]}', 1)
        labels.append(record)
        save_world(world)
        emit(record, jq)
        return 0

    if resource == "labels":
        spec = consume_failure(world, f"{slug}:labels")
        if spec:
            fail_with(spec)
        labels = repo.get("labels", [])
        page_items = paginate(labels, page, per_page)
        emit(page_items, jq)
        return 0

    if resource == "issues" and len(tail) == 1:
        spec = consume_failure(world, f"{slug}:issues")
        if spec:
            fail_with(spec)
        wanted_label = fields.get("labels")
        items = [
            issue
            for issue in repo.get("issues", [])
            if issue.get("state", "open") == "open"
            and (wanted_label is None or wanted_label in label_names(issue))
        ]
        emit(paginate(items, page, per_page), jq)
        return 0

    if resource == "issues" and len(tail) == 2:
        number = int(tail[1])
        spec = consume_failure(world, f"{slug}:issue:{number}")
        if spec:
            fail_with(spec)
        for issue in repo.get("issues", []):
            if int(issue["number"]) == number:
                # GitHub's single-object endpoint answers for pull requests too,
                # marking them with a `pull_request` field. A number that exists
                # only in `pulls` is served here as such.
                payload = dict(issue)
                if number in {int(pr["number"]) for pr in repo.get("pulls", [])}:
                    payload["pull_request"] = {
                        "url": f"https://api.github.com/repos/{slug}/pulls/{number}"
                    }
                emit(payload, jq)
                return 0
        for pr in repo.get("pulls", []):
            if int(pr["number"]) == number:
                # A pull request is returned by the issues endpoint with the PR
                # marker set and no labels, exactly as GitHub does.
                emit(
                    {
                        "number": number,
                        "title": pr.get("title", f"PR {number}"),
                        "state": pr.get("state", "open"),
                        "html_url": pr.get("html_url", ""),
                        "labels": [],
                        "pull_request": {
                            "url": f"https://api.github.com/repos/{slug}/pulls/{number}"
                        },
                    },
                    jq,
                )
                return 0
        die(f"gh: Not Found (HTTP 404) — issue {slug}#{number}", 1)

    if resource == "branches" and len(tail) == 2:
        branch = tail[1]
        spec = consume_failure(world, f"{slug}:branches")
        if spec:
            fail_with(spec)
        if branch not in (repo.get("branches") or []):
            # Exactly what a missing branch looks like: a real HTTP 404, which is
            # the only answer the service treats as "absent".
            die(f"gh: Not Found (HTTP 404) — branch {branch} not found in {slug}", 1)
        emit({"name": branch, "commit": {"sha": "0" * 40}}, jq)
        return 0

    if resource == "pulls" and len(tail) == 2:
        number = int(tail[1])
        spec = consume_failure(world, f"{slug}:pull:{number}")
        if spec:
            fail_with(spec)
        for pr in repo.get("pulls", []):
            if int(pr["number"]) == number:
                emit(pr, jq)
                return 0
        die(f"gh: Not Found (HTTP 404) — pull request {slug}#{number}", 1)

    if resource == "pulls" and method == "POST":
        spec = consume_failure(world, f"{slug}:pull_create")
        if spec:
            fail_with(spec)
        head = fields.get("head", "")
        existing = [
            pr
            for pr in repo.get("pulls", [])
            if pr.get("head", {}).get("ref") == head and pr.get("state") == "open"
        ]
        if existing:
            # GitHub refuses a second open PR for the same head/base pair. The
            # service must never rely on this error, but it must not be able to
            # mistake it for success either.
            die(
                'gh: Validation Failed (HTTP 422) {"errors":[{"message":"A pull request '
                'already exists for this branch"}]}',
                1,
            )
        pulls = repo.setdefault("pulls", [])
        number = max([int(pr["number"]) for pr in pulls] or [0]) + 1
        # `head.repo.full_name` and `head.user.login` are what real GitHub returns,
        # and the service uses them to prove the PR is not from a fork whose branch
        # name happens to collide with ours. The fake must send them too, or the
        # adoption checks would pass here and fail live.
        record = {
            "number": number,
            "state": "open",
            "merged_at": None,
            "head": {
                "ref": head,
                "sha": "0" * 40,
                "repo": {"full_name": slug, "owner": {"login": slug.split("/")[0]}},
                "user": {"login": slug.split("/")[0]},
            },
            "html_url": f"https://github.com/{slug}/pull/{number}",
            "title": fields.get("title", ""),
            "body": fields.get("body", ""),
            "base": {"ref": fields.get("base", "main")},
        }
        pulls.append(record)
        save_world(world)
        emit(record, jq)
        return 0

    if resource == "pulls":
        spec = consume_failure(world, f"{slug}:pulls")
        if spec:
            fail_with(spec)
        items = repo.get("pulls", [])
        # `head=owner:branch` is how the service looks for the PR belonging to a
        # branch it owns. Honouring it is what makes the adoption/recovery tests
        # meaningful rather than accidentally passing on an unfiltered list.
        head_filter = fields.get("head")
        if head_filter:
            wanted = head_filter.split(":", 1)[-1]
            state_filter = fields.get("state")
            items = [
                pr
                for pr in items
                if pr.get("head", {}).get("ref") == wanted
                and (state_filter in (None, "all") or pr.get("state") == state_filter)
            ]
        if pad_pages and page <= pad_pages:
            # Return an exactly-full page so the client keeps paginating and
            # eventually hits its own cap: a genuinely truncated listing.
            real = items[:per_page]
            filler = [
                {
                    "number": 900000 + (page - 1) * per_page + index,
                    "state": "closed",
                    "merged_at": None,
                    "head": {"ref": f"noise/{page}-{index}"},
                    "html_url": f"https://github.com/{slug}/pull/900000",
                    "title": "unrelated filler",
                    "body": "",
                }
                for index in range(len(real), per_page)
            ]
            emit(real + filler, jq)
            return 0
        emit(paginate(items, page, per_page), jq)
        return 0

    die(f"fake wrapper: unsupported endpoint {endpoint!r}", 2)
    return 2


def paginate(items: list, page: int, per_page: int) -> list:
    start = max(page - 1, 0) * per_page
    return items[start : start + per_page]


def label_names(issue: dict) -> list[str]:
    """GitHub returns labels as objects; the service reads ``label['name']``."""
    return [
        str(item["name"]) if isinstance(item, dict) else str(item)
        for item in (issue.get("labels") or [])
    ]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
