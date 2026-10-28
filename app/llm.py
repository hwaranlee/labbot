"""Claude API 래퍼. ANTHROPIC_API_KEY가 없으면 available=False이고, 각 기능은 규칙 기반으로 동작합니다."""
from __future__ import annotations

import logging

log = logging.getLogger("labbot.llm")


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, api_key: str = "", model: str = "claude-sonnet-5-5"):
        self.model = model
        self.client = None
        if api_key:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=120)

    @property
    def available(self) -> bool:
        return self.client is not None

    def structured(self, system: str, prompt: str, name: str, description: str, schema: dict,
                   max_tokens: int = 4000) -> dict:
        """도구 호출을 강제해 schema에 맞는 JSON(dict)을 받습니다."""
        if not self.client:
            raise LLMError("ANTHROPIC_API_KEY가 설정되지 않았어요.")
        try:
            r = self.client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                tools=[{"name": name, "description": description, "input_schema": schema}],
                tool_choice={"type": "tool", "name": name},
                messages=[{"role": "user", "content": prompt}])
        except Exception as e:  # noqa: BLE001
            log.exception("Claude API 호출 실패")
            raise LLMError(str(e)) from e
        for block in r.content:
            if block.type == "tool_use":
                return block.input
        raise LLMError("모델이 구조화된 결과를 돌려주지 않았어요.")

    def text(self, system: str, prompt: str, max_tokens: int = 1500) -> str:
        if not self.client:
            raise LLMError("ANTHROPIC_API_KEY가 설정되지 않았어요.")
        try:
            r = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                            messages=[{"role": "user", "content": prompt}])
        except Exception as e:  # noqa: BLE001
            log.exception("Claude API 호출 실패")
            raise LLMError(str(e)) from e
        return "".join(b.text for b in r.content if b.type == "text").strip()
