"""测试/验证输出解析 — 把常见 runner 的原始输出转成结构化结果

支持 unittest / pytest / compileall 的启发式解析；`passed` 以退出码为准
（这三者失败都返回非 0），解析只用于生成给 LLM 与 trace 的简洁摘要与失败定位。
"""

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class VerifyResult:
    """一次验证的结构化结果。"""

    passed: bool
    framework: str = "unknown"          # unittest / pytest / compileall / unknown
    summary: str = ""                   # 一行摘要（给 LLM / trace）
    failures: List[str] = field(default_factory=list)  # 失败项 / 错误行
    raw_tail: str = ""                  # 原始输出尾部（保留上下文）


def _detect_framework(text: str) -> str:
    low = text.lower()
    if "===" in text and re.search(r"\d+\s+(passed|failed|error)", low):
        return "pytest"
    if re.search(r"ran\s+\d+\s+test", low):
        return "unittest"
    if "compiling" in low or "compileall" in low or "syntaxerror" in low:
        return "compileall"
    return "unknown"


def parse_test_output(output: str, exit_code: int) -> VerifyResult:
    """解析验证命令输出；passed 以 exit_code==0 为准。"""
    text = output or ""
    lines = [ln.rstrip() for ln in text.splitlines()]
    passed = exit_code == 0
    framework = _detect_framework(text)
    failures: List[str] = []
    summary = ""

    if framework == "pytest":
        failures = [ln.strip() for ln in lines if ln.strip().startswith("FAILED")]
        summary_lines = [ln for ln in lines if re.search(r"\d+\s+(passed|failed|error)", ln)]
        if summary_lines:
            summary = summary_lines[-1].strip().strip("= ").strip()

    elif framework == "unittest":
        failures = [
            ln.strip() for ln in lines
            if ln.strip().startswith(("FAIL:", "ERROR:"))
        ]
        ran = next((ln.strip() for ln in lines if ln.strip().startswith("Ran ")), "")
        verdict = next(
            (ln.strip() for ln in reversed(lines)
             if ln.strip() == "OK" or ln.strip().startswith(("OK ", "FAILED"))),
            "",
        )
        summary = " ".join(x for x in (ran, verdict) if x)

    elif framework == "compileall":
        failures = [
            ln.strip() for ln in lines
            if "error" in ln.lower() or "***" in ln
        ]
        summary = "编译通过" if passed else f"编译失败（{len(failures)} 处错误行）"

    if not summary:
        summary = ("通过" if passed else "失败") + f"（exit={exit_code}）"

    return VerifyResult(
        passed=passed,
        framework=framework,
        summary=summary,
        failures=failures[:10],
        raw_tail="\n".join(lines[-30:]),
    )
