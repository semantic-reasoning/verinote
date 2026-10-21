"""Trusted HTTP adapter boundary; no HTTP implementation or credential loading."""
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

MIN_JOB_TOKENS = 1
MAX_JOB_TOKENS = 65536
MIN_ANSWER_TOKENS = 1024
MAX_REASONING_TOKENS = 4096
DEFAULT_ANSWER_TOKENS = 8192


def reasoning_budget(total, reserve):
    """Requested budget, not a provider-independent guarantee of reservation."""
    if type(reserve) is not int or reserve < MIN_ANSWER_TOKENS or reserve > total - 1024:
        raise ValueError('답변 목표 여유는 1,024토큰 이상이어야 하고 추론용으로도 1,024토큰 이상 남겨야 합니다. 총 한도를 2,048토큰 이상으로 조절하세요.')
    return min(MAX_REASONING_TOKENS, total - reserve)


def validate_job_tokens(value):
    """Validate only user overrides, not legacy/default execution options."""
    if type(value) is not int or not MIN_JOB_TOKENS <= value <= MAX_JOB_TOKENS:
        raise ValueError('토큰 한도는 1~65,536 사이의 정수여야 합니다.')
    return value


@dataclass(frozen=True)
class ExecutionOptions:
    adapter_contract_version: int = 1
    max_tokens: int = 16384
    timeout: float = 180
    reasoning_max_tokens: int | None = None
    answer_reserve_tokens: int | None = None

    def __post_init__(self):
        if type(self.adapter_contract_version) is not int or self.adapter_contract_version not in (1, 2):
            raise ValueError('지원하지 않는 어댑터 계약 버전')
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError('max_tokens는 양의 정수여야 합니다.')
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout < 0:
            raise ValueError('timeout은 0 이상의 유한한 초 값이어야 합니다.')
        if self.adapter_contract_version == 1:
            if self.reasoning_max_tokens is not None or self.answer_reserve_tokens is not None:
                raise ValueError('v1 실행에는 답변 여유 정책을 추가할 수 없습니다.')
        elif type(self.reasoning_max_tokens) is not int or self.reasoning_max_tokens != reasoning_budget(self.max_tokens, self.answer_reserve_tokens):
            raise ValueError('고정 추론 요청 예산이 답변 목표 여유와 일치하지 않습니다.')


@dataclass(frozen=True)
class Credentials:
    """Server-memory-only value. Never serialize this object."""
    api_key: str = field(repr=False)

    @classmethod
    def from_server(cls, credentials):
        """Explicit bridge from the already-loaded #703 server-memory value."""
        from .config import Credentials as ServerCredentials
        if not isinstance(credentials, ServerCredentials):
            raise TypeError('서버 자격 증명이 필요합니다.')
        return cls(credentials.openrouter_key())

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
    error_code: str | None = None


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


def job_options(value):
    """Strict secret-free persisted HTTP job snapshot; legacy CLI uses null."""
    required = {'adapter_contract_version', 'max_tokens', 'timeouts'}
    if isinstance(value, dict) and value.get('adapter_contract_version') == 2:
        required |= {'reasoning_max_tokens', 'answer_reserve_tokens'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('고정 HTTP 작업 옵션이 올바르지 않습니다. 작업을 다시 등록하세요.')
    times = value['timeouts']
    if not isinstance(times, dict) or set(times) != {'default', 'ontology'}:
        raise ValueError('고정 HTTP 단계별 timeout이 올바르지 않습니다.')
    for timeout in times.values():
        ExecutionOptions(value['adapter_contract_version'], value['max_tokens'], timeout,
                         value.get('reasoning_max_tokens'), value.get('answer_reserve_tokens'))
    result = {'adapter_contract_version': value['adapter_contract_version'], 'max_tokens': value['max_tokens'],
              'timeouts': dict(times)}
    if value['adapter_contract_version'] == 2:
        result.update(reasoning_max_tokens=value['reasoning_max_tokens'], answer_reserve_tokens=value['answer_reserve_tokens'])
    return result
