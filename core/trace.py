"""TraceLogger - 轻量 JSONL 执行追踪

职责：
- 为一次任务（run）生成结构化 trace，逐事件追加写入 `.jsonl`
- 记录每次 LLM 调用（model / latency / token usage）与工具调用（name / arguments / result / error）
- 敏感信息脱敏（API key / token 等）+ 超长字段截断
- 可选导出简易 HTML 报告

设计原则：trace 是旁路观测，任何写入异常都被吞掉，绝不影响 Agent 主流程。
"""

import json
import os
import re
import time
import uuid
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

# 键名命中时，其值整体脱敏（精确匹配，避免 total_tokens 之类误伤）
_SENSITIVE_KEY_EXACT = frozenset({
    "api_key", "apikey", "authorization", "token", "password",
    "secret", "access_key", "access_token", "auth_token", "secret_key",
})


def _is_sensitive_key(key: Any) -> bool:
    """判断键名是否敏感。精确名或以 _token/_key/_secret 结尾，或含 api_key/password。"""
    k = str(key).lower()
    if k in _SENSITIVE_KEY_EXACT:
        return True
    if k.endswith(("_token", "_key", "_secret", "_password")):
        return True
    return "api_key" in k or "apikey" in k or "password" in k

# 字符串内联敏感片段的脱敏规则
_SENSITIVE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),          # OpenAI 风格密钥
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"),    # Bearer token
)

_REDACTED = "***REDACTED***"


class TraceLogger:
    """JSONL 执行追踪记录器。

    用法：
    ```python
    tracer = TraceLogger(trace_dir="memory/traces")
    path = tracer.start_run()
    tracer.record("agent_start", input="...")
    tracer.record("llm_call", model="gpt-4", latency_ms=120, usage={...})
    html = tracer.write_html_report()
    ```
    """

    def __init__(
        self,
        trace_dir: str = "memory/traces",
        sanitize: bool = True,
        max_field_len: int = 2000,
    ) -> None:
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.sanitize = sanitize
        self.max_field_len = max_field_len

        self._run_id: Optional[str] = None
        self._path: Optional[Path] = None
        self._seq = 0
        self._records: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 运行生命周期
    # ------------------------------------------------------------------

    def start_run(self, run_id: Optional[str] = None) -> str:
        """开启一次新的 trace 运行，返回 JSONL 文件路径。"""
        if run_id is None:
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            run_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
        self._run_id = run_id
        self._path = self.trace_dir / f"trace-{run_id}.jsonl"
        self._seq = 0
        self._records = []
        return str(self._path)

    @property
    def run_id(self) -> Optional[str]:
        return self._run_id

    @property
    def path(self) -> Optional[str]:
        return str(self._path) if self._path else None

    def record(self, event: str, **data: Any) -> None:
        """记录一个事件（追加一行 JSON）。任何异常都被吞掉。"""
        if self._path is None:
            return
        try:
            self._seq += 1
            entry = {
                "run_id": self._run_id,
                "seq": self._seq,
                "ts": time.time(),
                "event": event,
                "data": self._sanitize(data),
            }
            self._records.append(entry)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # 观测旁路，绝不影响主流程
            pass

    # ------------------------------------------------------------------
    # HTML 报告
    # ------------------------------------------------------------------

    def write_html_report(self, include_raw: bool = False) -> Optional[str]:
        """把当前运行的记录渲染成简易 HTML 表格，返回 HTML 路径。"""
        if self._path is None:
            return None
        try:
            html_path = self.trace_dir / f"trace-{self._run_id}.html"
            rows = []
            for r in self._records:
                ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
                data_str = json.dumps(r["data"], ensure_ascii=False, indent=2)
                rows.append(
                    "<tr>"
                    f"<td>{r['seq']}</td>"
                    f"<td>{ts}</td>"
                    f"<td><b>{escape(r['event'])}</b></td>"
                    f"<td><pre>{escape(data_str)}</pre></td>"
                    "</tr>"
                )
            html = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                f"<title>Trace {escape(self._run_id or '')}</title>"
                "<style>body{font-family:sans-serif;margin:20px}"
                "table{border-collapse:collapse;width:100%}"
                "td,th{border:1px solid #ccc;padding:6px;vertical-align:top;text-align:left}"
                "pre{margin:0;white-space:pre-wrap;font-size:12px}"
                "th{background:#f0f0f0}</style></head><body>"
                f"<h2>Trace: {escape(self._run_id or '')}</h2>"
                f"<p>共 {len(self._records)} 条事件</p>"
                "<table><tr><th>#</th><th>时间</th><th>事件</th><th>数据</th></tr>"
                + "".join(rows)
                + "</table></body></html>"
            )
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)
            return str(html_path)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 脱敏 / 截断
    # ------------------------------------------------------------------

    def _sanitize(self, obj: Any) -> Any:
        """递归脱敏并截断长字段。"""
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if self.sanitize and _is_sensitive_key(k):
                    result[k] = _REDACTED
                else:
                    result[k] = self._sanitize(v)
            return result
        if isinstance(obj, (list, tuple)):
            return [self._sanitize(v) for v in obj]
        if isinstance(obj, str):
            return self._sanitize_str(obj)
        return obj

    def _sanitize_str(self, text: str) -> str:
        if self.sanitize:
            for pattern in _SENSITIVE_PATTERNS:
                text = pattern.sub(_REDACTED, text)
        if len(text) > self.max_field_len:
            text = text[: self.max_field_len] + "...(truncated)"
        return text
