# Bengaluru Classifier Model Directory

The public UIMRS release **does not include the trained Bengaluru classifier artifact**.
This directory is intentionally empty apart from this README and `__init__.py`.

## Why the artifact is excluded

The original academic artifact was trained on a Bengaluru complaint log for which no
explicit redistribution or training-data permission was established. A trained text
classifier can encode information from its training corpus, so the artifact is not
published.

See [`docs/MODEL_AND_DATA_LICENSING.md`](../../../docs/MODEL_AND_DATA_LICENSING.md).

## What happens without the artifact

The application remains fully runnable:

- Bengaluru complaints return category `Other`;
- the classification source is `needs_clarification`;
- the complaint is marked for human review;
- the review reason states that the model artifact is unavailable;
- no model version, confidence or performance metric is invented;
- Gurugram and unknown cities continue to use deterministic keyword rules.

## Installing an authorised model

If you have documented permission to use an authorised artifact, place it at:

```text
backend/app/ml_models/india_bengaluru_v2_2_candidate/classifier.joblib
```

Optional approved metadata:

```text
backend/app/ml_models/india_bengaluru_v2_2_candidate/metadata.json
```

The artifact must:

- be loadable with `joblib.load`;
- expose `predict_proba`;
- expose class labels through `classes_` or `named_steps["classifier"].classes_`;
- accept a single combined title-and-description string;
- produce the seven expected labels:
  `Road_Damage`, `Garbage_Waste`, `Streetlight`, `Traffic_Management`,
  `Drainage_Sewage`, `Water_Supply`, `Trees_Environment`.

The classifier applies its own score and margin guards. Unsupported labels and
low-confidence predictions are sent to review.

## What not to do

- Do not substitute an unrelated model.
- Do not rename a different artifact to look like the original.
- Do not claim the original model's recorded metrics for a different artifact.
- Do not commit an artifact unless you have documented redistribution permission.
- Do not publish model binaries in a public repository without reviewing the licence and
  training-data terms.

## Metadata

If you install an approved metadata file, keep it factual. Do not add performance claims
that have not been measured on the artifact and data you actually use.
