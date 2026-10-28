"""Claude API 래퍼. ANTHROPIC_API_KEY가 없으면 available=False이고, 각 기능은 규칙 기반으로 동작합니다."""
from __future__ import annotations

import json
import logging

log = logging.getLogger("labbot.llm")

# 요약·추출은 깊게 생각할 필요가 없는 작업이라 effort를 낮춰 비용과 지연을 줄임.
# 최신 모델은 thinking이 항상 켜져 있고 thinking 토큰도 max_tokens에 포함되므로 max_tokens는 넉넉하게.
EFFORT = "low"

# structured outputs가 받지 않는 제약 (값 범위·길이). 범위 검사는 호출하는 쪽에서 합니다.
_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                "minLength", "maxLength", "minItems", "maxItems"}


class LLMError(RuntimeError):
    pass


def strict_schema(schema):
    """structured outputs 규칙에 맞게 고칩니다: 모든 object에 additionalProperties=false, 미지원 제약 제거."""
    if isinstance(schema, list):
        return [strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED}
    if out.get("type") == "object":
        out["additionalProperties"] = False
    return out


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

    def _create(self, **kw):
        if not self.client:
            raise LLMError("ANTHROPIC_API_KEY가 설정되지 않았어요.")
        try:
            r = self.client.messages.create(model=self.model, **kw)
        except Exception as e:  # noqa: BLE001
            log.exception("Claude API 호출 실패")
            raise LLMError(str(e)) from e
        if r.stop_reason == "refusal":
            raise LLMError("모델이 요청을 거절했어요.")
        if r.stop_reason == "max_tokens":
            raise LLMError("응답이 max_tokens에서 잘렸어요.")
        return r

    def structured(self, system: str, prompt: str, name: str, description: str, schema: dict,
                   max_tokens: int = 16000) -> dict:
        """structured outputs(output_config.format)로 schema에 맞는 JSON(dict)을 받습니다.
        name/description은 출력이 무엇인지 모델에게 알려 주는 데만 씁니다."""
        r = self._create(
            max_tokens=max_tokens,
            system=f"{system}\n\n출력: {description} ({name})",
            output_config={"effort": EFFORT,
                           "format": {"type": "json_schema", "schema": strict_schema(schema)}},
            messages=[{"role": "user", "content": prompt}])
        text = next((b.text for b in r.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("모델이 구조화된 결과를 돌려주지 않았어요.") from e

    def text(self, system: str, prompt: str, max_tokens: int = 8000) -> str:
        r = self._create(max_tokens=max_tokens, system=system, output_config={"effort": EFFORT},
                         messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if b.type == "text").strip()
