#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Typora GitHub image uploader.

Features:
- Upload images to a separate GitHub repository.
- Keep a local mirror with the same directory structure.
- Use Markdown front matter date when available.
- Reuse an existing article date directory if front matter has no date.
- Deduplicate by Git blob SHA.
- Assign stable incremental filenames.
- Retry on remote filename conflicts.
- Print URLs to stdout only after all images are processed successfully.
- Keep logs on stderr.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("image_uploader")


class UploadError(Exception):
    pass


class RemoteConflict(UploadError):
    pass


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise UploadError(f"配置文件不存在：{CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)

    required = [
        ("github", "owner"),
        ("github", "repository"),
        ("github", "branch"),
        ("github", "base_directory"),
        ("github", "token_env"),
        ("local", "backup_directory"),
    ]

    for section, key in required:
        if not config.get(section, {}).get(key):
            raise UploadError(f"配置缺少字段：{section}.{key}")

    token_env = config["github"]["token_env"]
    token = os.environ.get(token_env)

    if not token:
        raise UploadError(
            f"没有找到 GitHub Token 环境变量：{token_env}"
        )

    config["_token"] = token
    return config


def clean_path_component(value: str) -> str:
    """Sanitize a filename/path component for cross-platform use."""
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = value.rstrip(". ")

    if not value:
        return "untitled"

    return value


def git_blob_sha(data: bytes) -> str:
    """Calculate the Git blob SHA used by GitHub's tree API."""
    header = f"blob {len(data)}\0".encode("utf-8")
    return hashlib.sha1(header + data).hexdigest()


def parse_front_matter_date(markdown_path: Path) -> str | None:
    """
    Read a simple YAML front matter date.

    Supported examples:
        ---
        date: 2026-01-01
        ---

        ---
        date: "2026-01-01 10:30:00"
        ---
    """
    if not markdown_path.is_file():
        return None

    try:
        text = markdown_path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )
    except OSError:
        return None

    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        return None

    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break

        match = re.match(r"^\s*date\s*:\s*(.*?)\s*$", line)

        if not match:
            continue

        value = match.group(1).strip().strip("'\"")

        # Accept YYYY-MM-DD or a timestamp beginning with YYYY-MM-DD.
        date_match = re.match(
            r"^(\d{4}-\d{2}-\d{2})(?:[ T].*)?$",
            value,
        )

        if date_match:
            return date_match.group(1)

        raise UploadError(
            f"Front Matter 中的 date 格式不正确：{value}"
        )

    return None


def validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise UploadError(f"日期格式必须为 YYYY-MM-DD：{value}")

    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise UploadError(f"无效日期：{value}") from exc

    return value


def api_request(
    config: dict[str, Any],
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    owner = config["github"]["owner"]
    repository = config["github"]["repository"]

    api_base = (
        f"https://api.github.com/repos/"
        f"{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(repository, safe='')}"
    )

    url = api_base + endpoint

    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {config['_token']}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Typora-GitHub-Image-Uploader",
    }

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    timeout = int(
        config.get("upload", {}).get("timeout_seconds", 30)
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            raw = response.read()

            if not raw:
                return None

            return json.loads(raw.decode("utf-8"))

    except urllib.error.HTTPError as exc:
        response_text = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        if exc.code in (409, 422):
            try:
                message = json.loads(response_text).get(
                    "message", ""
                )
            except json.JSONDecodeError:
                message = response_text

            lower_message = message.lower()

            if (
                exc.code == 409
                or "already exists" in lower_message
                or "sha" in lower_message
            ):
                raise RemoteConflict(message)

        raise UploadError(
            f"GitHub API 请求失败：HTTP {exc.code}，"
            f"{response_text}"
        ) from exc

    except urllib.error.URLError as exc:
        raise UploadError(
            f"网络请求失败：{exc.reason}"
        ) from exc


def get_remote_tree(
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Retrieve the repository tree recursively."""
    branch = config["github"]["branch"]
    encoded_branch = urllib.parse.quote(branch, safe="")

    ref = api_request(
        config,
        "GET",
        f"/git/ref/heads/{encoded_branch}",
    )

    commit_sha = ref["object"]["sha"]

    commit = api_request(
        config,
        "GET",
        f"/git/commits/{commit_sha}",
    )

    tree_sha = commit["tree"]["sha"]

    tree = api_request(
        config,
        "GET",
        f"/git/trees/{tree_sha}?recursive=1",
    )

    if tree.get("truncated"):
        raise UploadError(
            "GitHub 返回的文件树被截断，"
            "无法安全分配图片编号。请缩小图床仓库规模后重试。"
        )

    return tree.get("tree", [])


def article_directories(
    config: dict[str, Any],
    article_name: str,
) -> set[str]:
    """Find existing date directories for this article remotely and locally."""
    base = config["github"]["base_directory"].strip("/")
    prefix = f"{base}/"

    found: set[str] = set()

    for item in get_remote_tree(config):
        path = item.get("path", "")

        if not path.startswith(prefix):
            continue

        parts = path.split("/")

        if len(parts) >= 3 and parts[2] == article_name:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
                found.add(parts[1])

    backup_root = Path(
        config["local"]["backup_directory"]
    ).expanduser()

    local_article_root = backup_root / base

    if local_article_root.is_dir():
        for date_dir in local_article_root.iterdir():
            if not date_dir.is_dir():
                continue

            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir.name):
                continue

            if (date_dir / article_name).is_dir():
                found.add(date_dir.name)

    return found


def resolve_article_date(
    config: dict[str, Any],
    markdown_path: Path | None,
    article_name: str,
) -> str:
    if markdown_path:
        front_matter_date = parse_front_matter_date(
            markdown_path
        )

        if front_matter_date:
            return validate_date(front_matter_date)

    existing = article_directories(config, article_name)

    if len(existing) == 1:
        return next(iter(existing))

    if len(existing) > 1:
        choices = ", ".join(sorted(existing))
        raise UploadError(
            f"文章“{article_name}”存在多个日期目录：{choices}。"
            "请在 Markdown Front Matter 中明确指定 date。"
        )

    from datetime import date

    return date.today().isoformat()


def encode_repo_path(path: str) -> str:
    return "/".join(
        urllib.parse.quote(part, safe="")
        for part in path.split("/")
    )


def raw_url(
    config: dict[str, Any],
    remote_path: str,
) -> str:
    owner = urllib.parse.quote(
        config["github"]["owner"],
        safe="",
    )
    repository = urllib.parse.quote(
        config["github"]["repository"],
        safe="",
    )
    branch = urllib.parse.quote(
        config["github"]["branch"],
        safe="",
    )

    return (
        f"https://raw.githubusercontent.com/"
        f"{owner}/{repository}/{branch}/"
        f"{encode_repo_path(remote_path)}"
    )


def get_remote_file_map(
    config: dict[str, Any],
) -> dict[str, str]:
    tree = get_remote_tree(config)

    return {
        item["path"]: item["sha"]
        for item in tree
        if item.get("type") == "blob"
    }


def find_matching_remote(
    remote_files: dict[str, str],
    article_prefix: str,
    blob_sha: str,
) -> str | None:
    candidates = [
        path
        for path, sha in remote_files.items()
        if path.startswith(article_prefix)
        and sha == blob_sha
    ]

    return sorted(candidates)[0] if candidates else None


def find_matching_local(
    local_article_dir: Path,
    blob_sha: str,
) -> Path | None:
    if not local_article_dir.is_dir():
        return None

    for path in local_article_dir.iterdir():
        if not path.is_file():
            continue

        try:
            if git_blob_sha(path.read_bytes()) == blob_sha:
                return path
        except OSError:
            continue

    return None


def next_image_number(
    remote_files: dict[str, str],
    local_article_dir: Path,
    article_prefix: str,
    extension: str,
) -> int:
    pattern = re.compile(
        r"-(\d+)" + re.escape(extension) + r"$",
        re.IGNORECASE,
    )

    numbers: list[int] = []

    for path in remote_files:
        if not path.startswith(article_prefix):
            continue

        match = pattern.search(Path(path).name)

        if match:
            numbers.append(int(match.group(1)))

    if local_article_dir.is_dir():
        for path in local_article_dir.iterdir():
            if not path.is_file():
                continue

            match = pattern.search(path.name)

            if match:
                numbers.append(int(match.group(1)))

    return max(numbers, default=0) + 1


def upload_remote(
    config: dict[str, Any],
    remote_path: str,
    data: bytes,
) -> None:
    payload = {
        "message": f"Upload image: {Path(remote_path).name}",
        "content": base64.b64encode(data).decode("ascii"),
        "branch": config["github"]["branch"],
    }

    encoded_path = encode_repo_path(remote_path)

    api_request(
        config,
        "PUT",
        f"/contents/{encoded_path}",
        payload,
    )


def verify_remote(
    config: dict[str, Any],
    remote_path: str,
    expected_sha: str,
) -> bool:
    remote_files = get_remote_file_map(config)
    return remote_files.get(remote_path) == expected_sha


def write_local_mirror(
    source_data: bytes,
    local_path: Path,
) -> None:
    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if local_path.exists():
        existing_sha = git_blob_sha(local_path.read_bytes())
        incoming_sha = git_blob_sha(source_data)

        if existing_sha == incoming_sha:
            return

        raise UploadError(
            f"本地镜像文件已存在且内容不同，拒绝覆盖：{local_path}"
        )

    temp_path = local_path.with_name(
        local_path.name + ".uploading"
    )

    try:
        with temp_path.open("xb") as f:
            f.write(source_data)

        temp_path.replace(local_path)

    finally:
        if temp_path.exists():
            temp_path.unlink()


def process_one_image(
    config: dict[str, Any],
    source_path: Path,
    article_name: str,
    article_date: str,
) -> str:
    if not source_path.is_file():
        raise UploadError(f"图片文件不存在：{source_path}")

    extension = source_path.suffix.lower()

    allowed = {
        item.lower()
        for item in config.get("upload", {}).get(
            "supported_extensions",
            [".png", ".jpg", ".jpeg", ".gif", ".webp"],
        )
    }

    if extension not in allowed:
        raise UploadError(
            f"不支持的图片格式：{extension}（{source_path}）"
        )

    data = source_path.read_bytes()
    blob_sha = git_blob_sha(data)

    base = config["github"]["base_directory"].strip("/")
    article_prefix = (
        f"{base}/{article_date}/{article_name}/"
    )

    backup_root = Path(
        config["local"]["backup_directory"]
    ).expanduser()

    local_article_dir = (
        backup_root
        / base
        / article_date
        / article_name
    )

    retries = int(
        config.get("upload", {}).get("max_retries", 5)
    )

    for attempt in range(retries):
        remote_files = get_remote_file_map(config)

        # Case 1: The same image already exists remotely.
        existing_remote = find_matching_remote(
            remote_files,
            article_prefix,
            blob_sha,
        )

        if existing_remote:
            local_path = (
                local_article_dir
                / Path(existing_remote).name
            )

            write_local_mirror(data, local_path)

            log.info("复用远端图片：%s", existing_remote)
            return raw_url(config, existing_remote)

        # Case 2: A local-only copy exists.
        # Reuse its filename if the remote path is still free.
        local_match = find_matching_local(
            local_article_dir,
            blob_sha,
        )

        if local_match:
            candidate_path = (
                article_prefix + local_match.name
            )

            if candidate_path not in remote_files:
                try:
                    upload_remote(
                        config,
                        candidate_path,
                        data,
                    )

                    if not verify_remote(
                        config,
                        candidate_path,
                        blob_sha,
                    ):
                        raise UploadError(
                            "上传后远端校验失败："
                            + candidate_path
                        )

                    return raw_url(config, candidate_path)

                except RemoteConflict:
                    log.warning(
                        "远端文件名冲突，重新扫描：%s",
                        candidate_path,
                    )
                    time.sleep(0.5 * (attempt + 1))
                    continue

        # Case 3: Allocate a new stable number.
        number = next_image_number(
            remote_files,
            local_article_dir,
            article_prefix,
            extension,
        )

        filename = (
            f"{article_name}-{number:02d}{extension}"
        )
        remote_path = article_prefix + filename

        if remote_path in remote_files:
            time.sleep(0.5 * (attempt + 1))
            continue

        try:
            upload_remote(config, remote_path, data)

        except RemoteConflict:
            log.warning(
                "发现并发编号冲突，重新分配：%s",
                remote_path,
            )
            time.sleep(0.5 * (attempt + 1))
            continue

        if not verify_remote(
            config,
            remote_path,
            blob_sha,
        ):
            raise UploadError(
                f"远端上传后校验失败：{remote_path}"
            )

        # Only return after the local mirror also succeeds.
        local_path = local_article_dir / filename
        write_local_mirror(data, local_path)

        log.info("上传成功：%s", remote_path)
        return raw_url(config, remote_path)

    raise UploadError(
        f"多次重试仍无法完成上传：{source_path}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Typora GitHub image uploader"
    )

    parser.add_argument(
        "--markdown",
        type=str,
        default=None,
        help="当前 Markdown 文件路径",
    )

    parser.add_argument(
        "images",
        nargs="*",
        help="需要上传的图片路径",
    )

    args = parser.parse_args()

    try:
        config = load_config()

        markdown_path = (
            Path(args.markdown).expanduser().resolve()
            if args.markdown
            else None
        )

        if markdown_path:
            article_name = clean_path_component(
                markdown_path.stem
            )
        else:
            article_name = config.get(
                "upload", {}
            ).get(
                "untitled_article_name",
                "untitled",
            )

        article_date = resolve_article_date(
            config,
            markdown_path,
            article_name,
        )

        image_paths = [
            Path(item).expanduser().resolve()
            for item in args.images
        ]

        if not image_paths:
            log.info("没有收到需要上传的图片。")
            return 0

        log.info("文章目录：%s/%s/%s",
                 config["github"]["base_directory"],
                 article_date,
                 article_name)

        urls: list[str] = []

        # Process sequentially to preserve stable numbering
        # and the original input order.
        for image_path in image_paths:
            url = process_one_image(
                config,
                image_path,
                article_name,
                article_date,
            )
            urls.append(url)

        # Do not print partial results. Typora receives URLs
        # only when every image has completed successfully.
        for url in urls:
            print(url)

        return 0

    except Exception as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())