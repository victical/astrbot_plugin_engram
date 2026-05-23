import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from astrbot_plugin_engram.services.intent_classifier import IntentClassifier


class DummyProvider:
    def __init__(self):
        self.prompts = []

    async def text_chat(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(completion_text="是")


class DummyContext:
    def __init__(self, provider):
        self.provider = provider

    def get_using_provider(self):
        return self.provider

    def get_provider_by_id(self, provider_id):
        return None


def test_llm_intent_prompt_accepts_user_braces_verbatim():
    provider = DummyProvider()
    classifier = IntentClassifier(
        config={"memory_intent_mode": "llm", "intent_min_length": 1},
        context=DummyContext(provider),
    )

    result = asyncio.run(classifier.should_retrieve_memory("之前说过 {x} 吗"))

    assert result is True
    assert "之前说过 {x} 吗" in provider.prompts[0]


def test_intent_classifier_reads_hot_config_each_call():
    config = {"memory_intent_mode": "disabled", "intent_min_length": 20}
    classifier = IntentClassifier(config=config)

    assert asyncio.run(classifier.should_retrieve_memory("hi")) is True

    config["memory_intent_mode"] = "keyword"
    config["intent_min_length"] = 1
    config["intent_weak_triggers"] = ["custom-hot-trigger"]
    config["intent_trigger_score_threshold"] = 1

    assert classifier.classify_query("custom-hot-trigger") != ("skip", 0.0)
    assert asyncio.run(classifier.should_retrieve_memory("custom-hot-trigger")) is True
