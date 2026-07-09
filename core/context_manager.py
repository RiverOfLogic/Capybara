"""上下文预算管理 — 把喂给 LLM 的上下文保持在窗口预算内

提供三个纯函数（无状态、易测）：
- estimate_tokens: 粗估 token 数（len//4，与基类口径一致）
- truncate_output: 截断单次（工具）输出，防止灌爆上下文
- compact_history: 历史过长时把旧轮折叠成一条摘要消息

设计原则：只改「喂回 LLM 的副本」，不改磁盘真实文件或工具返回对象。
"""

from typing import Any, Callable, List, Optional, Tuple

# 与 core/agent.py 原 _estimate_history_tokens 一致的粗估系数
_CHARS_PER_TOKEN = 4


def estimate_tokens(obj: Any) -> int:
    """粗估 token 数。接受 str 或 list[dict(role, content)]。"""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj) // _CHARS_PER_TOKEN
    if isinstance(obj, (list, tuple)):
        total = 0
        for item in obj:
            if isinstance(item, dict):
                total += len(str(item.get("content") or ""))
            else:
                total += len(str(getattr(item, "content", "")))
        return total // _CHARS_PER_TOKEN
    return len(str(obj)) // _CHARS_PER_TOKEN


def truncate_output(
    text: str,
    max_lines: int,
    max_bytes: int,
    direction: str = "head",
) -> Tuple[str, bool]:
    """截断输出文本。先按行数、再按字节截断。

    direction:
      - head: 保留开头
      - tail: 保留结尾
      - head_tail: 保留开头和结尾各一半

    返回 (截断后文本, 是否发生截断)。
    """
    if not text:
        return text, False

    original_lines = text.count("\n") + 1
    original_bytes = len(text.encode("utf-8"))
    truncated = False

    # 1) 按行数截断
    lines = text.split("\n")
    if max_lines > 0 and len(lines) > max_lines:
        truncated = True
        if direction == "tail":
            lines = lines[-max_lines:]
        elif direction == "head_tail":
            half = max_lines // 2
            lines = lines[:half] + ["...[中间省略]..."] + lines[-(max_lines - half):]
        else:  # head
            lines = lines[:max_lines]
        text = "\n".join(lines)

    # 2) 按字节截断（在行截断之后再保险一道）
    encoded = text.encode("utf-8")
    if max_bytes > 0 and len(encoded) > max_bytes:
        truncated = True
        if direction == "tail":
            clipped = encoded[-max_bytes:]
        elif direction == "head_tail":
            half = max_bytes // 2
            clipped = encoded[:half] + "...[中间省略]...".encode("utf-8") + encoded[-half:]
        else:  # head
            clipped = encoded[:max_bytes]
        text = clipped.decode("utf-8", errors="ignore")

    if truncated:
        marker = (
            f"\n...[输出已截断：原 {original_lines} 行 / "
            f"{original_bytes} 字节，方向={direction}]..."
        )
        text = text + marker

    return text, truncated


def _cheap_summary(old_messages: List[dict]) -> str:
    """默认摘要：拼接被折叠各轮的角色 + 内容片段，无 LLM 调用。"""
    parts = []
    for m in old_messages:
        role = m.get("role", "")
        content = (m.get("content") or "").replace("\n", " ").strip()
        if len(content) > 120:
            content = content[:120] + "..."
        if content:
            parts.append(f"{role}: {content}")
    body = " | ".join(parts)
    if len(body) > 1500:
        body = body[:1500] + "..."
    return f"[History Summary] Key points from the previous {len(old_messages)} messages: {body}"


def compact_history(
    messages: List[dict],
    *,
    context_window: int,
    threshold: float,
    min_retain_rounds: int,
    summarizer: Optional[Callable[[List[dict]], str]] = None,
) -> Tuple[List[dict], bool]:
    """历史过长时把旧轮折叠成一条摘要消息。

    messages: 仅含 system?/user/assistant 的跨轮历史（无 tool 消息），
              因此可安全折叠，不会破坏 assistant↔tool 配对。

    若估算 token 未超过 threshold*context_window → 原样返回 (messages, False)。
    否则保留开头的 system 消息 + 最近 min_retain_rounds 轮（1 轮 = 1 条 user
    起到下一条 user 前），更早的折叠为一条 system 摘要消息。
    """
    budget = int(context_window * threshold)
    if estimate_tokens(messages) <= budget:
        return messages, False

    # 分离开头连续的 system 消息
    head_system: List[dict] = []
    idx = 0
    while idx < len(messages) and messages[idx].get("role") == "system":
        head_system.append(messages[idx])
        idx += 1
    body = messages[idx:]

    # 找到「最近 min_retain_rounds 轮」的起点（按 user 消息切分轮次）
    user_positions = [i for i, m in enumerate(body) if m.get("role") == "user"]
    if len(user_positions) <= min_retain_rounds:
        # 轮次本就不多，无可折叠
        return messages, False

    retain_start = user_positions[-min_retain_rounds]
    old_part = body[:retain_start]
    retained = body[retain_start:]

    if not old_part:
        return messages, False

    summarize = summarizer or _cheap_summary
    try:
        summary_text = summarize(old_part)
    except Exception:
        summary_text = _cheap_summary(old_part)

    summary_msg = {"role": "system", "content": summary_text}
    return head_system + [summary_msg] + retained, True


def compact_run_messages(
    messages: List[dict],
    *,
    prefix_len: int,
    token_threshold: int,
    keep_recent_groups: int,
    summarizer: Optional[Callable[[List[dict]], str]] = None,
) -> Tuple[List[dict], bool]:
    """单任务内压缩：折叠 ReAct 循环里较早的 assistant+tool 分组。

    messages[:prefix_len] 为 system/跨轮历史/当前 user，永不触碰。
    messages[prefix_len:] 按 role=="assistant" 切分分组（一个 assistant 消息 +
    紧随其后的所有 tool 消息 = 一组），保证折叠永远整组进行，不会拆开
    assistant(tool_calls) 与其 tool 响应的配对。

    若估算 token 未超过 token_threshold，或分组数不足以在保留
    keep_recent_groups 组的前提下再折叠，则原样返回 (messages, False)。
    否则保留最近 keep_recent_groups 组，更早的组折叠为一条 system 摘要消息。
    """
    if estimate_tokens(messages) <= token_threshold:
        return messages, False

    prefix = messages[:prefix_len]
    body = messages[prefix_len:]

    group_starts = [i for i, m in enumerate(body) if m.get("role") == "assistant"]
    if len(group_starts) <= keep_recent_groups:
        return messages, False

    fold_upto = group_starts[-keep_recent_groups]
    old_part = body[:fold_upto]
    retained = body[fold_upto:]

    if not old_part:
        return messages, False

    summarize = summarizer or _cheap_summary
    try:
        summary_text = summarize(old_part)
    except Exception:
        summary_text = _cheap_summary(old_part)

    summary_msg = {"role": "system", "content": summary_text}
    return prefix + [summary_msg] + retained, True
