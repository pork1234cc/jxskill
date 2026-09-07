from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def safe_filename(value: str, fallback: str = "duibiao", limit: int = 80) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    if not text:
        text = fallback
    if len(text) > limit:
        text = text[:limit].rstrip(" ._")
    return text or fallback


def md_escape(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def table_escape(value: Any) -> str:
    return md_escape(value).replace("|", "\\|") or "-"


def clean_metadata_text(value: Any) -> str:
    text = md_escape(value)
    text = re.sub(r"\s*#\d+\s+[^#\n|]*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_topics_from_text(value: Any, topics: list[str] | None = None) -> str:
    """清理展示用的标题/描述：剥掉所有 #话题，只保留正文。"""
    text = md_escape(value)
    # 先删抖音 "#12345 话题名" 这种数字挑战 id 形态
    text = re.sub(r"\s*#\d+\s+[^#\n|]*", "", text)
    # 删已知话题（clean_topics 输出，形如 "#好书推荐"）
    for topic in topics or []:
        text = text.replace(topic, "")
    # 兜底删残留的 #token（到空白或下一个 # 为止）
    text = re.sub(r"#[^\s#]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_topic(value: Any) -> str:
    text = md_escape(value).lstrip("#").strip()
    text = text.strip("，,。；;！!？?、")
    if not text:
        return ""
    if re.fullmatch(r"[\d\s_:\-]+", text):
        return ""
    if re.search(r"\d{6,}", text):
        return ""
    if re.search(r"\s", text) and re.search(r"\d", text):
        return ""
    return f"#{text}"


def clean_topics(values: Any) -> list[str]:
    topics: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        topic = clean_topic(value)
        if topic and topic not in seen:
            seen.add(topic)
            topics.append(topic)
    return topics


def seconds_to_time(value: Any) -> str:
    try:
        seconds = max(0, float(value or 0))
    except (TypeError, ValueError):
        seconds = 0
    minutes, sec = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def transcript_source_text(transcript: str, segments: list[dict[str, Any]] | None) -> str:
    if segments:
        text = "".join(str(item.get("text") or "").strip() for item in segments)
    else:
        text = str(transcript or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", "", text)
    # Long audio chunk joins can create fragments like "。的结果。".
    text = re.sub(r"([。！？])的结果。", "的结果。", text)
    text = text.replace("总觉得。自己", "总觉得自己")
    return text.strip()


def paragraphize_transcript(transcript: str, segments: list[dict[str, Any]] | None, max_chars: int = 280) -> list[str]:
    text = transcript_source_text(transcript, segments)
    if not text:
        return []

    sentences = [item for item in re.split(r"(?<=[。！？])", text) if item]
    paragraphs: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > max_chars:
            paragraphs.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        paragraphs.append(buffer)
    return paragraphs


def render_segment_lines(segments: list[dict[str, Any]] | None) -> list[str]:
    if not segments:
        return ["-"]
    lines: list[str] = []
    for item in segments:
        text = md_escape(item.get("text"))
        if not text:
            continue
        start = seconds_to_time(item.get("start"))
        end = seconds_to_time(item.get("end"))
        lines.append(f"- [{start}-{end}] {text}")
    return lines or ["-"]


def render_markdown(
    *,
    profile: dict[str, Any],
    source_url: str,
    collected_at: str,
    case_id: str = "",
    book_name: str = "",
    transcript: str,
    segments: list[dict[str, Any]] | None = None,
    duration: float | None = None,
) -> str:
    feed = ((profile.get("data") or {}).get("feedInfo") or {})
    author = ((profile.get("data") or {}).get("authorInfo") or {})
    topics = clean_topics(feed.get("topics") or [])
    # 标题/描述展示值：优先描述、其次标题，均剥掉 #话题；都为空则留空
    display_desc = strip_topics_from_text(feed.get("description"), topics) or strip_topics_from_text(feed.get("title"), topics)
    # H1 文档标题需要非空兜底，避免出现空标题
    title = clean_metadata_text(book_name) or display_desc or "对标视频"
    platform = str(profile.get("platform") or "")
    author_name = md_escape(author.get("nickname") or author.get("username") or "")
    metrics = {
        "点赞": feed.get("likeCountFmt") or feed.get("likeCount") or "",
        "收藏": feed.get("favCountFmt") or feed.get("favCount") or "",
        "评论": feed.get("commentCountFmt") or feed.get("commentCount") or "",
        "转发": feed.get("forwardCountFmt") or feed.get("forwardCount") or "",
    }
    topic_text = " ".join(topics)
    info_rows = [
        ("案例 ID", case_id),
        ("采集时间", collected_at),
        ("原始链接", source_url),
        ("平台", platform),
        ("作者", author_name),
        ("书名", book_name),
        ("标题/描述", display_desc),
        ("话题", topic_text),
        ("点赞", metrics["点赞"]),
        ("收藏", metrics["收藏"]),
        ("评论", metrics["评论"]),
        ("转发", metrics["转发"]),
        ("时长", seconds_to_time(duration) if duration else ""),
    ]
    clean_paragraphs = paragraphize_transcript(transcript, segments)
    lines = [
        f"# {title}",
        "",
        "## 基础信息",
        "",
        "| 字段 | 内容 |",
        "|---|---|",
        *[f"| {field} | {table_escape(value)} |" for field, value in info_rows],
        "",
        "## 清洗逐字稿",
        "",
        *(clean_paragraphs or ["-"]),
        "",
        "## 分句逐字稿",
        "",
        *render_segment_lines(segments),
        "",
    ]
    return "\n".join(lines)


def timestamp_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
