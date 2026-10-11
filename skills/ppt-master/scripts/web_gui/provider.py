#!/usr/bin/env python3
"""PPT Master - Compatible Model Provider

Load private local credentials and call the configured chat-completions endpoint.

Usage:
    Import ModelClient from the Web GUI pipeline.

Examples:
    client = ModelClient(load_config())
    text = client.complete("Return JSON.", "Outline this topic.")

Dependencies:
    requests
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests


@dataclass
class ProviderConfig:
    api_key: str = field(repr=False)
    base_url: str = "https://apihub.agnes-ai.com/v1"
    model: str = "agnes-2.5-flash"


class ProviderError(RuntimeError):
    """Represent a sanitized API failure with retry/recovery eligibility."""

    def __init__(self, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.recoverable = recoverable


def _retry_delay(response, attempt: int) -> float:
    limited = response is not None and response.status_code == 429
    delay = (20.0 if limited else 1.5) * (attempt + 1)
    value = response.headers.get("Retry-After") if response is not None else None
    if isinstance(value, str):
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    return max(0.0, min(delay, 60.0))


def config_path() -> Path:
    """Return the private settings location outside project artifacts."""
    override = os.environ.get("PPT_MASTER_PROVIDER_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".ppt-master" / "web-gui-provider.json"


def load_config() -> ProviderConfig:
    """Read credentials without including their values in diagnostics."""
    path = config_path()
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderError(f"模型配置无法读取，请修复 {path}。", recoverable=False) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"模型配置必须是 JSON 对象：{path}。", recoverable=False)
    key = os.environ.get("PPT_MASTER_LLM_API_KEY", data.get("api_key", ""))
    base = os.environ.get("PPT_MASTER_LLM_BASE_URL", data.get("base_url", "https://apihub.agnes-ai.com/v1"))
    model = os.environ.get("PPT_MASTER_LLM_MODEL", data.get("model", "agnes-2.5-flash"))
    if not all(isinstance(value, str) and value.strip() for value in (key, base, model)):
        raise ProviderError(f"请在 {path} 配置 api_key、base_url 和 model。", recoverable=False)
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise ProviderError("模型 base_url 必须是有效的 HTTPS API 地址，不得包含凭据或查询参数。", recoverable=False)
    return ProviderConfig(key.strip(), base.rstrip("/"), model.strip())


class ModelClient:
    """Call one configured model with bounded, cancellation-aware retries."""

    def __init__(self, config: ProviderConfig, *, session=None, cancel_event=None, progress=None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.cancel_event = cancel_event or threading.Event()
        self.progress = progress or (lambda *args, **kwargs: None)

    def complete(self, system: str, user: str | list, *, temperature: float = 0.3, max_tokens: int = 8192,
                 json_mode: bool = False) -> str:
        """Return assistant text; never return partial or silently truncated content."""
        payload = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature, "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(3):
            if self.cancel_event.is_set():
                raise InterruptedError("生成已取消。")
            try:
                response = self.session.post(
                    self.config.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.config.api_key, "Content-Type": "application/json"},
                    json=payload, timeout=(15, 120), allow_redirects=False,
                )
            except requests.RequestException:
                status = None
                response = None
            else:
                status = response.status_code
            if status is None or status in {408, 429, 500, 502, 503, 504}:
                if attempt == 2:
                    reason = f"HTTP {status}" if status is not None else "网络连接或读取超时"
                    raise ProviderError(f"模型服务暂不可用（{reason}），已重试 3 次。请稍后重试并检查服务状态。")
                delay = _retry_delay(response, attempt)
                reason = f"HTTP {status}" if status is not None else "连接或读取超时"
                self.progress("retrying", f"模型请求暂未成功（{reason}），{delay:g} 秒后重试（{attempt + 2}/3）。")
                if self.cancel_event.wait(delay):
                    raise InterruptedError("生成已取消。")
                continue
            if status in {401, 403}:
                raise ProviderError("模型鉴权失败，请检查本机配置中的 API Key 与模型访问权限。", recoverable=False)
            if status != 200:
                raise ProviderError(f"模型接口返回 HTTP {status}，请检查 Base URL、模型名称和请求限制。",
                                    recoverable=False)
            try:
                data = response.json()
                choice = data["choices"][0]
                content = choice["message"]["content"]
                finish = choice.get("finish_reason")
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ProviderError("模型返回格式不符合 chat/completions 协议，请检查服务兼容性。") from exc
            if finish == "length":
                raise ProviderError("模型输出被长度限制截断；需要缩短内容或通过错误恢复重新生成完整结果。")
            if finish in {"content_filter", "refusal"}:
                raise ProviderError("模型未能处理此内容，请调整输入后重试。", recoverable=False)
            if not isinstance(content, str) or not content.strip():
                raise ProviderError("模型返回了空内容，请重试。")
            if self.cancel_event.is_set():
                raise InterruptedError("生成已取消。")
            return content.strip()
        raise ProviderError("模型请求未完成。")
