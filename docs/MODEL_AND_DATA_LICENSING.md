# Model, Dataset and Licence Review

This document records what the public release does and does not redistribute, and the
licence decisions that remain with the project owner. It is deliberately conservative:
public visibility is not treated as permission to redistribute data or model artifacts.

---

## 1. Summary of decisions

| Item | Public-release decision | Reason |
|---|---|---|
| Application source code | Included | Written for the academic project |
| Four web portals | Included | Part of the application |
| Alembic migrations | Included | Required to create an empty database |
| Runtime scripts | Included | Setup, migration, provisioning, backup/restore |
| 21-row starter department directory | Included via a seed script | Public agency directory information, not complaint records |
| Historical complaint database | **Excluded** | No documented redistribution permission; contains complaint records |
| Bengaluru classifier artifact | **Excluded** | No documented training-data or redistribution permission |
| Bengaluru model metadata | **Excluded** | Describes an excluded artifact; no public performance claim is made |
| Test accounts, hashes, JWTs, secrets | **Excluded** | Private credentials |
| Uploads, evidence, backups, logs | **Excluded** | Potentially personal data |
| Raw/processed training datasets | **Excluded** | No redistribution permission |
| Software licence file | **Not added** | The owner must choose; this release does not invent one |

---

## 2. Bengaluru classifier

### What the original project used

The internal academic build used a Bengaluru-specific text classification pipeline:

- TF-IDF features over complaint title and description;
- a logistic-regression classifier;
- seven mapped labels: `Road_Damage`, `Garbage_Waste`, `Streetlight`,
  `Traffic_Management`, `Drainage_Sewage`, `Water_Supply`, `Trees_Environment`;
- conservative score and top-two-margin guards, with abstention to human review.

The internal metadata recorded a training set of 11,969 Bengaluru complaint records
associated with the I Change My City / Janaagraha complaint log (2019–2022), and
five-fold grouped cross-validation with a recorded mean accuracy of 0.8874 and mean
macro-F1 of 0.8345.

Those figures are **historical internal metadata only**. They are not a claim about this
public release, they have not been independently reproduced here, and the artifact is not
shipped.

### Why the artifact is excluded

No explicit licence, written permission, or terms of use permitting redistribution of the
trained model or its training data was found in the project. A trained text classifier can
encode information from its training corpus, so the absence of permission is treated as a
blocker for publishing the artifact.

### What happens when the artifact is absent

The classifier remains runnable:

- it does not crash at import or startup;
- Bengaluru complaints return `IssueCategory.OTHER`;
- the source is `needs_clarification`;
- `needs_review` is `True`;
- the review reason states that the model artifact is unavailable and points to the model
  README;
- no model version is recorded for an artifact that was never loaded;
- no confidence or performance number is invented.

Gurugram and unknown cities are unaffected: they use deterministic keyword rules and never
invoke the Bengaluru model.

### How an authorised model can be installed

An owner with documented permission may install an artifact at:

```text
backend/app/ml_models/india_bengaluru_v2_2_candidate/classifier.joblib
```

Optional approved metadata:

```text
backend/app/ml_models/india_bengaluru_v2_2_candidate/metadata.json
```

Requirements:

- the object must expose `predict_proba`;
- it must expose the seven expected class labels;
- it must accept a single combined title-and-description string;
- the operator must record the model provenance, permission and evaluation separately.

See [`../backend/app/ml_models/README.md`](../backend/app/ml_models/README.md).

**Do not** substitute an unrelated model, rename it to look like the original, or claim
the original metrics for a different artifact.

---

## 3. Historical complaint dataset

### Provenance

The original academic project analysed a Bengaluru civic complaint log associated with
I Change My City / Janaagraha (2019–2022). The private working database contained
historical complaint rows used for analysis and model experiments.

### Why it is excluded

- No explicit redistribution permission or licence was found.
- Complaint records may contain personal data or free text supplied by citizens.
- The public release must not expose private complaint records, source identifiers,
  precise coordinates, evidence or derived exports.

### Public behaviour without the dataset

The application starts with an empty database. The public dashboard shows explicit
empty-state messages. It does not fabricate records, charts, hotspots, resolution times or
priority values.

### If an owner has permission

The owner may load an approved dataset into their own deployment after confirming the
legal basis and removing or lawfully processing personal data. The public release does not
ship the private importer or the dataset. Any imported rows must be marked as historical
so that public analytics remain separate from live submissions.

---

## 4. Starter department directory

`scripts/seed_public_departments.py` contains 21 starter rows:

- 10 Bengaluru rows with agency names and categories, no contact details, and
  `routing_mode=manual_review`;
- 11 Gurugram rows with official public agency contacts, jurisdiction notes, verification
  source URLs, and `automatic`/`manual_review`/`disabled` modes.

These rows are public agency directory information, not complaint records. They contain no
personal mobile numbers. They are provided to make routing demonstrable on an empty
database.

The operator is responsible for re-verifying contacts and jurisdiction before operational
use. The seed script is idempotent and does not overwrite rows the operator has edited.

---

## 5. Third-party software licences

The main direct dependencies and their declared licences are listed below. This is a
summary, not legal advice. The authoritative licence text ships with each package.

| Package | Version in the verified environment | Declared licence |
|---|---|---|
| FastAPI | 0.141.1 | MIT |
| Uvicorn | 0.53.0 | BSD-3-Clause |
| SQLAlchemy | 2.0.54 | MIT |
| Alembic | 1.20.0 | MIT |
| python-jose | 3.5.0 | MIT |
| bcrypt | 5.0.0 | Apache-2.0 |
| python-multipart | 0.0.32 | Apache-2.0 |
| scikit-learn | 1.9.1 | BSD-3-Clause |
| NumPy | 2.5.3 | BSD-3-Clause plus bundled component licences (0BSD, MIT, Zlib, CC0-1.0) |
| pandas | 3.0.6 | BSD-3-Clause |
| opencv-python | 5.0.0.93 | Apache-2.0 (OpenCV) |
| Pydantic | 2.13.5 | MIT |
| pytest | 8.3.3 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| joblib | 1.6.0 | BSD-3-Clause |

Frontend runtime dependencies:

- **Leaflet** is loaded from a public CDN by the historical dashboard. Leaflet is
  distributed under the BSD-2-Clause licence.
- **OpenStreetMap tiles** are used with attribution. OpenStreetMap data is subject to the
  Open Database Licence (ODbL) and the OpenStreetMap Foundation tile usage policy. A
  public deployment with heavy traffic must arrange an appropriate tile provider.

The project does not vendor these libraries. Installing them creates a separate
obligation to retain their licence notices.

---

## 6. Software licence status

**No software licence has been added to this repository.** The source code is therefore
not offered under an open-source licence by default. The academic owner must decide.

Available choices include:

1. **MIT** — short, permissive, widely used for academic projects; allows reuse with
   attribution and no warranty.
2. **Apache-2.0** — permissive with an explicit patent grant and notice requirements;
   suitable if patent clarity matters.
3. **GPL-3.0** — copyleft; derivative distributions must remain under the same licence.
4. **Proprietary / all rights reserved** — no reuse permission beyond what the owner
   grants in writing.
5. **Dual licence or academic-only licence** — for example, free for research and
   separate terms for commercial use.

Before publishing, the owner should:

- choose one licence and add the full licence text as `LICENSE`;
- add a short licence statement to the README;
- confirm that all team members agree;
- keep model and dataset permissions separate from the code licence;
- retain third-party licence notices.

Do not describe the project as “open source” until a licence has actually been added.

---

## 7. Remaining owner decisions

1. Choose the software licence and add `LICENSE`.
2. Decide whether to pursue written permission for the Bengaluru model and historical
   dataset. If permission is granted, record it and publish the artifact/data separately
   with clear terms.
3. Decide whether the starter department directory should be re-verified against current
   official sources before operational use.
4. Decide whether to add screenshots (with no personal data) and real team contribution
   details.
5. Decide whether to publish the repository publicly, privately, or not at all.
6. Decide whether to add a code of conduct, contribution guide or security policy.

Until decisions 1 and 2 are resolved, this release is safe to run locally but should not
be presented as a fully licensed open-source project.
