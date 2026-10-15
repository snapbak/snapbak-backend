import pytest

from snapbak_backend.config.pricing import PRICES, cost
from snapbak_backend.config.settings import Config, load

ENV = {
    "SNAPBAK_TOKEN": "dev",
    "SNAPBAK_POLISH": "openai:gpt-4.1-nano",
    "SNAPBAK_BIG": "openai:gpt-4.1",
    "SNAPBAK_FALLBACK": "groq:llama-3.3-70b-versatile",
    "OPENAI_API_KEY": "sk-1",
    "GROQ_API_KEY": "gsk-1",
}


def test_load_reads_the_models_their_settings_and_keys() -> None:
    env = {
        **ENV,
        "SNAPBAK_CORRECTION": "openai:gpt-6-luna",
        "SNAPBAK_POLISH_TEMPERATURE": "0.2",
        "SNAPBAK_POLISH_EFFORT": "none",
        "SNAPBAK_EXAMPLES_POLICY": "per_module",
    }
    assert load(env) == Config(
        token="dev",
        polish="openai:gpt-4.1-nano",
        big="openai:gpt-4.1",
        correction="openai:gpt-6-luna",
        fallback="groq:llama-3.3-70b-versatile",
        temperature=0.2,
        effort="none",
        examples="per_module",
        keys={"groq": "gsk-1", "openai": "sk-1", "anthropic": "", "gemini": ""},
    )


def test_one_polish_model_with_the_r7_winners_settings_is_the_default() -> None:
    # The R7 round (15 Oct 2026) picked gpt-6-luna without reasoning and with every example to polish every chunk;
    # a refused answer is corrected on the big model.
    env = {name: value for name, value in ENV.items() if name != "SNAPBAK_POLISH"}
    config = load(env)
    assert (config.polish, config.correction, config.temperature, config.effort, config.examples) == (
        "openai:gpt-6-luna",
        "openai:gpt-4.1",
        0.0,
        "none",
        "all",
    )


def test_a_correction_of_none_turns_the_retry_off() -> None:
    assert load({**ENV, "SNAPBAK_CORRECTION": "none"}).correction is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SNAPBAK_POLISH_TEMPERATURE", "warm"),
        ("SNAPBAK_POLISH_TEMPERATURE", "2.5"),
        ("SNAPBAK_EXAMPLES_POLICY", "some"),
    ],
)
def test_a_setting_out_of_range_is_named(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        load({**ENV, name: value})


def test_the_fallback_is_optional() -> None:
    env = {name: value for name, value in ENV.items() if name != "SNAPBAK_FALLBACK"}
    assert load(env).fallback is None
    assert load({**ENV, "SNAPBAK_FALLBACK": ""}).fallback is None


@pytest.mark.parametrize("name", ["SNAPBAK_TOKEN", "SNAPBAK_BIG"])
def test_a_missing_required_setting_is_named(name: str) -> None:
    env = {key: value for key, value in ENV.items() if key != name}
    with pytest.raises(ValueError, match=name):
        load(env)


def test_cost_prices_input_and_output_tokens_per_million() -> None:
    assert PRICES["openai:gpt-4.1-nano"] == (0.10, 0.40)
    assert cost("openai:gpt-4.1-nano", 1_000_000, 1_000_000) == pytest.approx(0.50)


def test_cost_of_an_unpriced_engine_is_zero() -> None:
    assert cost("fake:echo", 5_000, 100) == 0


@pytest.mark.parametrize(
    ("engine", "price"),
    [
        ("openai:gpt-6-luna", (0.10, 0.50)),
        ("openai:gpt-5.4-nano", (0.20, 1.25)),
        ("openai:gpt-5-nano", (0.05, 0.40)),
        ("openai:gpt-6.1-sol", (2.00, 10.00)),
    ],
)
def test_the_latest_openai_models_carry_their_list_prices(engine: str, price: tuple[float, float]) -> None:
    assert PRICES[engine] == price


def test_cached_input_tokens_cost_the_cached_price() -> None:
    assert cost("openai:gpt-6-luna", 1_000_000, 0, 1_000_000) == pytest.approx(0.01)
    assert cost("openai:gpt-4.1-nano", 2_000_000, 1_000_000, 1_000_000) == pytest.approx(0.10 + 0.025 + 0.40)


def test_an_engine_without_a_cached_price_bills_cached_tokens_as_input() -> None:
    assert cost("groq:llama-3.1-8b-instant", 1_000_000, 0, 1_000_000) == pytest.approx(0.05)


def test_an_openai_call_is_billed_at_the_priority_estimate_and_others_at_list_price() -> None:
    from snapbak_backend.config.pricing import billed

    # OpenAI calls ask for priority processing (back on 15 Oct after R7); its price was not published where we could
    # read it, so the ledger counts it at twice the Standard price, an estimate on the safe side.
    assert billed("openai:gpt-6-luna", 1_000_000, 1_000_000) == pytest.approx(
        2 * cost("openai:gpt-6-luna", 1_000_000, 1_000_000)
    )
    assert billed("groq:llama-3.1-8b-instant", 1_000_000, 0) == pytest.approx(
        cost("groq:llama-3.1-8b-instant", 1_000_000, 0)
    )
