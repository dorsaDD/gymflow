"""Offline checks for local and optional OpenAI adapters (no live requests).

Run from the project root with: python3 _devtools/test_ai_provider.py
"""
import os
import sys
import types
import importlib.util
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)

# This adapter-only test intentionally loads no Flask app or database. Minimal
# placeholders satisfy ai_agent.py's import-time domain references; the
# provider adapter itself is the code under test.
package = types.ModuleType("gymflow")
package.__path__ = [os.path.join(APP_ROOT, "gymflow")]
sys.modules["gymflow"] = package
models = types.ModuleType("gymflow.models")
models.Booking = type("Booking", (), {})
models.FitnessClass = type("FitnessClass", (), {})
sys.modules["gymflow.models"] = models
package.models = models
for name in ("nutrition_manager", "payment_manager"):
    module = types.ModuleType(f"gymflow.{name}")
    if name == "nutrition_manager":
        module.daily_summary = lambda card, day: {
            "goal": None, "entries": [], "consumed": {"calories": 0},
            "percent_of_goal": None,
        }
    else:
        module.outstanding_balance = lambda card: 0
    sys.modules[f"gymflow.{name}"] = module
    setattr(package, name, module)
loyalty_engine = types.ModuleType("gymflow.loyalty_engine")
loyalty_engine.get_account = lambda *args, **kwargs: None
loyalty_engine.current_tier = lambda account: None
sys.modules["gymflow.loyalty_engine"] = loyalty_engine
package.loyalty_engine = loyalty_engine
spec = importlib.util.spec_from_file_location("gymflow.resilience", os.path.join(APP_ROOT, "gymflow", "resilience.py"))
resilience = importlib.util.module_from_spec(spec)
sys.modules["gymflow.resilience"] = resilience
spec.loader.exec_module(resilience)
package.resilience = resilience
spec = importlib.util.spec_from_file_location("gymflow.ai_agent", os.path.join(APP_ROOT, "gymflow", "ai_agent.py"))
ai_agent = importlib.util.module_from_spec(spec)
sys.modules["gymflow.ai_agent"] = ai_agent
spec.loader.exec_module(ai_agent)


def run():
    checks = []
    original_key = os.environ.pop("OPENAI_API_KEY", None)
    original_model = os.environ.pop("GYMFLOW_LLM_MODEL", None)
    original_provider = os.environ.pop("GYMFLOW_LLM_PROVIDER", None)
    original_base_url = os.environ.pop("GYMFLOW_LLM_BASE_URL", None)
    original_ollama_model = os.environ.pop("GYMFLOW_OLLAMA_MODEL", None)
    original_openai = sys.modules.get("openai")
    messages = [{"role": "user", "content": "hello"}]
    try:
        # Ollama is the default and has no cloud/API key dependency.
        os.environ["GYMFLOW_LLM_PROVIDER"] = "ollama"
        sys.modules["openai"] = None
        reply = ai_agent._call_llm(messages)
        checks.append(("missing SDK explains requirements install", "pip install -r requirements.txt" in reply))

        captured = {}

        class FakeOpenAI:
            def __init__(self, *, api_key, base_url=None, timeout=None):
                captured["api_key"] = api_key
                captured["base_url"] = base_url
                captured["timeout"] = timeout
                self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                message = types.SimpleNamespace(content="A real-looking test reply")
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        sys.modules["openai"] = types.SimpleNamespace(OpenAI=FakeOpenAI)
        resilience.reset_all_breakers()
        reply = ai_agent._call_llm(messages)
        checks.append(("default local adapter returns model reply", reply == "A real-looking test reply"))
        checks.append(("default adapter uses local Ollama without a paid key",
                       captured.get("api_key") == "ollama"
                       and captured.get("base_url") == "http://localhost:11434/v1"
                       and captured.get("timeout") == 120.0
                       and captured.get("model") == "qwen2.5:3b"
                       and captured.get("messages") == messages
                       and captured.get("max_tokens") == 400
                       and "OPENAI_API_KEY" not in os.environ))

        os.environ["GYMFLOW_LLM_PROVIDER"] = "openai"
        os.environ.pop("OPENAI_API_KEY", None)
        reply = ai_agent._call_llm(messages)
        checks.append(("paid provider is opt-in and requires a key",
                       "Paid AI is not configured" in reply))

        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ["GYMFLOW_LLM_MODEL"] = "test-model"
        resilience.reset_all_breakers()
        reply = ai_agent._call_llm(messages)
        checks.append(("configured cloud adapter returns model reply",
                       reply == "A real-looking test reply"))
        checks.append(("explicit cloud request uses selected model and capped tokens",
                       captured.get("api_key") == "test-key"
                       and captured.get("base_url") is None
                       and captured.get("model") == "test-model"
                       and captured.get("messages") == messages
                       and captured.get("max_completion_tokens") == 400
                       and captured.get("reasoning_effort") == "none"))

        class FakeQuery:
            def filter_by(self, **kwargs):
                now = datetime.now()
                past = types.SimpleNamespace(
                    fitness_class=types.SimpleNamespace(
                        schedule=now - timedelta(days=1), name="Old class", room="A"
                    )
                )
                future = types.SimpleNamespace(
                    fitness_class=types.SimpleNamespace(
                        schedule=now + timedelta(days=1), name="Next class", room="B"
                    )
                )
                return types.SimpleNamespace(all=lambda: [past, future])

        ai_agent.Booking.query = FakeQuery()
        member = types.SimpleNamespace(id=1, card=None)
        context = ai_agent._member_context(member)
        checks.append(("member context excludes past bookings from upcoming sessions",
                       "Next class" in context and "Old class" not in context))

        account = types.SimpleNamespace(balance=35, lifetime_points=520)
        loyalty_engine.get_account = lambda *args, **kwargs: account
        loyalty_engine.current_tier = lambda account: types.SimpleNamespace(name="Silver")
        member.card = types.SimpleNamespace(member_code="PRIVATE-123", diet_plans=[])
        member.full_name = "Private Member Name"
        context = ai_agent._member_context(member)
        checks.append(("member context includes their points and distinct rewards tier",
                       "35 spendable points" in context
                       and "520 lifetime qualifying points" in context
                       and "Silver Rewards Tier" in context))
        checks.append(("member context omits name and membership code",
                       "Private Member Name" not in context
                       and "PRIVATE-123" not in context))

        for label, passed in checks:
            print(f"{'PASS' if passed else 'FAIL'}: {label}")
        print(f"{sum(passed for _, passed in checks)}/{len(checks)} checks passed; no live API request made.")
        return 0 if all(passed for _, passed in checks) else 1
    finally:
        resilience.reset_all_breakers()
        if original_key is not None:
            os.environ["OPENAI_API_KEY"] = original_key
        else:
            os.environ.pop("OPENAI_API_KEY", None)
        if original_model is not None:
            os.environ["GYMFLOW_LLM_MODEL"] = original_model
        else:
            os.environ.pop("GYMFLOW_LLM_MODEL", None)
        if original_provider is not None:
            os.environ["GYMFLOW_LLM_PROVIDER"] = original_provider
        else:
            os.environ.pop("GYMFLOW_LLM_PROVIDER", None)
        if original_base_url is not None:
            os.environ["GYMFLOW_LLM_BASE_URL"] = original_base_url
        else:
            os.environ.pop("GYMFLOW_LLM_BASE_URL", None)
        if original_ollama_model is not None:
            os.environ["GYMFLOW_OLLAMA_MODEL"] = original_ollama_model
        else:
            os.environ.pop("GYMFLOW_OLLAMA_MODEL", None)
        if original_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = original_openai


if __name__ == "__main__":
    raise SystemExit(run())
