import os
import time
import json
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain.schema.output_parser import StrOutputParser

load_dotenv()

RETRY_WAIT_SECONDS = int(os.getenv("RATE_LIMIT_WAIT_SECONDS", "60"))

# Rate limit error substrings — covers Gemini and OpenAI error messages
RATE_LIMIT_SIGNALS = [
    "429",
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "resourceexhausted",
    "too many requests",
]


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return any(signal in msg for signal in RATE_LIMIT_SIGNALS)


def _build_clients() -> list:
    """
    Build the ordered list of (label, llm) pairs from environment variables.
    Only includes clients where the key is actually set.
    """
    clients = []

    # key1 = os.getenv("GEMINI_API_KEY_1")
    # key2 = os.getenv("GEMINI_API_KEY_2")
    openai_key = os.getenv("OPENAI_API_KEY")

    # if key1:
    #     clients.append((
    #         "Gemini key 1",
    #         ChatGoogleGenerativeAI(
    #             model="gemini-2.5-flash",
    #             google_api_key=key1,
    #             temperature=0.1,
    #         )
    #     ))

    # if key2:
    #     clients.append((
    #         "Gemini key 2",
    #         ChatGoogleGenerativeAI(
    #             model="gemini-2.5-flash",
    #             google_api_key=key2,
    #             temperature=0.1,
    #         )
    #     ))

    if openai_key:
        clients.append((
            "GPT-4.1",
            ChatOpenAI(
                model="gpt-4.1",
                api_key=openai_key,
                temperature=0.1,
            )
        ))

    if not clients:
        raise RuntimeError(
            "No LLM keys configured. Set at least one of: "
            "GEMINI_API_KEY_1, GEMINI_API_KEY_2, OPENAI_API_KEY"
        )

    return clients


# Build once on import
_CLIENTS = _build_clients()
_active_index = 0


def _get_active_label() -> str:
    return _CLIENTS[_active_index][0]


def invoke_with_rotation(prompt, input_vars: dict, temperature: float = 0.1) -> str:
    """
    Invoke the prompt with the active LLM client.
    On rate limit, rotates to the next client automatically.
    If all clients are exhausted, waits RETRY_WAIT_SECONDS and resets.

    Args:
        prompt:      A LangChain ChatPromptTemplate
        input_vars:  Dict of variables to pass to the prompt
        temperature: Ignored here — set per-client in _build_clients

    Returns:
        The raw string response from the model
    """
    global _active_index

    attempts = 0
    max_attempts = len(_CLIENTS) + 1  # +1 to allow one full retry cycle after wait

    while attempts < max_attempts:
        label, llm = _CLIENTS[_active_index]
        chain = prompt | llm | StrOutputParser()

        try:
            result = chain.invoke(input_vars)
            return result

        except Exception as e:
            if _is_rate_limit(e):
                next_index = (_active_index + 1) % len(_CLIENTS)

                if next_index == 0:
                    # Completed a full rotation — all keys exhausted
                    print(
                        f"  [Router] All {len(_CLIENTS)} keys are rate limited. "
                        f"Waiting {RETRY_WAIT_SECONDS}s before retrying..."
                    )
                    time.sleep(RETRY_WAIT_SECONDS)
                    _active_index = 0
                    attempts += 1
                    continue
                else:
                    print(
                        f"  [Router] {label} hit rate limit. "
                        f"Rotating to {_CLIENTS[next_index][0]}..."
                    )
                    _active_index = next_index
                    attempts += 1
                    continue
            else:
                # Not a rate limit error — raise immediately, do not rotate
                raise

    raise RuntimeError(
        f"All LLM clients failed after {max_attempts} attempts. "
        "Check your API keys and quota."
    )


def get_current_provider() -> str:
    """Returns the label of the currently active LLM client."""
    return _CLIENTS[_active_index][0]