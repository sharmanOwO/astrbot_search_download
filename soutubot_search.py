"""Requests-based soutubot search and formatting for the AstrBot plugin."""

from __future__ import annotations

import math
import mimetypes
import threading
import time
import re
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://soutubot.moe"
SEARCH_URL = f"{BASE_URL}/api/search"
USER_AGENT = "Mozilla/5.0 astrbot-soutu-download/1.1"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class SearchError(RuntimeError):
    """A network, API, or response schema error."""


# This service runs on the same private Docker network and uses the same
# egress address as AstrBot. Never send image bytes to the browser service.
CLOUDFLARE_SOLVER_URL = "http://soutubot-browser:8191/v1"
_clearance_lock = threading.Lock()
_clearance: dict[str, Any] | None = None


def _cached_clearance() -> dict[str, Any] | None:
    cached = _clearance
    if cached is not None and cached["valid_until"] > time.monotonic():
        return cached
    return None


def _apply_clearance(session: requests.Session, cached: dict[str, Any] | None) -> None:
    if cached is None:
        return
    session.headers["User-Agent"] = cached["user_agent"]
    # Only the site's clearance cookie is reused. No login cookies are copied.
    session.cookies.set(
        "cf_clearance", cached["cookie"], domain="soutubot.moe", path="/", secure=True
    )


def _refresh_clearance(
    rejected: dict[str, Any] | None, timeout: float
) -> dict[str, Any]:
    global _clearance
    # One browser at a time; concurrent searches reuse a newly refreshed cookie.
    if not _clearance_lock.acquire(timeout=min(timeout, 50.0)):
        raise SearchError("搜图网站验证繁忙，请稍后重试")
    try:
        cached = _cached_clearance()
        if cached is not None and cached is not rejected:
            return cached
        _clearance = None
        solve_seconds = min(timeout, 45.0)
        try:
            with requests.Session() as solver:
                # Internal service traffic must never go through an HTTP proxy.
                solver.trust_env = False
                with solver.post(
                    CLOUDFLARE_SOLVER_URL,
                    json={
                        "cmd": "request.get",
                        "url": BASE_URL + "/",
                        "maxTimeout": max(1000, int(solve_seconds * 1000)),
                    },
                    timeout=(min(5.0, timeout), solve_seconds + 5.0),
                    allow_redirects=False,
                ) as response:
                    if response.status_code != 200:
                        raise SearchError("搜图网站验证服务暂不可用，请稍后重试")
                    payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Do not expose service responses, cookie values, or proxy secrets.
            raise SearchError("搜图网站验证服务连接失败或超时，请稍后重试") from exc
        solution = payload.get("solution") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "ok"
            or not isinstance(solution, dict)
            or solution.get("status") != 200
            or not str(solution.get("url", "")).startswith(BASE_URL + "/")
        ):
            raise SearchError("搜图网站 Cloudflare 验证未通过，请稍后重试")
        user_agent = solution.get("userAgent")
        cookies = solution.get("cookies")
        if not isinstance(user_agent, str) or not user_agent or not isinstance(cookies, list):
            raise SearchError("搜图网站验证未返回可用会话，请稍后重试")
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            if (
                cookie.get("name") != "cf_clearance"
                or str(cookie.get("domain", "")).lstrip(".") != "soutubot.moe"
                or not isinstance(cookie.get("value"), str)
                or not cookie["value"]
            ):
                continue
            lifetime = 1800.0
            expires = cookie.get("expiry", cookie.get("expires"))
            if isinstance(expires, (int, float)) and expires > 0:
                lifetime = min(lifetime, expires - time.time() - 30.0)
            if lifetime <= 0:
                continue
            _clearance = {
                "user_agent": user_agent,
                "cookie": cookie["value"],
                "valid_until": time.monotonic() + lifetime,
            }
            return _clearance
        raise SearchError("搜图网站验证未返回有效通行凭据，请稍后重试")
    finally:
        _clearance_lock.release()


def _text(value: Any) -> str:
    """Keep optional API values on one line, without control characters."""
    if value is None or value == [] or value == {}:
        return ""
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value)).split())


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the current schema; a missing results key is not zero matches."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchError("响应缺少 results 列表，搜图接口结构可能已变化")
    if payload.get("status") in ("error", "failed"):
        raise SearchError(
            f"搜索服务失败：{_text(payload.get('detail') or payload['status'])}"
        )
    results = payload["results"]
    for hit in results:
        if not isinstance(hit, dict):
            raise SearchError("results 中的命中不是对象")
        score = hit.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise SearchError("命中的 score 不是有效数值")
        segments = hit.get("path_segments")
        if (
            not isinstance(segments, list)
            or not segments
            or not isinstance(segments[0], dict)
        ):
            raise SearchError("命中的 path_segments 缺少第一条来源路径")
    return results


def search_image(
    image_path: Path | str,
    strict: bool = False,
    timeout: float = 60,
) -> dict[str, Any]:
    """Upload and return JSON; retry once only after a Cloudflare challenge."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须是大于 0 的有限数值")
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("图片不存在或不是文件")
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in SUPPORTED_TYPES:
        raise ValueError("仅支持 JPG、PNG、WebP、GIF 图片")
    if not 0 < path.stat().st_size <= MAX_UPLOAD_BYTES:
        raise ValueError("图片必须非空且不超过 5 MiB，请先缩小或压缩")

    try:
        with requests.Session() as session, path.open("rb") as image_file:
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Origin": BASE_URL,
                    "Referer": f"{BASE_URL}/",
                }
            )
            cached = _cached_clearance()
            _apply_clearance(session, cached)
            for attempt in range(2):
                image_file.seek(0)
                with session.post(
                    SEARCH_URL,
                    data={"factor": "1.4" if strict else "1.2", "metadata_mode": "display"},
                    files={"file": (path.name, image_file, mime)},
                    timeout=(min(10.0, timeout), timeout),
                    allow_redirects=False,
                ) as response:
                    if response.headers.get("cf-mitigated", "").lower() == "challenge":
                        if attempt == 0:
                            response.close()
                            cached = _refresh_clearance(cached, timeout)
                            _apply_clearance(session, cached)
                            continue
                        raise SearchError(
                            "搜图网站 Cloudflare 验证仍未通过，已停止重复上传，请稍后重试"
                        )
                    if 300 <= response.status_code < 400:
                        raise SearchError(
                            f"上传接口发生重定向（HTTP {response.status_code}），请检查网站"
                        )
                    if response.status_code >= 400:
                        detail = ""
                        try:
                            error = response.json()
                            if isinstance(error, dict):
                                detail = _text(error.get("detail") or error.get("message"))[
                                    :500
                                ]
                        except ValueError:
                            pass
                        hints = {
                            401: "接口要求认证，请检查当前网页要求",
                            403: "访问被拒绝，请检查登录或验证要求",
                            413: "图片过大，请缩小或压缩",
                            422: "图片格式、尺寸或搜索参数不符合要求",
                            429: "请求频率或额度受限，请稍后重试",
                        }
                        retry_after = _text(response.headers.get("Retry-After"))
                        suffix = f"；Retry-After={retry_after}" if retry_after else ""
                        raise SearchError(
                            f"HTTP {response.status_code}："
                            f"{detail or hints.get(response.status_code, '搜索服务请求失败')}{suffix}"
                        )
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise SearchError(
                            "网站返回的内容不是有效 JSON，可能是验证页或接口已变化"
                        ) from exc
                    break
    except requests.Timeout as exc:
        raise SearchError("搜图连接或读取超时，已停止继续上传，请稍后重试") from exc
    except requests.RequestException as exc:
        # Avoid exposing proxy credentials or full URLs from exception messages.
        raise SearchError(
            f"网络请求失败（{type(exc).__name__}），请检查网络、代理和证书"
        ) from exc

    _results(payload)
    return payload


def _labels(items: Any, key: str) -> list[str]:
    """Read nonempty labels from optional metadata lists."""
    if not isinstance(items, list):
        return []
    return [
        text
        for item in items
        if isinstance(item, dict) and (text := _text(item.get(key)))
    ]


def normalized_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Sort every hit by score, then use its first source path; do not mutate JSON."""
    rows = []
    for hit in sorted(_results(payload), key=lambda item: item["score"], reverse=True):
        segment = hit["path_segments"][0]
        metadata = segment.get("metadata") or {}
        title = metadata.get("title") or {}
        facts = metadata.get("facts") or {}
        tags = metadata.get("tags") or {}
        booru_tags = any(
            key in tags for key in ("artists", "characters", "works", "general")
        )
        if booru_tags:
            works = _labels((tags.get("works") or [])[:1], "text")
            characters = _labels(tags.get("characters"), "text")
            artists = _labels(tags.get("artists"), "text")
            labels = _labels(tags.get("general"), "text")
        else:
            works = _labels((metadata.get("works") or [])[:1], "name")
            characters = _labels(metadata.get("characters"), "name")
            artists = []
            labels = [_text(tag) for tag in (tags.get("other") or []) if _text(tag)]
        rows.append(
            {
                "score": hit["score"],
                "title": _text(title.get("japanese_or_alias") or title.get("primary")),
                "source": _text(segment.get("source_key")),
                "language": _text(segment.get("language")),
                "source_url": _text(segment.get("source_url")),
                "page_url": _text(
                    segment.get("page_url") or segment.get("chapter_url")
                ),
                "work": works[0] if works else "",
                "characters": ", ".join(characters),
                "artists": ", ".join(artists),
                "category": _text(facts.get("category")),
                "page_count": _text(facts.get("page_count")),
                "chapter": _text(facts.get("chapter")),
                "tags": ", ".join(labels),
            }
        )
    return rows


def format_summary(
    payload: dict[str, Any], limit: int = 10, detail: bool = False
) -> str:
    """Render the highest-scoring hits, omitting absent or empty optional values."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("n 必须是非负整数喵")
    rows = normalized_results(payload)
    notices = []
    if payload.get("partial") or payload.get("status") == "partial":
        notices.append("本次可能只返回了部分结果喵")
    for warning in payload.get("warnings") or []:
        code = _text(warning.get("code") if isinstance(warning, dict) else warning)
        if code == "no_search_features":
            code = "图片缺少可用于搜索的特征，请换用细节更丰富的图片喵"
        if code and f"提示：{code}" not in notices:
            notices.append(f"提示：{code}喵")
    if not rows:
        return "\n".join(["没有找到匹配结果喵", *notices])
    if limit == 0:
        return "\n".join(
            [f"搜图完成，共 {len(rows)} 条结果；当前展示条数为 0 喵", *notices]
        )

    blocks = []
    for index, row in enumerate(rows[:limit], 1):
        lines = [
            f"{index}.[{row['score']}%]" + (f" {row['title']}" if row["title"] else "")
        ]
        source_line = "   ".join(
            f"{label}：{row[key]}"
            for label, key in (("来源", "source"), ("语言", "language"))
            if row[key]
        )
        if source_line:
            lines.append(f" {source_line}")
        fields = [("链接", "source_url")]
        if detail:
            fields.extend(
                [
                    ("页面链接", "page_url"),
                    ("作品", "work"),
                    ("画师", "artists"),
                    ("角色", "characters"),
                    ("分类", "category"),
                    ("页数", "page_count"),
                    ("章节数", "chapter"),
                    ("标签", "tags"),
                ]
            )
        lines.extend(f" {label}：{row[key]}" for label, key in fields if row[key])
        blocks.append("\n".join(lines))
    return "\n\n".join([*blocks, *notices])
