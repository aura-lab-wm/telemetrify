import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator


@dataclass
class ToolCall:
    seq: int
    tool_name: str
    tool_use_id: str | None
    input_json: str
    output_text: str
    is_error: bool
    started_at: str | None


@dataclass
class Turn:
    session_id: str
    user_uuid: str
    parent_uuid: str | None
    prompt_id: str | None
    user_text: str
    assistant_text: str
    thinking_text: str
    model: str | None
    started_at: str
    finished_at: str | None
    latency_ms: int | None
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cwd: str | None
    git_branch: str | None
    project_dir: str | None
    transcript_path: str | None
    cli_version: str | None
    entrypoint: str | None
    user_type: str | None
    attribution_skill: str | None
    attribution_plugin: str | None
    tool_call_count: int
    assistant_message_count: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_json: str = ""


def iter_records(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def is_user_prompt(rec: dict) -> bool:
    """Real typed user prompt: type='user' with non-empty string content."""
    if rec.get("type") != "user":
        return False
    msg = rec.get("message") or {}
    content = msg.get("content")
    return isinstance(content, str) and content.strip() != ""


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summarize_tool_result(content) -> tuple[str, bool]:
    """Tool results in JSONL show up later as a user record whose message.content
    is a list of tool_result blocks. We collapse it to plain text + is_error flag."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    chunks: list[str] = []
    is_error = False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("is_error"):
            is_error = True
        inner = block.get("content")
        if isinstance(inner, str):
            chunks.append(inner)
        elif isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    chunks.append(sub.get("text") or "")
    return "\n".join(c for c in chunks if c), is_error


def build_turn(records: list[dict], user_idx: int, end_idx: int, transcript_path: Path) -> Turn | None:
    """Build a Turn from records[user_idx .. end_idx-1] where records[user_idx] is the user prompt."""
    user = records[user_idx]
    user_text = user["message"]["content"]
    user_uuid = user.get("uuid")
    if not user_uuid:
        return None

    assistant_chunks: list[str] = []
    thinking_chunks: list[str] = []
    tool_calls: list[ToolCall] = []
    pending_tool_use: dict[str, ToolCall] = {}  # tool_use_id -> ToolCall awaiting result
    tool_seq = 0
    assistant_msg_count = 0

    input_tokens = 0
    output_tokens = 0
    cache_creation = 0
    cache_read = 0

    model = None
    attribution_skill = None
    attribution_plugin = None
    cli_version = None
    finished_ts: str | None = None

    raw_window: list[dict] = [user]

    for rec in records[user_idx + 1 : end_idx]:
        rtype = rec.get("type")
        if rtype == "assistant":
            raw_window.append(rec)
            assistant_msg_count += 1
            msg = rec.get("message") or {}
            if msg.get("model"):
                model = msg["model"]
            if rec.get("attributionSkill"):
                attribution_skill = rec["attributionSkill"]
            if rec.get("attributionPlugin"):
                attribution_plugin = rec["attributionPlugin"]
            if rec.get("version"):
                cli_version = rec["version"]
            usage = msg.get("usage") or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            if rec.get("timestamp"):
                finished_ts = rec["timestamp"]

            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text") or ""
                        if text:
                            assistant_chunks.append(text)
                    elif btype == "thinking":
                        t = block.get("thinking") or ""
                        if t:
                            thinking_chunks.append(t)
                    elif btype == "tool_use":
                        tool_seq += 1
                        tc = ToolCall(
                            seq=tool_seq,
                            tool_name=block.get("name") or "",
                            tool_use_id=block.get("id"),
                            input_json=json.dumps(block.get("input") or {}, ensure_ascii=False),
                            output_text="",
                            is_error=False,
                            started_at=rec.get("timestamp"),
                        )
                        tool_calls.append(tc)
                        if tc.tool_use_id:
                            pending_tool_use[tc.tool_use_id] = tc
        elif rtype == "user":
            # Either a tool_result-bearing user record, or a real new prompt (which our
            # caller has already excluded via end_idx). Treat as tool_result harvest.
            raw_window.append(rec)
            msg = rec.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        tu_id = block.get("tool_use_id")
                        text, is_err = _summarize_tool_result(block.get("content"))
                        if tu_id and tu_id in pending_tool_use:
                            tc = pending_tool_use[tu_id]
                            tc.output_text = text
                            tc.is_error = is_err
        else:
            raw_window.append(rec)

    assistant_text = "\n\n".join(assistant_chunks).strip()
    if not assistant_text and not tool_calls:
        return None  # nothing meaningful captured

    thinking_text = "\n\n".join(thinking_chunks).strip() or None
    started = _parse_ts(user.get("timestamp"))
    finished = _parse_ts(finished_ts)
    latency_ms: int | None = None
    if started and finished:
        latency_ms = int((finished - started).total_seconds() * 1000)

    return Turn(
        session_id=user.get("sessionId") or "",
        user_uuid=user_uuid,
        parent_uuid=user.get("parentUuid"),
        prompt_id=user.get("promptId"),
        user_text=user_text,
        assistant_text=assistant_text,
        thinking_text=thinking_text or "",
        model=model,
        started_at=user.get("timestamp") or "",
        finished_at=finished_ts,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cwd=user.get("cwd"),
        git_branch=user.get("gitBranch"),
        project_dir=str(transcript_path.parent),
        transcript_path=str(transcript_path),
        cli_version=cli_version or user.get("version"),
        entrypoint=user.get("entrypoint"),
        user_type=user.get("userType"),
        attribution_skill=attribution_skill,
        attribution_plugin=attribution_plugin,
        tool_call_count=len(tool_calls),
        assistant_message_count=assistant_msg_count,
        tool_calls=tool_calls,
        raw_json=json.dumps(raw_window, ensure_ascii=False),
    )


def parse_latest_turn(path: Path) -> Turn | None:
    records = list(iter_records(path))
    if not records:
        return None
    last_user_idx = None
    for i in range(len(records) - 1, -1, -1):
        if is_user_prompt(records[i]):
            last_user_idx = i
            break
    if last_user_idx is None:
        return None
    return build_turn(records, last_user_idx, len(records), path)


def iter_all_turns(path: Path) -> Iterator[Turn]:
    """Yield every user→assistant turn in the transcript, in order."""
    records = list(iter_records(path))
    user_indices = [i for i, r in enumerate(records) if is_user_prompt(r)]
    for i, ui in enumerate(user_indices):
        end = user_indices[i + 1] if i + 1 < len(user_indices) else len(records)
        turn = build_turn(records, ui, end, path)
        if turn is not None:
            yield turn
