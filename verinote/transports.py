"""Trusted HTTP adapter boundary; no HTTP implementation or credential loading."""
from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExecutionOptions:
    adapter_contract_version: int = 1
    max_tokens: int = 16384
    timeout: float = 180

    def __post_init__(self):
        if type(self.adapter_contract_version) is not int or self.adapter_contract_version != 1:
            raise ValueError('지원하지 않는 어댑터 계약 버전')
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError('max_tokens는 양의 정수여야 합니다.')
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout < 0:
            raise ValueError('timeout은 0 이상의 유한한 초 값이어야 합니다.')


@dataclass(frozen=True)
class Credentials:
    """Server-memory-only value. Never serialize this object."""
    api_key: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError('OpenRouter API 자격 증명이 구성되지 않았습니다.')


@dataclass(frozen=True)
class SafeCallResult:
    """Adapter attestation: metadata is bounded and secret checked by the adapter.

    success means a complete HTTP response, independently of domain JSON validity.
    Only a complete, size/secret-checked response may set response_verified and
    supply safe_response_payload. The sink owns any persistence of this data.
    """
    call_id: str
    transport_status: str
    safe_metadata: dict = field(default_factory=dict)
    response_verified: bool = False
    safe_response_payload: dict | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError('호출 ID가 필요합니다.')
        if self.transport_status not in ('success', 'error', 'timeout', 'not_started'):
            raise ValueError('알 수 없는 transport 상태')
        if self.safe_response_payload is not None and (not self.response_verified or self.transport_status != 'success'):
            raise ValueError('검사가 끝난 완전한 응답만 payload를 제공할 수 있습니다.')


@dataclass(frozen=True)
class HTTPResult:
    envelope: SafeCallResult
    response: str | None = field(default=None, repr=False)


class HTTPAdapter(Protocol):
    def ready(self, credentials: Credentials) -> bool:
        """Local configuration check; must not make network requests."""
        ...

    def complete(self, agent: str, model: str, prompt: str, folder: Path, *,
                 phase: str | None, options: ExecutionOptions,
                 credentials: Credentials, call_id: str) -> HTTPResult:
        """Return a terminal checked envelope even on transport failure.

        Transport exceptions must be sanitized by the adapter; credentials,
        request headers and unchecked responses must never enter the envelope.
        """
        ...
