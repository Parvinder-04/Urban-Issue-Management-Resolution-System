"""UIMRS complaint classification + robust text normalization + priority scoring.

Design goals:
- Bengaluru uses the verified V2.2 Bengaluru candidate pipeline.
- Gurugram and unsupported cities use deterministic Indian keyword rules only.
- The legacy NYC model is not part of the operational classification path.
- Lightweight spelling/variant normalization handles common English/Hinglish
  typos for the rule-based path.
- Priority is evidence-based: severity + urgency + exposure + crowd signal + age.
- No fabricated data or fallback coordinates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from difflib import SequenceMatcher

import joblib
import numpy as np

from app.models import IssueCategory

MODEL_DIR = Path(__file__).parent.parent / "ml_models"

# ---------------------------------------------------------------------------
# City-aware classification configuration
# ---------------------------------------------------------------------------
CITY_BENGALURU = "Bengaluru, Karnataka, India"
CITY_GURUGRAM = "Gurugram, Haryana, India"

BENGALURU_MODEL_DIR = MODEL_DIR / "india_bengaluru_v2_2_candidate"
BENGALURU_MODEL_PATH = BENGALURU_MODEL_DIR / "classifier.joblib"
BENGALURU_METADATA_PATH = BENGALURU_MODEL_DIR / "metadata.json"
BENGALURU_MODEL_VERSION = "india_bengaluru_v2_2_candidate"

# Conservative abstention guards for the Bengaluru candidate. These are
# uncalibrated model-score guards, not calibrated probability thresholds.
ML_SCORE_THRESHOLD = 0.55
ML_MARGIN_THRESHOLD = 0.15

# Only the seven labels actually produced by the verified V2.2 artifact.
# Any other label is treated as unsupported and sent to human review.
MODEL_LABEL_TO_CATEGORY = {
    "Road_Damage": IssueCategory.ROAD_DAMAGE,
    "Garbage_Waste": IssueCategory.GARBAGE_WASTE,
    "Streetlight": IssueCategory.STREETLIGHT,
    "Water_Supply": IssueCategory.WATER_SUPPLY,
    "Drainage_Sewage": IssueCategory.DRAINAGE_SEWAGE,
    "Traffic_Management": IssueCategory.TRAFFIC_MANAGEMENT,
    "Trees_Environment": IssueCategory.TREES_ENVIRONMENT,
}

CITY_ALIASES = {
    "bengaluru": CITY_BENGALURU,
    "bangalore": CITY_BENGALURU,
    "gurugram": CITY_GURUGRAM,
    "gurgaon": CITY_GURUGRAM,
}

# What kind of number ``category_confidence`` actually holds, keyed by the
# persisted ``category_source``. These are deliberately distinct: an
# uncalibrated model score is not a probability, and a rule heuristic is not an
# ML confidence.
SCORE_KIND_BY_SOURCE = {
    "bengaluru_v2_2_ml": "uncalibrated_model_score",
    "india_keyword_rule": "rule_heuristic",
    "needs_clarification": "none",
    "empty_input": "none",
}


def score_kind_for_source(source: str | None) -> str | None:
    """Return the honest score type for a persisted classification source.

    Unknown sources return ``None`` so the database records "not available"
    rather than an invented category.
    """
    if not source:
        return None
    return SCORE_KIND_BY_SOURCE.get(source)

# Exact common variants/typos first; these are normalization rules, not training data.
COMMON_VARIANTS = {
    "pothloe": "pothole",
    "pothoel": "pothole",
    "pot hole": "pothole",
    "pothols": "potholes",
    "garbge": "garbage",
    "garabge": "garbage",
    "garbaj": "garbage",
    "streetlght": "streetlight",
    "streetligt": "streetlight",
    "stret light": "street light",
    "trafic": "traffic",
    "trafffic": "traffic",
    "sigal": "signal",
    "siganl": "signal",
    "dran": "drain",
    "drian": "drain",
    "sewerage": "sewage",
    "leek": "leak",
    "leackage": "leakage",
    "waterr": "water",
    "floodng": "flooding",
    "flloding": "flooding",
    "accidnt": "accident",
    "injurd": "injured",
    "dangerus": "dangerous",
    "emergncy": "emergency",
    "urgnt": "urgent",
    "immediatly": "immediately",
    "konstruction": "construction",
    "constrution": "construction",
    "encroachement": "encroachment",
    "parkng": "parking",
    "vehical": "vehicle",
    "vechile": "vehicle",
    # common Indian/Hinglish variants
    "pani": "pani",
    "panee": "pani",
    "kaachra": "kachra",
    "kachraa": "kachra",
    "gadha": "gadda",
    "gaddha": "gadda",
    "sadak": "sadak",
    "nalla": "nala",
    "nalah": "nala",
    "nali": "nali",
}

# Conservative domain vocabulary for fuzzy single-token correction.
DOMAIN_WORDS = sorted({
    "pothole", "potholes", "garbage", "trash", "waste", "dustbin", "litter",
    "streetlight", "street", "light", "traffic", "signal", "drain", "drainage",
    "sewage", "sewer", "manhole", "water", "pipeline", "leak", "leakage", "flood",
    "flooding", "parking", "vehicle", "construction", "encroachment", "tree", "fallen",
    "branch", "graffiti", "road", "crack", "cracks", "accident", "danger", "dangerous",
    "children", "school", "hospital", "fire", "electrocution", "electric", "shock",
    "collapse", "exposed", "wire", "injury", "injured", "emergency", "urgent", "immediately",
    "critical", "khatra", "turant", "jaldi", "kachra", "nala", "nali", "gadda", "sadak",
    "pani", "bijli", "chowk", "market", "colony", "society", "gali"
})

CATEGORY_KEYWORDS = {
    "Road_Damage": [
        "pothole", "potholes", "road damage", "road crack", "road cracks",
        "damaged road", "broken road", "crater", "footpath", "pavement",
        "speed breaker", "gadda", "sadak kharab", "sadak toot", "sadak",
    ],
    "Garbage_Waste": [
        "garbage", "trash", "waste", "dustbin", "litter", "dumping",
        "illegal dumping", "garbage dump", "waste pile", "kachra", "kachra pada",
        "kachra jama", "kachre ka", "waste accumulation",
    ],
    "Streetlight": [
        "streetlight", "street light", "street lamp", "lamp post", "pole light",
        "street lighting", "lighting is not working", "light is not working",
        "street lights", "bijli ki light", "road light",
    ],
    "Water_Supply": [
        "water supply", "water shortage", "water pipe", "water pipeline",
        "drinking water", "borewell", "tanker", "water connection", "pipeline leak",
        "water leakage", "water line", "pani nahi", "pani ki supply", "pani ki line",
        "nal ka pani", "water pressure",
    ],
    "Drainage_Sewage": [
        "drain", "drainage", "sewage", "sewer", "manhole", "sewer line",
        "drain overflow", "drain blocked", "blocked drain", "waterlogging",
        "stagnant water", "nala", "nala blocked", "nali", "dirty water overflow",
    ],
    "Traffic_Signal": [
        "traffic signal", "traffic light", "signal failure", "signal not working",
        "signal is not working", "red light", "junction signal", "chowk signal",
    ],
    "Traffic_Management": [
        "illegal parking", "wrong parking", "blocked driveway", "abandoned vehicle",
        "derelict vehicle", "parking on road", "vehicle obstruction", "traffic obstruction",
        "parked illegally", "roadside parking",
    ],
    "Illegal_Construction": [
        "illegal construction", "unauthorized construction", "unauthorised construction",
        "illegal building", "encroachment", "encroached", "unauthorized structure",
        "unauthorised structure", "illegal extension",
    ],
    "Trees_Environment": [
        "damaged tree", "fallen tree", "fallen branch", "overgrown tree", "tree branch",
        "dead tree", "dying tree", "tree blocking road", "green infrastructure",
    ],
    "Public_Property_Damage": [
        "damaged bench", "park bench", "public property", "broken public toilet",
        "graffiti", "damaged sign", "missing sign", "dangling sign", "public shelter",
    ],
}

CATEGORY_BASE_SEVERITY = {
    "Road_Damage": 3.0,
    "Garbage_Waste": 2.0,
    "Streetlight": 3.0,
    "Water_Supply": 4.0,
    "Drainage_Sewage": 4.0,
    "Traffic_Signal": 5.0,
    "Traffic_Management": 3.0,
    "Illegal_Construction": 2.0,
    "Trees_Environment": 3.0,
    "Public_Property_Damage": 2.0,
    "Other": 2.0,
}

URGENCY_PHRASES = [
    "accident", "accidents", "danger", "dangerous", "children", "school", "hospital",
    "fire", "electrocution", "electric shock", "collapse", "collapsed", "flood", "flooding",
    "exposed wire", "live wire", "gas leak", "injury", "injured", "emergency", "urgent",
    "immediately", "critical", "life threatening", "jaan ka khatra", "bahut dangerous",
    "turant", "jaldi", "emergency hai", "people are falling", "people falling", "vehicles falling",
    "severe", "major", "huge", "deep", "completely blocked", "completely broken",
]

EXPOSURE_PHRASES = [
    "school", "hospital", "market", "main road", "main intersection", "junction", "chowk",
    "bus stop", "railway station", "crowded", "busy road", "busy intersection", "residential area",
    "public road", "pedestrians", "children", "elderly",
]

@dataclass
class ClassificationResult:
    category: object
    confidence: float
    source: str
    needs_review: bool = False
    review_reason: str | None = None
    model_version: str | None = None
    top_alternatives: list[dict] = field(default_factory=list)

@dataclass
class PriorityResult:
    score: float
    level: str
    reasons: list[str]


def _basic_normalize(text: str) -> str:
    text = str(text or "").lower()
    text = text.replace("’", "'")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _collapse_repeated_letters(token: str) -> str:
    if len(token) < 5:
        return token
    return re.sub(r"(.)\1{2,}", r"\1\1", token)


def _fuzzy_correct_token(token: str) -> str:
    if len(token) < 5 or token in DOMAIN_WORDS:
        return token
    best_word = token
    best_score = 0.0
    for candidate in DOMAIN_WORDS:
        # Length gate prevents extreme corrections.
        if abs(len(candidate) - len(token)) > 2:
            continue
        score = SequenceMatcher(None, token, candidate).ratio()
        if score > best_score:
            best_score = score
            best_word = candidate
    return best_word if best_score >= 0.87 else token


def normalize_civic_text(text: str) -> str:
    """Normalize common English/Hinglish spelling variants conservatively."""
    value = _basic_normalize(text)
    for src, dst in sorted(COMMON_VARIANTS.items(), key=lambda x: len(x[0]), reverse=True):
        value = re.sub(rf"(?<!\w){re.escape(src)}(?!\w)", dst, value)

    tokens = []
    for tok in value.split():
        tok = _collapse_repeated_letters(tok)
        tok = _fuzzy_correct_token(tok)
        tokens.append(tok)
    return " ".join(tokens)


def canonical_city(city: str | None) -> str | None:
    """Return the canonical supported city, or ``None`` for an unknown city.

    Only the documented aliases are mapped. An unrecognised value is never
    silently treated as Bengaluru or Gurugram.
    """
    value = str(city or "").strip().lower()
    if not value:
        return None
    for token in re.findall(r"[a-z]+", value):
        canonical = CITY_ALIASES.get(token)
        if canonical is not None:
            return canonical
    return None


def combine_complaint_text(title: str | None, description: str | None) -> str:
    """Build the exact input text used by the V2.2 training pipeline.

    Training used ``title + " " + description`` with whitespace collapsed and
    surrounding whitespace stripped. The TF-IDF vectorizer performs lowercasing
    and accent stripping, so no additional typo/fuzzy rewriting is applied here.
    """
    parts = [str(title or "").strip(), str(description or "").strip()]
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()


class IssueClassifier:
    """City-aware civic classifier.

    Bengaluru complaints use the verified V2.2 Bengaluru candidate pipeline.
    Gurugram and unsupported cities use deterministic Indian keyword rules
    only. The legacy NYC model is never loaded on this operational path.
    """

    def __init__(self, model_path: Path | str | None = None,
                 metadata_path: Path | str | None = None):
        self.model_path = (
            Path(model_path) if model_path is not None else BENGALURU_MODEL_PATH
        )
        self.metadata_path = (
            Path(metadata_path) if metadata_path is not None else BENGALURU_METADATA_PATH
        )
        self.metadata = self._read_metadata()

        # Lazy, one-time load. A missing or corrupt artifact must not break
        # FastAPI import or unrelated rule-based classification.
        self._bengaluru_model = None
        self._bengaluru_load_attempted = False
        self._bengaluru_load_error = None

    def _read_metadata(self) -> dict:
        try:
            if self.metadata_path.exists():
                return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {}

    @staticmethod
    def _model_classes(model):
        classes = getattr(model, "classes_", None)
        if classes is None:
            classifier = getattr(model, "named_steps", {}).get("classifier")
            classes = getattr(classifier, "classes_", None)
        return classes

    def _load_bengaluru_model(self):
        """Load the V2.2 candidate once. Return ``None`` if unavailable."""
        if self._bengaluru_load_attempted:
            return self._bengaluru_model
        self._bengaluru_load_attempted = True
        try:
            if not self.model_path.exists():
                raise FileNotFoundError(
                    f"Bengaluru V2.2 candidate model not found: {self.model_path}"
                )
            model = joblib.load(self.model_path)
            if not hasattr(model, "predict_proba"):
                raise ValueError(
                    "Bengaluru candidate model does not expose predict_proba()."
                )
            if not len(self._model_classes(model)):
                raise ValueError(
                    "Bengaluru candidate model does not expose class labels."
                )
            self._bengaluru_model = model
            self._bengaluru_load_error = None
        except Exception as exc:  # noqa: BLE001 - corrupt artifacts must not crash startup
            self._bengaluru_model = None
            self._bengaluru_load_error = str(exc)
        return self._bengaluru_model

    def _bengaluru_scores(self, text: str):
        model = self._load_bengaluru_model()
        if model is None:
            return None
        probabilities = np.asarray(model.predict_proba([text])[0], dtype=float)
        return list(self._model_classes(model)), probabilities

    def _keyword_scores(self, normalized_text: str):
        results = []
        for category, keywords in CATEGORY_KEYWORDS.items():
            score = 0.0
            matched = []
            for keyword in keywords:
                kw = _basic_normalize(keyword)
                if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", normalized_text):
                    # Specific phrases carry more weight than generic single tokens.
                    weight = 2.5 if " " in kw else 1.5
                    if kw in {"road", "sadak", "nala", "nali", "drain", "signal", "light", "street"}:
                        weight = 1.0
                    score += weight
                    matched.append(keyword)
            if score > 0:
                results.append((category, score, matched))
        return sorted(results, key=lambda x: x[1], reverse=True)

    def _keyword_result(self, normalized_text: str) -> ClassificationResult | None:
        scores = self._keyword_scores(normalized_text)
        if not scores:
            return None
        category, score, _ = scores[0]
        second = scores[1][1] if len(scores) > 1 else 0.0
        if score >= 2.5 or score - second >= 1.0:
            heuristic = min(0.70 + 0.08 * score, 0.95)
            try:
                mapped = IssueCategory(category)
            except ValueError:
                return None
            return ClassificationResult(mapped, heuristic, "india_keyword_rule")
        return None

    def _bengaluru_result(self, text: str) -> ClassificationResult:
        model_text = re.sub(r"\s+", " ", str(text or "")).strip()
        scores = self._bengaluru_scores(model_text)
        if scores is None:
            return ClassificationResult(
                IssueCategory.OTHER,
                0.0,
                "needs_clarification",
                needs_review=True,
                review_reason="Bengaluru V2.2 model unavailable or unreadable",
                model_version=BENGALURU_MODEL_VERSION,
            )

        classes, probabilities = scores
        order = probabilities.argsort()[::-1]
        top_index = int(order[0])
        top_score = float(probabilities[top_index])
        second_score = float(probabilities[order[1]]) if len(order) > 1 else 0.0
        margin = top_score - second_score
        raw_label = str(classes[top_index])
        top_alternatives = [
            {
                "category": str(classes[index]),
                "model_score": round(float(probabilities[index]), 4),
            }
            for index in order[:3]
        ]

        mapped = MODEL_LABEL_TO_CATEGORY.get(raw_label)
        if mapped is None:
            return ClassificationResult(
                IssueCategory.OTHER,
                0.0,
                "needs_clarification",
                needs_review=True,
                review_reason=f"Unsupported model label: {raw_label}",
                model_version=BENGALURU_MODEL_VERSION,
                top_alternatives=top_alternatives,
            )

        if top_score < ML_SCORE_THRESHOLD or margin < ML_MARGIN_THRESHOLD:
            return ClassificationResult(
                IssueCategory.OTHER,
                0.0,
                "needs_clarification",
                needs_review=True,
                review_reason="Low model score or small top-two margin",
                model_version=BENGALURU_MODEL_VERSION,
                top_alternatives=top_alternatives,
            )

        return ClassificationResult(
            mapped,
            top_score,
            "bengaluru_v2_2_ml",
            needs_review=False,
            review_reason=None,
            model_version=BENGALURU_MODEL_VERSION,
            top_alternatives=top_alternatives,
        )

    def classify(self, text: str, city: str | None = None) -> ClassificationResult:
        original = str(text or "").strip()
        if not original:
            return ClassificationResult(
                IssueCategory.OTHER,
                0.0,
                "empty_input",
                needs_review=True,
                review_reason="Empty complaint text",
            )

        canonical = canonical_city(city)
        if canonical == CITY_BENGALURU:
            return self._bengaluru_result(original)

        normalized = normalize_civic_text(original)
        rule_result = self._keyword_result(normalized)
        if rule_result is not None:
            return rule_result

        reason = (
            "No clear Indian keyword rule matched"
            if canonical == CITY_GURUGRAM
            else "Unsupported or missing city and no clear keyword rule matched"
        )
        return ClassificationResult(
            IssueCategory.OTHER,
            0.0,
            "needs_clarification",
            needs_review=True,
            review_reason=reason,
        )


class PriorityEngine:
    """Explainable 0–10 priority based on multiple real evidence signals."""

    def score(
        self,
        text: str,
        category: str,
        duplicate_count: int = 0,
        hours_since_reported: float = 0,
    ) -> PriorityResult:
        reasons: list[str] = []
        score = float(CATEGORY_BASE_SEVERITY.get(category, 2.0))
        reasons.append(f"Base severity for {category}: {score:g}")

        normalized = normalize_civic_text(text)
        urgency_hits = [
            phrase for phrase in URGENCY_PHRASES
            if re.search(rf"(?<!\w){re.escape(_basic_normalize(phrase))}(?!\w)", normalized)
        ]
        urgency_hits = list(dict.fromkeys(urgency_hits))
        if urgency_hits:
            boost = min(len(urgency_hits) * 1.4, 4.0)
            score += boost
            reasons.append(f"Urgency/severity evidence ({', '.join(urgency_hits)}): +{boost:g}")

        exposure_hits = [
            phrase for phrase in EXPOSURE_PHRASES
            if re.search(rf"(?<!\w){re.escape(_basic_normalize(phrase))}(?!\w)", normalized)
        ]
        exposure_hits = list(dict.fromkeys(exposure_hits))
        if exposure_hits:
            boost = min(len(exposure_hits) * 0.6, 1.8)
            score += boost
            reasons.append(f"Public exposure/context ({', '.join(exposure_hits)}): +{boost:g}")

        if duplicate_count > 0:
            dup_boost = min(duplicate_count * 0.5, 3.0)
            score += dup_boost
            reasons.append(f"{duplicate_count} other reports linked to same issue: +{dup_boost:g}")

        if hours_since_reported > 72:
            score += 1.0
            reasons.append("Unresolved for over 72 hours: +1")
        elif hours_since_reported > 24:
            score += 0.5
            reasons.append("Unresolved for over 24 hours: +0.5")

        # High/critical requires multiple pieces of evidence unless the base category is inherently severe.
        score = min(round(score, 1), 10.0)
        if score >= 8.0:
            level = "Critical"
        elif score >= 6.0:
            level = "High"
        elif score >= 3.5:
            level = "Medium"
        else:
            level = "Low"

        return PriorityResult(score, level, reasons)
