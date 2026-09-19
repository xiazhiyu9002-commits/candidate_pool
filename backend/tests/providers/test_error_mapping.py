import httpx
import pytest
from pydantic import BaseModel

from kerui_recruit.providers.errors import FailureCategory, ProviderError, map_http_error
from kerui_recruit.providers.openai_compatible import OpenAICompatibleClient


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "E_API_FORMAT", False),
        (401, "E_API_AUTH", False),
        (402, "E_API_BALANCE", False),
        (422, "E_API_PARAMETERS", False),
        (429, "E_API_RATE_LIMIT", True),
        (500, "E_API_UPSTREAM", True),
        (503, "E_API_BUSY", True),
    ],
)
def test_http_status_mapping(status: int, code: str, retryable: bool) -> None:
    """Wrong retry classification can either lose work or cause an API retry storm."""
    error = map_http_error(status, request_id="request-1")

    assert (error.code, error.retryable, error.request_id) == (
        code,
        retryable,
        "request-1",
    )


@pytest.mark.parametrize(
    "body",
    [
        '{"error": {"message": "model deepseek-v4-flash not found"}}',
        '{"error": {"message": "model has been deprecated"}}',
        '{"error": {"message": "you have no permission to use this model"}}',
    ],
)
def test_model_related_upstream_error_is_switchable(body: str) -> None:
    error = map_http_error(404, error_body=body)
    assert error.category == FailureCategory.MODEL
    assert error.switchable is True


@pytest.mark.parametrize(
    "body",
    [
        '{"error": {"message": "request content violates content policy"}}',
        '{"error": {"message": "prompt too long, exceeds max context length"}}',
        '{"error": {"message": "unsupported image format"}}',
    ],
)
def test_policy_and_input_upstream_errors_do_not_switch(body: str) -> None:
    error = map_http_error(400, error_body=body)
    assert error.switchable is False
    assert error.category in (FailureCategory.POLICY, FailureCategory.INPUT)


class PersonResult(BaseModel):
    name: str


@pytest.mark.asyncio
async def test_openai_compatible_client_validates_structured_json() -> None:
    """Malformed model output must not enter the fact database as parsed data."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-key"
        assert b'"model":"model-one"' in request.content
        return httpx.Response(
            200,
            headers={"x-request-id": "upstream-1"},
            json={
                "choices": [
                    {"message": {"content": '{"name":"张三"}'}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleClient(
            base_url="https://provider.example/v1",
            api_key="secret-key",
            model="model-one",
            http_client=http_client,
        )
        result = await client.complete_json(
            [{"role": "user", "content": "parse"}],
            PersonResult,
        )

    assert result == PersonResult(name="张三")


@pytest.mark.asyncio
async def test_openai_compatible_error_never_exposes_api_key() -> None:
    """An upstream authentication failure must not leak the configured secret."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text="denied"))
    ) as http_client:
        client = OpenAICompatibleClient(
            base_url="https://provider.example/v1",
            api_key="secret-key",
            model="model-one",
            http_client=http_client,
        )
        with pytest.raises(ProviderError) as error:
            await client.complete_json(
                [{"role": "user", "content": "parse"}],
                PersonResult,
            )

    assert error.value.code == "E_API_AUTH"
    assert "secret-key" not in str(error.value)
