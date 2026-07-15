from __future__ import annotations
# 구조화된 prose provider 계약과 prompt·generation 설정을 검증한다.

import hashlib
import json

import pytest

from arc.contracts import (
    ContractError,
    PROSE_PROVIDER_CONTRACT_VERSION,
    materialize_prose_provider_response,
    validate_draft_prose,
    validate_prose,
)
from arc.live_model import GemmaPoolClient, LiveConfig, MODEL_NAME
from arc.prompts import build_prompt


def _raw(text: str, **extra: object) -> str:
    return json.dumps({"text": text, **extra}, ensure_ascii=False, separators=(",", ":"))


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (_raw("가"), None),
        (_raw("가", character_count=1), "PROSE_PROVIDER_FIELDS_MISMATCH"),
        ("{}", "PROSE_PROVIDER_FIELDS_MISMATCH"),
        ("null", "PROSE_PROVIDER_RESPONSE_NOT_OBJECT"),
        ("[]", "PROSE_PROVIDER_RESPONSE_NOT_OBJECT"),
        ("{bad", "PROSE_PROVIDER_RESPONSE_MALFORMED"),
        ("```json\n{\"text\":\"가\"}\n```", "PROSE_PROVIDER_RESPONSE_MALFORMED"),
        ("설명\n{\"text\":\"가\"}", "PROSE_PROVIDER_RESPONSE_MALFORMED"),
        ("{\"text\":\"가\"}\n설명", "PROSE_PROVIDER_RESPONSE_MALFORMED"),
        ("가", "PROSE_PROVIDER_RESPONSE_MALFORMED"),
        (_raw(""), "PROSE_PROVIDER_TEXT_INVALID"),
        (_raw(None), "PROSE_PROVIDER_TEXT_INVALID"),
        (_raw({"nested": "text"}), "PROSE_PROVIDER_TEXT_INVALID"),
        (_raw(["text"]), "PROSE_PROVIDER_TEXT_INVALID"),
    ],
)
def test_strict_prose_envelope_codes(raw: str, code: str | None) -> None:
    if code is None:
        assert materialize_prose_provider_response(raw, stage="writer") == "가"
    else:
        with pytest.raises(ContractError) as error:
            materialize_prose_provider_response(raw, stage="writer")
        assert error.value.contract_code == code


def test_materialization_preserves_text_and_separates_hashes() -> None:
    text = "  한국어 \"본문\" \\ 경로\n끝  "
    raw = _raw(text)
    materialized = materialize_prose_provider_response(raw, stage="revision")
    assert materialized == text
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() != hashlib.sha256(materialized.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("count", [4000, 8000])
def test_existing_prose_boundaries_are_unchanged(count: int) -> None:
    text = "가" * count
    assert validate_prose(text) == text
    assert validate_draft_prose(text)[1]["verdict"] == "PASS"


def test_writer_repairable_band_and_invalid_shape_remain_distinct() -> None:
    assert validate_draft_prose("가" * 3500)[1]["contract_code"] == "PROSE_UNDERLENGTH_REPAIRABLE"
    with pytest.raises(ContractError, match="invalid") as error:
        validate_prose("{" + "가" * 5000)
    assert error.value.contract_code == "PROSE_INVALID_SHAPE"


def test_run6_json_shaped_prose_is_legacy_shape_but_new_envelope_mismatch() -> None:
    raw = json.dumps({"episode": "episode_002", "prose": "가" * 5000}, ensure_ascii=False)
    with pytest.raises(ContractError) as legacy_error:
        validate_prose(raw)
    assert legacy_error.value.contract_code == "PROSE_INVALID_SHAPE"
    with pytest.raises(ContractError) as v1_error:
        materialize_prose_provider_response(raw, stage="writer")
    assert v1_error.value.contract_code == "PROSE_PROVIDER_FIELDS_MISMATCH"


def test_prose_prompts_put_final_contract_after_input_and_keep_guidance() -> None:
    writer = build_prompt("writer", "canonical", {"context": {}, "plan": {}})
    revision = build_prompt("revision", "canonical", {"context": {}, "plan": {}, "draft": "가" * 3500, "draft_contract": {"character_count": 3500}})
    for prompt, guidance in ((writer, "plan.immediate_objective"), (revision, "meaningful new prose development")):
        assert prompt.rfind("FINAL OUTPUT CONTRACT") > prompt.rfind("Input JSON:")
        assert 'exactly one key named "text"' in prompt
        assert "Return no text before or after the JSON object." in prompt
        assert guidance in prompt
        assert "prose_provider_contract_version" in prompt
    assert 'Do not add keys' in writer and 'Do not add keys' in revision
    assert writer.rfind("FINAL OUTPUT CONTRACT") > writer.rfind('"plan"')


def test_generation_config_uses_json_mime_schema_and_prose_limit() -> None:
    config = LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i}" for i in range(1, 12)}, launch_interval=1)
    client = GemmaPoolClient(config, client_factory=lambda key: object())
    prose = client._generation_config("writer")
    revision = client._generation_config("revision")
    planning = client._generation_config("planning")
    for value in (prose, revision):
        assert value["responseMimeType"] == "application/json"
        assert value["maxOutputTokens"] == 32768
        assert value["responseSchema"] == {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    assert planning["responseMimeType"] == "application/json"
    assert planning["maxOutputTokens"] == 8192
    assert PROSE_PROVIDER_CONTRACT_VERSION == 1
