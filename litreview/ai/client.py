"""AI provider adapter with budget enforcement, caching and usage records.

The provider is behind the small :class:`Provider` protocol so tests can use a fake and another
provider could be added later. The Claude provider uses the official ``anthropic`` SDK and reads
credentials from the environment; keys are never stored, logged or exported.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from litreview.config import Settings, api_key_configured
from litreview.context import AppContext
from litreview.db import now

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.2  # conservative (over-estimates tokens) for budgeting
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


class AIError(Exception):
    """Base class. ``user_message`` is safe to show in the UI (no secrets, no document text)."""

    retryable = False

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class AINotConfigured(AIError):
    pass


class BudgetExceeded(AIError):
    pass


class AIRefused(AIError):
    pass


class AIOutputInvalid(AIError):
    pass


class AIProviderError(AIError):
    def __init__(self, user_message: str, retryable: bool) -> None:
        super().__init__(user_message)
        self.retryable = retryable


@dataclass
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    request_id: str | None
    model: str


class Provider(Protocol):
    name: str

    def complete(
        self, *, model: str, system: str, user: str, schema: dict, max_tokens: int, effort: str
    ) -> ProviderResponse: ...


class ClaudeProvider:
    """Calls the Claude Messages API with JSON-schema structured output."""

    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            max_retries=settings.ai_max_retries, timeout=float(settings.ai_timeout_seconds)
        )
        self._use_fallback = settings.ai_refusal_fallback

    def complete(
        self, *, model: str, system: str, user: str, schema: dict, max_tokens: int, effort: str
    ) -> ProviderResponse:
        anthropic = self._anthropic
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }
        try:
            if self._use_fallback and model in FALLBACK_MODELS:
                try:
                    with self._client.beta.messages.stream(
                        **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                    ) as stream:
                        message = stream.get_final_message()
                except anthropic.BadRequestError as exc:
                    if "fallback" not in str(exc).lower():
                        raise
                    log.warning("Refusal fallback not accepted for this account; disabling it")
                    self._use_fallback = False
                    with self._client.messages.stream(**params) as stream:
                        message = stream.get_final_message()
            else:
                with self._client.messages.stream(**params) as stream:
                    message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise AIProviderError("The API key was rejected (authentication error).", False) from exc
        except anthropic.PermissionDeniedError as exc:
            raise AIProviderError("The API key lacks permission for this model.", False) from exc
        except anthropic.NotFoundError as exc:
            raise AIProviderError(f"Model '{model}' was not found. Check LITREVIEW_MODEL.", False) from exc
        except anthropic.RateLimitError as exc:
            raise AIProviderError("Rate limited by the API after retries. Try again later.", True) from exc
        except anthropic.BadRequestError as exc:
            raise AIProviderError(
                f"The API rejected the request: {getattr(exc, 'message', exc)}", False
            ) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code == 529
            raise AIProviderError(f"API error (HTTP {exc.status_code}).", retryable) from exc
        except anthropic.APIConnectionError as exc:
            raise AIProviderError("Could not reach the API (network error).", True) from exc

        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        usage = message.usage
        input_tokens = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens or 0,
            stop_reason=message.stop_reason,
            request_id=getattr(message, "_request_id", None),
            model=getattr(message, "model", model) or model,
        )


@dataclass
class AIRequest:
    purpose: str
    system: str
    user: str
    output_model: type[BaseModel]
    prompt_version: str
    max_tokens: int | None = None
    expected_output_tokens: int = 4000


@dataclass
class AIResult:
    data: BaseModel
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool
    model: str
    call_id: int


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def strict_schema(model: type[BaseModel]) -> dict:
    """JSON schema for structured outputs: refs inlined, no extra keys, all properties required."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            out = {k: walk(v) for k, v in node.items() if k not in ("title", "default", "examples")}
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(raw)


class AIService:
    def __init__(self, ctx: AppContext, provider: Provider | None = None) -> None:
        self.ctx = ctx
        self.settings = ctx.settings
        self._provider = provider

    # -- configuration ---------------------------------------------------------------------------

    @property
    def model(self) -> str:
        return self.settings.ai_model

    def available(self) -> bool:
        return self._provider is not None or api_key_configured()

    def provider(self) -> Provider:
        if self._provider is None:
            if not api_key_configured():
                raise AINotConfigured(
                    "AI features need an Anthropic API key. Set ANTHROPIC_API_KEY in the environment "
                    "(see README). Upload, extraction and keyword search work without it."
                )
            self._provider = ClaudeProvider(self.settings)
        return self._provider

    def price(self, model: str | None = None) -> tuple[float, float]:
        model = model or self.model
        price = self.settings.price_for(model)
        if price is None:
            raise AINotConfigured(
                f"No price is configured for model '{model}', so spending cannot be controlled. "
                f"Set LITREVIEW_PRICE_{model.upper().replace('-', '_')}=input,output (USD per million tokens)."
            )
        return price

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        p_in, p_out = self.price(model)
        return (input_tokens * p_in + output_tokens * p_out) / 1_000_000

    # -- accounting ------------------------------------------------------------------------------

    def project_spent(self, project_id: str) -> float:
        row = self.ctx.db.query_one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM ai_calls WHERE project_id = ?", (project_id,)
        )
        return float(row["s"]) if row else 0.0

    def project_budget(self, project_id: str) -> float:
        row = self.ctx.db.query_one("SELECT ai_budget_usd FROM projects WHERE id = ?", (project_id,))
        return float(row["ai_budget_usd"]) if row else 0.0

    def usage_summary(self, project_id: str) -> dict:
        row = self.ctx.db.query_one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS inp,"
            " COALESCE(SUM(output_tokens),0) AS outp, COALESCE(SUM(cost_usd),0) AS cost,"
            " SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) AS failed"
            " FROM ai_calls WHERE project_id = ?",
            (project_id,),
        )
        return dict(row) if row else {}

    # -- calls -----------------------------------------------------------------------------------

    def call(self, project_id: str, req: AIRequest, run_id: str | None = None) -> AIResult:
        provider = self.provider()
        schema = strict_schema(req.output_model)
        max_tokens = req.max_tokens or self.settings.ai_max_output_tokens
        input_hash = hashlib.sha256(
            json.dumps(
                [self.model, req.prompt_version, req.system, req.user, schema, self.settings.ai_effort],
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        cached = self.ctx.db.query_one(
            "SELECT id, response, model FROM ai_calls WHERE project_id = ? AND input_hash = ? AND"
            " status = 'success' ORDER BY id DESC LIMIT 1",
            (project_id, input_hash),
        )
        if cached:
            try:
                data = req.output_model.model_validate_json(cached["response"])
                return AIResult(data, 0, 0, 0.0, True, cached["model"], cached["id"])
            except ValidationError:
                pass  # schema changed; call again

        input_est = (
            estimate_tokens(req.system) + estimate_tokens(req.user) + estimate_tokens(json.dumps(schema))
        )
        est_cost = self.estimate_cost(input_est, req.expected_output_tokens)
        budget = self.project_budget(project_id)
        spent = self.project_spent(project_id)
        if spent + est_cost > budget:
            raise BudgetExceeded(
                f"Project AI budget ${budget:.2f} would be exceeded (spent ${spent:.2f}, this call "
                f"estimated ${est_cost:.2f}). Raise the budget in Settings to continue."
            )
        if run_id:
            run = self.ctx.db.query_one(
                "SELECT approved_budget_usd, spent_usd, status FROM runs WHERE id = ?", (run_id,)
            )
            if run and run["status"] == "cancelled":
                raise AIError("Run was cancelled")
            if (
                run
                and run["approved_budget_usd"] is not None
                and run["spent_usd"] + est_cost > run["approved_budget_usd"]
            ):
                raise BudgetExceeded(
                    f"The approved budget for this run (${run['approved_budget_usd']:.2f}) would be "
                    f"exceeded (spent ${run['spent_usd']:.2f})."
                )

        call_id = self.ctx.db.execute(
            "INSERT INTO ai_calls(project_id, run_id, purpose, provider, model, prompt_version,"
            " input_hash, status, settings, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                project_id,
                run_id,
                req.purpose,
                provider.name,
                self.model,
                req.prompt_version,
                input_hash,
                json.dumps({"effort": self.settings.ai_effort, "max_tokens": max_tokens}),
                now(),
            ),
        )
        try:
            resp = provider.complete(
                model=self.model,
                system=req.system,
                user=req.user,
                schema=schema,
                max_tokens=max_tokens,
                effort=self.settings.ai_effort,
            )
        except AIError as exc:
            self._finish(call_id, "failed", error=exc.user_message)
            raise
        except Exception as exc:
            self._finish(call_id, "failed", error=type(exc).__name__)
            raise AIProviderError(f"Unexpected AI client error ({type(exc).__name__}).", True) from exc

        try:
            price_model = resp.model if self.settings.price_for(resp.model) else self.model
            cost = self.estimate_cost(resp.input_tokens, resp.output_tokens, price_model)
        except AINotConfigured:
            cost = self.estimate_cost(resp.input_tokens, resp.output_tokens)
        common = {
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cost": cost,
            "request_id": resp.request_id,
            "model": resp.model,
        }
        if run_id:
            self.ctx.db.execute("UPDATE runs SET spent_usd = spent_usd + ? WHERE id = ?", (cost, run_id))
        if resp.stop_reason == "refusal":
            self._finish(call_id, "refused", error="Model declined the request", **common)
            raise AIRefused("The model declined this request. Nothing was recorded as a result.")
        if resp.stop_reason == "max_tokens":
            self._finish(call_id, "truncated", error="Output hit max_tokens", **common)
            raise AIOutputInvalid(
                "The response was cut off at the output token limit, so it was discarded. "
                "Narrow the scope or raise LITREVIEW_MAX_OUTPUT_TOKENS."
            )
        try:
            data = req.output_model.model_validate_json(resp.text)
        except ValidationError as exc:
            self._finish(
                call_id,
                "invalid_output",
                error=f"Schema validation failed: {exc.error_count()} errors",
                **common,
            )
            raise AIOutputInvalid(
                "The AI response did not match the expected structure and was discarded."
            ) from exc
        self._finish(call_id, "success", response=data.model_dump_json(), **common)
        return AIResult(data, resp.input_tokens, resp.output_tokens, cost, False, resp.model, call_id)

    def _finish(
        self,
        call_id: int,
        status: str,
        *,
        error: str | None = None,
        response: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
        request_id: str | None = None,
        model: str | None = None,
    ) -> None:
        self.ctx.db.execute(
            "UPDATE ai_calls SET status = ?, error = ?, response = ?, input_tokens = ?, output_tokens = ?,"
            " cost_usd = ?, request_id = ?, model = COALESCE(?, model), finished_at = ? WHERE id = ?",
            (status, error, response, input_tokens, output_tokens, cost, request_id, model, now(), call_id),
        )
