"""Ollama structured-output adapter with bounded, fail-closed behavior."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from katilim_analiz.contracts import CleanDocument
from katilim_analiz.llm.cache import (
    CachedModelAnswer,
    ModelResponseCache,
    model_response_cache_key,
)
from katilim_analiz.llm.contracts import (
    MAX_QUOTE_CHARS,
    ModelExtractionResponse,
    ModelFactField,
    ModelFactProposal,
    model_extraction_output_schema,
)
from katilim_analiz.llm.prompt import (
    PROMPT_VERSION,
    PromptPackage,
    build_prompt_package,
    build_system_prompt,
    select_model_fields,
)

_MAX_RESPONSE_BYTES = 1_000_000


class ModelFailureCode(StrEnum):
    CIRCUIT_OPEN = "model_circuit_open"
    TIMEOUT = "model_timeout"
    TRANSPORT = "model_transport_error"
    HTTP_STATUS = "model_http_status"
    RESPONSE_TOO_LARGE = "model_response_too_large"
    ENVELOPE_INVALID = "model_envelope_invalid"
    OUTPUT_TRUNCATED = "model_output_truncated"
    THINKING_PRESENT = "model_thinking_present"
    JSON_INVALID = "model_json_invalid"
    SCHEMA_INVALID = "model_schema_invalid"
    DOCUMENT_MISMATCH = "model_document_mismatch"
    FIELD_NOT_REQUESTED = "model_field_not_requested"
    BLOCK_NOT_SENT = "model_block_not_sent"


#: Codes proving the local model service itself is unhealthy: it never answered
#: inside the budget, or answered outside its own API contract.  Retrying these
#: burns a full inference slot, so they trip the breaker quickly.
DEPENDENCY_FAILURE_CODES: frozenset[ModelFailureCode] = frozenset(
    {
        ModelFailureCode.TIMEOUT,
        ModelFailureCode.TRANSPORT,
        ModelFailureCode.HTTP_STATUS,
        ModelFailureCode.ENVELOPE_INVALID,
        ModelFailureCode.RESPONSE_TOO_LARGE,
    }
)


def is_dependency_failure(code: ModelFailureCode) -> bool:
    """Answer whether a failure says the service is sick or the answer was rejected.

    A rejected quote means the grounding guard did its job on a live, responsive
    model; it is not evidence that the next document would fail too.
    """

    return code in DEPENDENCY_FAILURE_CODES


class ModelInferenceError(RuntimeError):
    def __init__(self, code: ModelFailureCode, detail: str) -> None:
        self.code = code
        super().__init__(detail)


class ModelInferenceSkipped(RuntimeError):
    """No physical inference was made because no safe, relevant source was available."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _EnvelopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _OllamaMessage(_EnvelopeModel):
    role: Literal["assistant"]
    content: Annotated[str, StringConstraints(max_length=_MAX_RESPONSE_BYTES)]
    thinking: str | None = None


class _OllamaEnvelope(_EnvelopeModel):
    model: str
    created_at: str
    message: _OllamaMessage
    done: bool
    done_reason: str | None = None
    total_duration: Annotated[int, Field(ge=0)] | None = None
    load_duration: Annotated[int, Field(ge=0)] | None = None
    prompt_eval_count: Annotated[int, Field(ge=0)] | None = None
    prompt_eval_duration: Annotated[int, Field(ge=0)] | None = None
    eval_count: Annotated[int, Field(ge=0)] | None = None
    eval_duration: Annotated[int, Field(ge=0)] | None = None


class _LiveModelFact(_EnvelopeModel):
    field: ModelFactField
    quote: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUOTE_CHARS),
    ]


class _LiveModelBody(_EnvelopeModel):
    facts: Annotated[list[_LiveModelFact], Field(max_length=1)]


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    """Small in-process breaker; one half-open probe is permitted."""

    failure_threshold: int = 2
    #: Rejected model content is expected on a small local model, so it needs its
    #: own, far more patient budget before the model is taken out of service.
    content_failure_threshold: int = 12
    recovery_seconds: float = 120.0
    monotonic: Callable[[], float] = time.monotonic
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    content_failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False

    def acquire(self) -> None:
        now = self.monotonic()
        if self.state is CircuitState.OPEN:
            assert self.opened_at is not None
            if now - self.opened_at < self.recovery_seconds:
                raise ModelInferenceError(
                    ModelFailureCode.CIRCUIT_OPEN,
                    "local model circuit is cooling down",
                )
            self.state = CircuitState.HALF_OPEN
        if self.state is CircuitState.HALF_OPEN:
            if self.probe_in_flight:
                raise ModelInferenceError(
                    ModelFailureCode.CIRCUIT_OPEN,
                    "local model recovery probe is already running",
                )
            self.probe_in_flight = True

    def success(self) -> None:
        self.state = CircuitState.CLOSED
        self.failures = 0
        self.content_failures = 0
        self.opened_at = None
        self.probe_in_flight = False

    def failure(self, *, dependency: bool = True) -> None:
        self.probe_in_flight = False
        if not dependency:
            self._content_failure()
            return
        self.failures += 1
        if self.state is CircuitState.HALF_OPEN or self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = self.monotonic()

    def _content_failure(self) -> None:
        self.content_failures += 1
        if self.content_failures >= self.content_failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = self.monotonic()
            return
        if self.state is CircuitState.HALF_OPEN:
            # The probe reached a responsive model and only its answer was
            # rejected, so the dependency has recovered even though this
            # document gained nothing.
            self.state = CircuitState.CLOSED
            self.failures = 0
            self.opened_at = None


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_finite,
    )


class OllamaStructuredClient:
    """Call one configured local endpoint; returned facts remain untrusted suggestions."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        model_digest: str,
        timeout_seconds: float,
        max_context: int,
        keep_alive: str,
        concurrency: int = 1,
        http_client: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        response_cache: ModelResponseCache | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_context < 512 or concurrency <= 0:
            raise ValueError("invalid local model client limits")
        if len(model_digest) != 64 or any(
            character not in "0123456789abcdef" for character in model_digest
        ):
            raise ValueError("model_digest must be a lowercase SHA-256 digest")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._model_digest = model_digest
        self._timeout = timeout_seconds
        self._max_context = max_context
        self._keep_alive = keep_alive
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client = http_client
        self._circuit = circuit_breaker or CircuitBreaker()
        self._response_cache = response_cache

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def model_digest(self) -> str:
        return self._model_digest

    async def extract(
        self,
        document: CleanDocument,
        requested_fields: frozenset[ModelFactField],
    ) -> ModelExtractionResponse:
        selected_fields = select_model_fields(document, requested_fields)
        if not selected_fields:
            raise ModelInferenceSkipped("no_relevant_source_signal")
        package = build_prompt_package(document, selected_fields)
        if not package.included_block_ids:
            raise ModelInferenceSkipped("no_safe_source_blocks")
        cache_key = model_response_cache_key(
            model_digest=self._model_digest,
            prompt_version=PROMPT_VERSION,
            requested_fields=selected_fields,
            user_content=package.user_content,
        )
        replayed = self._replay_cached_response(cache_key, document.id, selected_fields, package)
        if replayed is not None:
            return replayed
        request_body: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": build_system_prompt(selected_fields)},
                {"role": "user", "content": package.user_content},
            ],
            "stream": False,
            "think": False,
            "format": model_extraction_output_schema(selected_fields),
            "options": {
                "temperature": 0,
                "num_ctx": self._max_context,
                # CPU generation is about 2 tokens/s in the measured profile.
                # Bound the response so valid completion can fit the 120s wall
                # deadline; a larger/incomplete answer fails closed to rules.
                "num_predict": 192,
            },
            # Ollama's HTTP API accepts negative infinity as a JSON number, while
            # positive Go-style durations remain strings (for example, "30m").
            "keep_alive": -1 if self._keep_alive == "-1" else self._keep_alive,
        }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        circuit_acquired = False
        try:
            # The caller owns one wall-clock budget.  Starting the timeout
            # before the semaphore prevents queue wait from silently adding a
            # second full model timeout under CPU contention.
            async with asyncio.timeout(self._timeout):
                async with self._semaphore:
                    self._circuit.acquire()
                    circuit_acquired = True
                    envelope = await self._request(request_body)
                    result = await asyncio.to_thread(
                        self._parse_envelope,
                        envelope,
                        document.id,
                    )
                    self._validate_facts(result, selected_fields, package)
                    # Field checks are synchronous.  Enforce the same absolute
                    # deadline once that final validation work returns.
                    if loop.time() >= deadline:
                        raise TimeoutError
        except TimeoutError as exc:
            if circuit_acquired:
                self._circuit.failure()
            raise ModelInferenceError(
                ModelFailureCode.TIMEOUT,
                f"local model exceeded the configured {self._timeout:g}s timeout",
            ) from exc
        except ModelInferenceError as exc:
            if circuit_acquired:
                self._circuit.failure(dependency=is_dependency_failure(exc.code))
            raise
        self._circuit.success()
        if self._response_cache is not None:
            self._response_cache.put(
                cache_key,
                CachedModelAnswer(
                    facts=tuple((fact.field, fact.proposed_quote) for fact in result.facts)
                ),
            )
        return result

    def _replay_cached_response(
        self,
        cache_key: str,
        document_id: str,
        selected_fields: frozenset[ModelFactField],
        package: PromptPackage,
    ) -> ModelExtractionResponse | None:
        """Rebuild a cached validated answer and re-run the live validation path.

        Any defect in the stored entry — contract violation, unrequested field,
        quote not visible in the current bounded request — degrades to a cache
        miss so the live model is consulted instead.  The verbatim grounding
        check against the current document still happens downstream at the
        acceptance boundary, exactly as for a live response.
        """

        if self._response_cache is None:
            return None
        cached = self._response_cache.get(cache_key)
        if cached is None:
            return None
        try:
            response = ModelExtractionResponse(
                schema_version="model-extraction/1.1",
                document_id=document_id,
                facts=[
                    ModelFactProposal(field=field, quote=quote) for field, quote in cached.facts
                ],
            )
            self._validate_facts(response, selected_fields, package)
        except (ValidationError, ModelInferenceError):
            return None
        return response

    @staticmethod
    def _validate_facts(
        result: ModelExtractionResponse,
        selected_fields: frozenset[ModelFactField],
        package: PromptPackage,
    ) -> None:
        for fact in result.facts:
            if fact.field not in selected_fields:
                raise ModelInferenceError(
                    ModelFailureCode.FIELD_NOT_REQUESTED,
                    f"model returned unrequested field: {fact.field.value}",
                )
            block_id = fact.proposed_block_id
            if block_id is not None and block_id not in package.included_block_ids:
                raise ModelInferenceError(
                    ModelFailureCode.BLOCK_NOT_SENT,
                    "model cited a block that was not in its bounded request",
                )
            visible_blocks = (
                (sent_id, sent_text)
                for sent_id, sent_text in package.included_text_by_block
                if block_id is None or sent_id == block_id
            )
            if not any(fact.proposed_quote in sent_text for _sent_id, sent_text in visible_blocks):
                raise ModelInferenceError(
                    ModelFailureCode.BLOCK_NOT_SENT,
                    "model quote was not present in the bounded request",
                )

    async def _request(self, body: dict[str, object]) -> bytes:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )
        try:
            try:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/api/chat",
                    json=body,
                    timeout=httpx.Timeout(self._timeout),
                ) as response:
                    if response.status_code != 200:
                        raise ModelInferenceError(
                            ModelFailureCode.HTTP_STATUS,
                            f"local model returned HTTP {response.status_code}",
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None and int(declared) > _MAX_RESPONSE_BYTES:
                        raise ModelInferenceError(
                            ModelFailureCode.RESPONSE_TOO_LARGE,
                            "local model response exceeds byte limit",
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > _MAX_RESPONSE_BYTES:
                            raise ModelInferenceError(
                                ModelFailureCode.RESPONSE_TOO_LARGE,
                                "local model response exceeds byte limit",
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.TimeoutException as exc:
                raise ModelInferenceError(
                    ModelFailureCode.TIMEOUT,
                    f"local model exceeded the configured {self._timeout:g}s timeout",
                ) from exc
            except httpx.HTTPError as exc:
                raise ModelInferenceError(
                    ModelFailureCode.TRANSPORT,
                    "local model transport failed",
                ) from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _parse_envelope(raw: bytes, document_id: str) -> ModelExtractionResponse:
        try:
            envelope_payload = _strict_json_loads(raw)
            envelope = _OllamaEnvelope.model_validate(envelope_payload)
        except (UnicodeDecodeError, ValueError, ValidationError) as exc:
            raise ModelInferenceError(
                ModelFailureCode.ENVELOPE_INVALID,
                "local model response envelope is invalid",
            ) from exc
        if not envelope.done:
            raise ModelInferenceError(
                ModelFailureCode.ENVELOPE_INVALID,
                "local model response is incomplete",
            )
        if envelope.done_reason == "length":
            raise ModelInferenceError(
                ModelFailureCode.OUTPUT_TRUNCATED,
                "local model exhausted its output-token budget",
            )
        if envelope.message.thinking:
            raise ModelInferenceError(
                ModelFailureCode.THINKING_PRESENT,
                "local model returned hidden reasoning despite think=false",
            )
        try:
            payload = _strict_json_loads(envelope.message.content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ModelInferenceError(
                ModelFailureCode.JSON_INVALID,
                "local model content is not strict JSON",
            ) from exc
        try:
            live_body = _LiveModelBody.model_validate(payload)
        except ValidationError as exc:
            raise ModelInferenceError(
                ModelFailureCode.SCHEMA_INVALID,
                "local model content does not match the extraction schema",
            ) from exc
        try:
            return ModelExtractionResponse(
                schema_version="model-extraction/1.1",
                document_id=document_id,
                facts=[
                    ModelFactProposal(field=fact.field, quote=fact.quote)
                    for fact in live_body.facts
                ],
            )
        except ValidationError as exc:
            raise ModelInferenceError(
                ModelFailureCode.SCHEMA_INVALID,
                "local model facts violate the extraction contract",
            ) from exc


__all__ = [
    "PROMPT_VERSION",
    "CircuitBreaker",
    "CircuitState",
    "ModelFailureCode",
    "ModelInferenceError",
    "ModelInferenceSkipped",
    "OllamaStructuredClient",
]
