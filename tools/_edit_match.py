"""编辑匹配与原子写入的共享逻辑（apply_patch / multi_edit 复用）

- find_and_replace：先精确匹配（要求唯一），失败再做「忽略行首尾空白」的
  按行模糊匹配；找到唯一块则替换，并返回是否走了模糊路径。
- atomic_write_text：临时文件 + os.replace 原子写入。
"""

import os
from pathlib import Path
from typing import Optional, Tuple


class EditMatchError(Exception):
    """old_str 为空 / 未找到 / 有歧义时抛出（由调用方转成 ToolResult.fail）。"""


def find_and_replace(content: str, old_str: str, new_str: str) -> Tuple[str, bool]:
    """在 content 中把 old_str 替换为 new_str，返回 (新内容, 是否模糊匹配)。

    匹配策略：
      1. 精确匹配：恰好出现一次 → 直接替换（fuzzy=False）。
      2. 出现 ≥2 次 → 抛 EditMatchError（歧义）。
      3. 出现 0 次 → 按行「忽略行首尾空白」模糊匹配；唯一命中则替换（fuzzy=True），
         否则抛 EditMatchError（未找到 / 模糊歧义）。

    模糊匹配只用于「定位」，替换内容仍按 new_str 原样写入（缩进由调用方保证）。
    """
    if not old_str:
        raise EditMatchError("old_str 不能为空")

    count = content.count(old_str)
    if count == 1:
        return content.replace(old_str, new_str, 1), False
    if count >= 2:
        raise EditMatchError(
            f"old_str 出现 {count} 次，存在歧义，请提供更多上下文以唯一定位"
        )

    # count == 0 → 模糊匹配
    fuzzy = _fuzzy_replace(content, old_str, new_str)
    if fuzzy is None:
        raise EditMatchError(
            "未找到 old_str（精确与模糊匹配均失败），"
            "请检查缩进和换行符是否与文件内容一致"
        )
    return fuzzy, True


def _fuzzy_replace(content: str, old_str: str, new_str: str) -> Optional[str]:
    """按行忽略首尾空白的模糊匹配；唯一命中返回新内容，0 或多处命中返回 None。"""
    file_lines = content.split("\n")
    old_lines = old_str.split("\n")
    # old_str 末尾换行会引入一个空串行，去掉以匹配「整行」语义
    if len(old_lines) > 1 and old_lines[-1] == "":
        old_lines = old_lines[:-1]

    n = len(old_lines)
    norm_old = [ln.strip() for ln in old_lines]
    # 规范化后全为空 → 不做模糊匹配，避免命中任意空行
    if n == 0 or all(s == "" for s in norm_old):
        return None

    matches = [
        i for i in range(0, len(file_lines) - n + 1)
        if [w.strip() for w in file_lines[i:i + n]] == norm_old
    ]
    if len(matches) != 1:
        return None  # 0 处或多处命中：交给调用方报错

    i = matches[0]
    new_lines = new_str.split("\n")
    if len(new_lines) > 1 and new_lines[-1] == "" and new_str.endswith("\n"):
        new_lines = new_lines[:-1]
    result = file_lines[:i] + new_lines + file_lines[i + n:]
    return "\n".join(result)


def atomic_write_text(target: Path, content: str) -> None:
    """原子写入：先写临时文件，再 os.replace 覆盖目标。"""
    tmp_path = str(target) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, target)
