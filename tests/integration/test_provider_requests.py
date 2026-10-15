import json
import time

import httpx
import pytest
import respx

from snapbak_backend.ai.providers.base import ProviderError, Usage
from snapbak_backend.ai.providers.factory import make_provider

KEYS = {"groq": "gsk-1", "openai": "sk-1", "anthropic": "sk-ant-1", "gemini": "gm-1"}
TURNS = [
    ("system", "the rules"),
    ("user", "said"),
    ("assistant", "cleaned"),
    ("user", '{"text": "kal milte hain"}'),
]
REPLY = '{"revision": null, "text": "Kal milte hain."}'


def sent(route: respx.Route) -> dict:
    request = route.calls.last.request
    return json.loads(request.content)


# OpenAI is asked for priority processing and a cache key from the fixed prompt's opening, so the long prompt is
# served from its cache and the answer comes back sooner: the R7 round (15 Oct) measured the default tier 25 % slower
# at p50 for the same model. Groq gets neither.
OPENAI_SPEED = {"service_tier": "priority", "prompt_cache_key": "snapbak-c53b0f3aaea46bbe"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("spec", "url", "key", "speed"),
    [
        ("groq:llama-3.3-70b-versatile", "https://api.groq.com/openai/v1/chat/completions", "gsk-1", {}),
        ("openai:gpt-4.1-nano", "https://api.openai.com/v1/chat/completions", "sk-1", OPENAI_SPEED),
    ],
)
async def test_openai_style_providers_post_the_turns_as_a_chat(spec: str, url: str, key: str, speed: dict) -> None:
    provider = make_provider(spec, KEYS)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).respond(
            json={
                "choices": [{"message": {"content": REPLY}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 9},
            }
        )
        output = await provider.complete(TURNS, 64, 2.0)
    assert output == (REPLY, Usage(120, 9))
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {key}"
    assert request.headers["content-type"] == "application/json"
    assert sent(route) == {
        "model": spec.split(":", 1)[1],
        "temperature": 0,
        "seed": 7,
        "max_completion_tokens": 64,
        "response_format": {"type": "json_object"},
        "messages": [{"role": role, "content": content} for role, content in TURNS],
        **speed,
    }
    await provider.aclose()


@pytest.mark.anyio
async def test_the_openai_cache_key_follows_the_fixed_prompt_not_the_dictation() -> None:
    provider = make_provider("openai:gpt-6-luna", KEYS)
    reply = {"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 120, "completion_tokens": 9}}
    with respx.mock() as mock:
        route = mock.post("https://api.openai.com/v1/chat/completions").respond(json=reply)
        await provider.complete(TURNS, 64, 2.0)
        first = sent(route)["prompt_cache_key"]
        await provider.complete([*TURNS[:-1], ("user", '{"text": "ship it"}')], 64, 2.0)
        same = sent(route)["prompt_cache_key"]
        await provider.complete([("system", "other rules"), *TURNS[1:]], 64, 2.0)
        other = sent(route)["prompt_cache_key"]
    assert first == same != other
    await provider.aclose()


@pytest.mark.anyio
async def test_every_module_set_of_the_polish_prompt_shares_one_cache_key() -> None:
    from snapbak_backend.ai.prompts.command import COMMAND_SYSTEM as COMMAND
    from snapbak_backend.ai.prompts.polish import MODULES, system

    provider = make_provider("openai:gpt-6-luna", KEYS)
    reply = {"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 120, "completion_tokens": 9}}
    keys = []
    with respx.mock() as mock:
        route = mock.post("https://api.openai.com/v1/chat/completions").respond(json=reply)
        for rules in (system(["grammar"]), system(["grammar", "lists"]), system(MODULES), COMMAND):
            await provider.complete([("system", rules), *TURNS[1:]], 64, 2.0)
            keys.append(sent(route)["prompt_cache_key"])
    assert keys[0] == keys[1] == keys[2] != keys[3]
    await provider.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "sampling"),
    [
        ("gpt-4.1-mini", {"temperature": 0, "seed": 7}),
        ("gpt-5.4-nano", {"temperature": 0, "seed": 7, "reasoning_effort": "none"}),
        ("gpt-6-luna", {"reasoning_effort": "none"}),
        ("gpt-5-nano", {"reasoning_effort": "minimal"}),
        ("gpt-6.1-sol", {"reasoning_effort": "low"}),
    ],
)
async def test_openai_models_get_their_lowest_reasoning_effort_and_temperature_0_only_where_taken(
    model: str, sampling: dict
) -> None:
    provider = make_provider(f"openai:{model}", KEYS)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.openai.com/v1/chat/completions").respond(
            json={"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )
        await provider.complete(TURNS, 64, 2.0)
    body = sent(route)
    assert {name: body[name] for name in ("temperature", "seed", "reasoning_effort") if name in body} == sampling
    await provider.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "sampling"),
    [
        ("groq:llama-3.3-70b-versatile", {"temperature": 0.4, "seed": 7}),
        ("openai:gpt-4.1-nano", {"temperature": 0.4, "seed": 7}),
        ("openai:gpt-5.4-nano", {"temperature": 0.4, "seed": 7, "reasoning_effort": "none"}),
        # A model that refuses temperature gets none, whatever the setting.
        ("openai:gpt-6-luna", {"reasoning_effort": "none"}),
    ],
)
async def test_the_polish_temperature_and_a_fixed_seed_go_to_models_that_take_a_temperature(
    model: str, sampling: dict
) -> None:
    # 15 Oct: the bench sweeps temperature 0, 0.2 and 0.4 on the models that take one (SNAPBAK_POLISH_TEMPERATURE).
    provider = make_provider(model, KEYS, temperature=0.4)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url=httpx.URL(sent_to(model))).respond(
            json={"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )
        await provider.complete(TURNS, 64, 2.0)
    body = sent(route)
    assert {name: body[name] for name in ("temperature", "seed", "reasoning_effort") if name in body} == sampling
    await provider.aclose()


def sent_to(spec: str) -> str:
    base = "https://api.groq.com/openai/v1" if spec.startswith("groq:") else "https://api.openai.com/v1"
    return f"{base}/chat/completions"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "sampling"),
    [
        ("gpt-4.1-mini", {"temperature": 0}),
        ("gpt-5.4-nano", {"reasoning_effort": "medium"}),
        ("gpt-6-luna", {"reasoning_effort": "medium"}),
        ("gpt-5-nano", {"reasoning_effort": "medium"}),
        ("gpt-6.1-sol", {"reasoning_effort": "medium"}),
    ],
)
async def test_a_call_that_reasons_asks_reasoning_models_for_medium_effort(model: str, sampling: dict) -> None:
    # Round b3 (D8): medium effort passed 150 of 216 non-English bench rows on the big tier against 131 at low.
    provider = make_provider(f"openai:{model}", KEYS)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.openai.com/v1/chat/completions").respond(
            json={"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )
        await provider.complete(TURNS, 64, 2.0, reason="medium")
    body = sent(route)
    assert {name: body[name] for name in ("temperature", "reasoning_effort") if name in body} == sampling
    await provider.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("effort", ["minimal", "low", "medium"])
async def test_a_call_that_reasons_asks_for_the_effort_its_route_was_given(effort: str) -> None:
    provider = make_provider("openai:gpt-6-luna", KEYS)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.openai.com/v1/chat/completions").respond(
            json={"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        )
        await provider.complete(TURNS, 64, 2.0, reason=effort)
    assert sent(route)["reasoning_effort"] == effort
    await provider.aclose()


@pytest.mark.anyio
async def test_openai_reports_the_prompt_tokens_it_served_from_its_cache() -> None:
    provider = make_provider("openai:gpt-6-luna", KEYS)
    usage = {"prompt_tokens": 1600, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 1536}}
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.openai.com/v1/chat/completions").respond(
            json={"choices": [{"message": {"content": REPLY}}], "usage": usage}
        )
        output = await provider.complete(TURNS, 64, 2.0)
    assert output == (REPLY, Usage(1600, 30, 1536))
    await provider.aclose()


@pytest.mark.anyio
async def test_openai_reports_the_reasoning_tokens_inside_its_output() -> None:
    provider = make_provider("openai:gpt-6-luna", KEYS)
    usage = {"prompt_tokens": 1600, "completion_tokens": 130, "completion_tokens_details": {"reasoning_tokens": 100}}
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.openai.com/v1/chat/completions").respond(
            json={"choices": [{"message": {"content": REPLY}}], "usage": usage}
        )
        _, counted = await provider.complete(TURNS, 64, 2.0, reason="medium")
    assert counted.reasoning == 100
    await provider.aclose()


@pytest.mark.anyio
async def test_anthropic_puts_the_rules_in_system_and_sends_its_headers() -> None:
    provider = make_provider("anthropic:claude-haiku-4-5", KEYS)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://api.anthropic.com/v1/messages").respond(
            json={
                "content": [{"type": "text", "text": REPLY}],
                "usage": {"input_tokens": 130, "output_tokens": 11},
            }
        )
        output = await provider.complete(TURNS, 64, 2.0)
    assert output == (REPLY, Usage(130, 11))
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "sk-ant-1"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    cached = {"type": "ephemeral"}
    assert sent(route) == {
        "model": "claude-haiku-4-5",
        "max_tokens": 64,
        "temperature": 0,
        "system": [{"type": "text", "text": "the rules", "cache_control": cached}],
        "messages": [
            {"role": "user", "content": "said"},
            {"role": "assistant", "content": [{"type": "text", "text": "cleaned", "cache_control": cached}]},
            {"role": "user", "content": '{"text": "kal milte hain"}'},
        ],
    }
    await provider.aclose()


# The rules and the worked examples are the same in every request: they are cached up to the last example, and the
# tokens read from the cache are counted as cached input.
@pytest.mark.anyio
async def test_anthropic_counts_the_tokens_served_from_its_cache() -> None:
    provider = make_provider("anthropic:claude-haiku-4-5", KEYS)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://api.anthropic.com/v1/messages").respond(
            json={
                "content": [{"type": "text", "text": REPLY}],
                "usage": {
                    "input_tokens": 30,
                    "cache_read_input_tokens": 5000,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 11,
                },
            }
        )
        output = await provider.complete(TURNS, 64, 2.0)
    assert output == (REPLY, Usage(5130, 11, 5000))
    await provider.aclose()


@pytest.mark.anyio
async def test_gemini_sends_contents_with_model_turns_and_json_output() -> None:
    provider = make_provider("gemini:gemini-2.5-flash-lite", KEYS)
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(url).respond(
            json={
                "candidates": [{"content": {"parts": [{"text": REPLY}]}}],
                "usageMetadata": {"promptTokenCount": 140, "candidatesTokenCount": 12},
            }
        )
        output = await provider.complete(TURNS, 64, 2.0)
    assert output == (REPLY, Usage(140, 12))
    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "gm-1"
    assert sent(route) == {
        "systemInstruction": {"parts": [{"text": "the rules"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "said"}]},
            {"role": "model", "parts": [{"text": "cleaned"}]},
            {"role": "user", "parts": [{"text": '{"text": "kal milte hain"}'}]},
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 64,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    await provider.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(429, json={"error": "slow down"}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_a_failed_or_malformed_answer_is_a_provider_error(response: httpx.Response) -> None:
    provider = make_provider("openai:gpt-4.1-nano", KEYS)
    with respx.mock() as mock:
        mock.post("https://api.openai.com/v1/chat/completions").mock(return_value=response)
        with pytest.raises(ProviderError):
            await provider.complete(TURNS, 64, 2.0)
    await provider.aclose()


@pytest.mark.anyio
async def test_a_connection_error_is_a_provider_error() -> None:
    provider = make_provider("groq:llama-3.3-70b-versatile", KEYS)
    with respx.mock() as mock:
        mock.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(ProviderError):
            await provider.complete(TURNS, 64, 2.0)
    await provider.aclose()


OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANSWERED = httpx.Response(
    200, json={"choices": [{"message": {"content": REPLY}}], "usage": {"prompt_tokens": 120, "completion_tokens": 9}}
)


@pytest.mark.anyio
async def test_a_rate_limited_answer_is_asked_again_after_its_retry_after() -> None:
    # Founder dictations failed with "HTTP/2 429 Too Many Requests" on both models (D6, 11 Oct).
    provider = make_provider("openai:gpt-4.1-nano", KEYS)
    limited = httpx.Response(429, headers={"retry-after": "0.2"}, json={"error": {"code": "rate_limit_exceeded"}})
    with respx.mock() as mock:
        route = mock.post(OPENAI_URL).mock(side_effect=[limited, ANSWERED])
        started = time.monotonic()
        output = await provider.complete(TURNS, 64, 3.0)
    assert output == (REPLY, Usage(120, 9, 0, 1))
    assert route.call_count == 2
    assert time.monotonic() - started >= 0.2
    await provider.aclose()


@pytest.mark.anyio
async def test_a_server_error_is_asked_again_with_backoff() -> None:
    provider = make_provider("openai:gpt-4.1-nano", KEYS)
    busy = httpx.Response(503, json={"error": "overloaded"})
    with respx.mock() as mock:
        route = mock.post(OPENAI_URL).mock(side_effect=[busy, httpx.Response(500), ANSWERED])
        output = await provider.complete(TURNS, 64, 3.0)
    assert output == (REPLY, Usage(120, 9, 0, 2))
    assert route.call_count == 3
    await provider.aclose()


@pytest.mark.anyio
async def test_an_account_out_of_credits_is_not_asked_again() -> None:
    provider = make_provider("openai:gpt-4.1-nano", KEYS)
    broke = httpx.Response(429, json={"error": {"type": "insufficient_quota", "code": "credit_balance_exhausted"}})
    with respx.mock() as mock:
        route = mock.post(OPENAI_URL).mock(return_value=broke)
        with pytest.raises(ProviderError):
            await provider.complete(TURNS, 64, 3.0)
    assert route.call_count == 1
    await provider.aclose()


@pytest.mark.anyio
async def test_retries_stop_before_the_deadline_and_are_counted() -> None:
    provider = make_provider("openai:gpt-4.1-nano", KEYS)
    with respx.mock() as mock:
        route = mock.post(OPENAI_URL).mock(return_value=httpx.Response(503))
        started = time.monotonic()
        with pytest.raises(ProviderError) as failed:
            await provider.complete(TURNS, 64, 0.8)
    assert time.monotonic() - started < 0.8
    assert failed.value.retries == route.call_count - 1 >= 1
    await provider.aclose()


@pytest.mark.anyio
async def test_a_retry_after_past_the_deadline_is_not_waited_for() -> None:
    provider = make_provider("openai:gpt-4.1-nano", KEYS)
    limited = httpx.Response(429, headers={"retry-after": "30"})
    with respx.mock() as mock:
        route = mock.post(OPENAI_URL).mock(return_value=limited)
        started = time.monotonic()
        with pytest.raises(ProviderError):
            await provider.complete(TURNS, 64, 2.0)
    assert time.monotonic() - started < 0.5
    assert route.call_count == 1
    await provider.aclose()
