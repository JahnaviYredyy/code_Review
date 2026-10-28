import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from dotenv import load_dotenv
from groq import Groq

load_dotenv()


@dataclass
class CallRecord:
    provider: str
    model: str
    duration_seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GroqReviewClient:
    """Shared Groq client that records metadata for model calls."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ):
        load_dotenv()

        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.model = model or os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-120b",
        )

        if (
            not self.api_key
            or self.api_key == "your_groq_api_key_here"
        ):
            raise RuntimeError(
                "GROQ_API_KEY is missing. "
                "Add a valid key to your .env file."
            )

        self.client = Groq(api_key=self.api_key)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
        temperature: float = 0,
    ) -> tuple[str, dict[str, Any]]:
        """Return response text and metadata, recording failed calls too."""

        options: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }

        if max_tokens is not None:
            options["max_tokens"] = max_tokens 

        started = time.perf_counter()

        try:
            response = self.client.chat.completions.create(
                **options
            )
        except Exception as exc:
            duration = round(time.perf_counter() - started, 3)

            self.calls.append({
                "provider": "Groq",
                "model": self.model,
                "duration_seconds": duration,
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise

        duration = round(time.perf_counter() - started, 3)
        usage = getattr(response, "usage", None)

        record = CallRecord(
            provider="Groq",
            model=self.model,
            duration_seconds=duration,
            prompt_tokens=(
                getattr(usage, "prompt_tokens", None)
                if usage else None
            ),
            completion_tokens=(
                getattr(usage, "completion_tokens", None)
                if usage else None
            ),
            total_tokens=(
                getattr(usage, "total_tokens", None)
                if usage else None
            ),
        ).to_dict()

        choices = getattr(response, "choices", None)

        if not choices:
            record["status"] = "failed"
            record["error"] = "Groq returned no response choices."
            self.calls.append(record)

            raise RuntimeError(record["error"])

        message = getattr(choices[0], "message", None)
        raw_content = getattr(message, "content", None)
        content = raw_content.strip() if isinstance(
            raw_content, str
        ) else ""

        if not content:
            finish_reason = getattr(
                choices[0], "finish_reason", None
            )

            record["status"] = "failed"
            record["error"] = "Groq returned an empty response."
            record["finish_reason"] = finish_reason
            self.calls.append(record)

            raise RuntimeError(
                "Groq returned an empty response. "
                f"Model: {self.model}; "
                f"finish_reason: {finish_reason!r}; "
                f"usage: {record.get('total_tokens')!r} tokens."
            )

        record["status"] = "success"
        self.calls.append(record)

        return content, record
