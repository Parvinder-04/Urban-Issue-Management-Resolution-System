"""
Citizen Support Chatbot
-------------------------
Intent-classification based chatbot (not an LLM call) for two reasons:
1. Cost/latency — most citizen queries are the same handful of intents
   (check status, how to report, working hours, escalation). A 50ms local
   classifier beats a network round-trip to an LLM API for these.
2. Reliability — a municipal chatbot giving a hallucinated wrong answer
   about a legal/civic process is a real liability. Intent -> fixed,
   pre-approved response template is safer for this domain.

For genuinely open-ended queries that don't match a known intent, this is
designed to hand off to a human support agent (see `needs_human_handoff`)
rather than guess. A production build could route that fallback slice to
an LLM API (Claude) as an enhancement — noted in the design doc.

Multilingual: keyword sets are duplicated in English and Hindi (Devanagari
+ common Hinglish spellings), matching the primary languages of Gurugram
citizens. Adding a new language = adding one more keyword list.
"""
import re
from dataclasses import dataclass

INTENTS = {
    "check_status": {
        "en": ["status", "track", "where is my complaint", "update on my issue", "what happened to"],
        "hi": ["status", "sthiti", "mera complaint", "kaha hai", "kya hua"],
        "response": "To check your complaint status, please share your Complaint ID "
                    "(e.g. UIMRS-2024-00123) and I'll pull up the latest update.",
    },
    "how_to_report": {
        "en": ["how to report", "how do i report", "file a complaint", "raise an issue", "new complaint"],
        "hi": ["kaise report", "shikayat kaise", "complaint kaise"],
        "response": "You can report a new issue by tapping the '+ Report Issue' button, "
                    "taking a photo of the problem, and pinning its location on the map. "
                    "Our AI will auto-categorize it and route it to the right department.",
    },
    "escalate": {
        "en": ["not resolved", "still not fixed", "escalate", "no response", "too slow", "delay"],
        "hi": ["theek nahi hua", "abhi tak nahi", "escalate karo", "jawab nahi"],
        "response": "I'm sorry this hasn't been resolved yet. I'm escalating your complaint "
                    "to a senior officer now. You'll receive an update within 24 hours.",
    },
    "working_hours": {
        "en": ["working hours", "office hours", "when open", "contact time"],
        "hi": ["office time", "kab khula"],
        "response": "Our support desk is available Monday-Saturday, 9 AM - 6 PM. "
                    "Issue reporting through the app works 24/7.",
    },
    "greeting": {
        "en": ["hi", "hello", "hey", "namaste"],
        "hi": ["namaste", "hii", "hello"],
        "response": "Namaste! I'm your civic assistant. I can help you report a new issue, "
                     "check the status of an existing complaint, or answer questions. "
                     "How can I help?",
    },
}

CONFIDENCE_FLOOR = 0.0  # any keyword hit counts; simple by design


@dataclass
class ChatbotResponse:
    intent: str
    response: str
    needs_human_handoff: bool
    matched_language: str


def detect_intent(message: str) -> ChatbotResponse:
    message_lower = message.lower().strip()

    for intent_name, config in INTENTS.items():
        for lang in ("en", "hi"):
            for keyword in config[lang]:
                if keyword in message_lower:
                    return ChatbotResponse(
                        intent=intent_name,
                        response=config["response"],
                        needs_human_handoff=False,
                        matched_language=lang,
                    )

    # No known intent matched -> don't guess, hand off
    return ChatbotResponse(
        intent="unknown",
        response="I'm not fully sure I understood that. I'm connecting you with a "
                  "support agent who can help further. In the meantime, you can also "
                  "report a new issue or check an existing complaint's status directly "
                  "from the app menu.",
        needs_human_handoff=True,
        matched_language="unknown",
    )


if __name__ == "__main__":
    test_messages = [
        "Hi there!",
        "What's the status of my complaint UIMRS-2024-00089?",
        "How do I file a complaint about a pothole?",
        "It's been 2 weeks and still not fixed, please escalate",
        "mera complaint ka status kya hai",
        "Can you help me file my income tax return?",  # out of domain -> handoff
    ]
    for msg in test_messages:
        result = detect_intent(msg)
        print(f"\nUser: {msg}")
        print(f"  Intent: {result.intent} (lang={result.matched_language}, "
              f"handoff={result.needs_human_handoff})")
        print(f"  Bot: {result.response}")
