"""A heavy user's month on the one polish model, priced and reported (docs/replay.md).

`uv run scripts/replay.py [POLISH CORRECTION]` (default openai:gpt-6-luna openai:gpt-6-luna). Two hours of dictation
are drawn from the corpus utterances (fixed seed), each cut into chunks the way the app's ChunkGate cuts speech,
and each chunk is sent with the words before it as `prior`, like the app. Tokens are estimated from UTF-8 bytes
(4 per token). Runs offline: no keys needed.
"""

import json
import random
import sys

from bench import load
from snapbak_backend.ai.guardrail.judge import Reason, Reject
from snapbak_backend.ai.prompts.examples import EXAMPLES
from snapbak_backend.ai.prompts.polish import SYSTEM, correction, message
from snapbak_backend.config.pricing import billed
from snapbak_backend.config.settings import POLISH
from snapbak_backend.schemas.polish import MAX_PRIOR_WORDS

# The PRD's cost target, shown for reference: the founder put accuracy over cost and time (11 Oct).
TARGET = 0.50
SECONDS = 2 * 3600
WORDS_PER_SECOND = 2.5
CHUNK_WORDS = 15
MESSAGE_OVERHEAD = 4
# Answers the guardrail refuses, which the correction model corrects once with reasoning (D1): it reads the request
# again, the refused answer and the reasons. Replace with the bench's measured share.
RETRY_SHARE = 0.05
# Share of the fixed prompt served from the provider's prompt cache, as the founder set it (10 Oct): the bench
# measured 25 % on a cold start at 16 requests in flight and 99.8 % warm.
CACHE_HIT = 0.5
# Reasoning tokens a correction adds to its answer, the larger of two bench means on `gpt-6-luna`: 46 more output
# tokens on correction chunks at low effort than at none, and 20.5 reasoning tokens a big-tier call
# (docs/escalation.md).
REASONED_TOKENS = 46


def estimate(text: str) -> int:
    return len(text.encode()) // 4 + 1


STATIC = estimate(SYSTEM + "".join(said + cleaned for said, cleaned in EXAMPLES)) + MESSAGE_OVERHEAD * (
    2 + 2 * len(EXAMPLES)
)


# The reasons turn, as long as a typical one: an unheard word quoted.
CORRECTION_TOKENS = estimate(correction(Reason(Reject.UNHEARD, ("okay",)))) + 2 * MESSAGE_OVERHEAD


def chunks(said: str) -> list[tuple[str, str]]:
    """(prior, text) per chunk of one hold: about 6 s of speech each, prior capped at 300 words."""
    words = said.split()
    cut = []
    for start in range(0, len(words), CHUNK_WORDS):
        prior = words[max(0, start - MAX_PRIOR_WORDS) : start]
        cut.append((" ".join(prior), " ".join(words[start : start + CHUNK_WORDS])))
    return cut


def utterances() -> list[str]:
    """What each corpus row says, read the way the bench reads it, without the generated suite-growth rows."""
    return [item.text for item in load() if not item.prior and item.slice != "suite-growth"]


def month(pool: list[str], polish: str, correction: str) -> dict[str, float]:
    rng = random.Random(7)
    totals = {"requests": 0, "input": 0, "output": 0, "polish $": 0.0, "correction $": 0.0}
    cached_tokens = round(CACHE_HIT * STATIC)
    words = 0
    while words < SECONDS * WORDS_PER_SECOND:
        said = rng.choice(pool)
        words += len(said.split())
        for prior, text in chunks(said):
            request = {"text": text, "prior": prior, "terms": [], "app": "", "style": "chat"}
            input_tokens = STATIC + estimate(message(request))
            answer = estimate(json.dumps({"revision": None, "text": text}, ensure_ascii=False))
            totals["requests"] += 1
            totals["input"] += input_tokens
            totals["output"] += answer
            corrected = input_tokens + answer + CORRECTION_TOKENS
            retried = billed(correction, corrected, answer + REASONED_TOKENS, cached_tokens)
            totals["correction $"] += RETRY_SHARE * retried
            totals["polish $"] += billed(polish, input_tokens, answer, cached_tokens)
    return totals


def main() -> int:
    polish, correction = sys.argv[1:3] if len(sys.argv) >= 3 else (POLISH, "openai:gpt-6-luna")
    pool = utterances()
    print(
        f"A heavy month: {SECONDS // 3600} h of dictation, polish {polish}, correction {correction}, "
        f"retry share {RETRY_SHARE:.0%}, prompt cache {CACHE_HIT:.0%}"
    )
    totals = month(pool, polish, correction)
    dollars = totals["polish $"] + totals["correction $"]
    passes = dollars <= TARGET
    print(
        f"  corpus: {totals['requests']:.0f} requests, "
        f"{totals['input'] / 1e6:.2f} M in + {totals['output'] / 1e6:.3f} M out tokens, "
        f"${totals['polish $']:.3f} polish + ${totals['correction $']:.3f} correction = ${dollars:.3f} "
        f"{'within' if passes else 'OVER'} ${TARGET:.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
