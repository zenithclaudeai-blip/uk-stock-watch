"""
LSE Opportunity Scanner - AI Provider Gateway.

The investment scanner must remain fully functional if any single AI
provider is unavailable, out of credits, rate-limited, or down. This
module is the ONLY place that knows how to talk to a specific AI
provider's API - ai_evidence.py's Bull/Bear logic never imports
urllib or knows about Anthropic-specific request shapes directly; it
asks the gateway for a completion and gets back either a real result
or an honest failure reason.

Currently only Anthropic is actually wired (the only credential this
project has), but the adapter interface is provider-agnostic - adding
OpenAI or Google later means implementing one more AIProviderAdapter
subclass and registering it, never touching the Bull/Bear/Supervisor
logic that calls the gateway.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class AICallResult:
    """What every provider adapter returns - success or a genuine,
    specific failure reason, never a silent None with no explanation."""
    success: bool
    text: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    latency_seconds: Optional[float] = None
    error_type: Optional[str] = None  # "credits", "rate_limit", "timeout", "auth", "server_error", "network", "other"
    error_detail: Optional[str] = None


class AIProviderAdapter(ABC):
    """One implementation per real provider. The gateway below never
    calls a provider's HTTP API directly - only through this interface,
    so adding a new provider never requires touching gateway logic."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this provider genuinely has credentials available
        this run - checked before attempting a call, not discovered
        via a failed request."""
        ...

    @abstractmethod
    def call(self, system_prompt: str, user_message: str, max_tokens: int = 500) -> AICallResult: ...


class AnthropicAdapter(AIProviderAdapter):
    MODEL = "claude-haiku-4-5-20251001"

    def name(self) -> str:
        return "Anthropic"

    def is_configured(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def call(self, system_prompt: str, user_message: str, max_tokens: int = 500) -> AICallResult:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return AICallResult(success=False, provider=self.name(), error_type="auth",
                                 error_detail="No ANTHROPIC_API_KEY configured")
        body = json.dumps({
            "model": self.MODEL, "max_tokens": max_tokens, "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
        )
        start = time.time()
        try:
            resp_data = json.loads(urllib.request.urlopen(req, timeout=30).read())
            latency = time.time() - start
            text = "".join(b.get("text", "") for b in resp_data.get("content", []) if b.get("type") == "text").strip()
            if not text:
                return AICallResult(success=False, provider=self.name(), model=self.MODEL,
                                     latency_seconds=latency, error_type="other", error_detail="Empty response")
            return AICallResult(success=True, text=text, provider=self.name(), model=self.MODEL,
                                 latency_seconds=latency)
        except urllib.error.HTTPError as e:
            latency = time.time() - start
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                error_body = "(couldn't read error body)"
            error_type = "other"
            if e.code == 429:
                error_type = "rate_limit"
            elif e.code in (401, 403):
                error_type = "auth"
            elif e.code == 400 and "credit" in error_body.lower():
                error_type = "credits"
            elif 500 <= e.code < 600:
                error_type = "server_error"
            return AICallResult(success=False, provider=self.name(), model=self.MODEL, latency_seconds=latency,
                                 error_type=error_type, error_detail=f"HTTP {e.code}: {error_body[:200]}")
        except TimeoutError:
            return AICallResult(success=False, provider=self.name(), model=self.MODEL,
                                 latency_seconds=time.time() - start, error_type="timeout",
                                 error_detail="Request timed out")
        except Exception as e:
            return AICallResult(success=False, provider=self.name(), model=self.MODEL,
                                 latency_seconds=time.time() - start, error_type="network", error_detail=str(e))


class AIProviderGateway:
    """
    Tries configured providers in priority order. Returns the FIRST
    genuine success, or an honest aggregate failure if every configured
    provider failed - never fabricates a result, never silently
    pretends success. Records every attempt (success or failure) for
    cost/performance tracking.
    """

    def __init__(self, adapters: list = None):
        self.adapters = adapters if adapters is not None else [AnthropicAdapter()]
        self.call_log = []  # every attempt this gateway instance has made, for cost/performance tracking

    def configured_providers(self) -> list:
        return [a.name() for a in self.adapters if a.is_configured()]

    def complete(self, system_prompt: str, user_message: str, max_tokens: int = 500) -> AICallResult:
        attempts = []
        for adapter in self.adapters:
            if not adapter.is_configured():
                continue
            result = adapter.call(system_prompt, user_message, max_tokens)
            attempts.append(result)
            self.call_log.append({
                "provider": result.provider, "model": result.model, "success": result.success,
                "errorType": result.error_type, "latencySeconds": result.latency_seconds,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if result.success:
                return result
            print(f"  ! AI provider {adapter.name()} failed ({result.error_type}): "
                  f"{result.error_detail}", file=sys.stderr)
        if not attempts:
            return AICallResult(success=False, error_type="auth",
                                 error_detail="No AI provider is configured this run")
        # All configured providers genuinely failed - return the last
        # failure, never a fabricated success.
        return attempts[-1]
