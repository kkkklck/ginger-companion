# Paper library · Local evidence and citations

[← Back to the project](../README.md)

## What the public repository includes

The application includes local retrieval, citation display, and a developer review interface. The author's PDF collection, extracted evidence packages, research snapshots, and runtime databases are not distributed with this repository. A fresh installation starts with an empty paper library.

## Connecting local materials

The store reads compatible catalogs and evidence packages from the project directory. Relevant locations include:

| Location | Role |
| :--- | :--- |
| `ginger_evidence_catalog.json` | Optional source catalog |
| `papers/` | Local original papers |
| `ginger_evidence_results/` | Compatible extracted evidence packages |
| `ginger_companion_data/paper_library.sqlite3` | Rebuildable local library index |

Obtain or prepare compatible evidence packages using the separate extraction workflow. The historical extraction tool is not included in this application release. Simply adding an arbitrary PDF does not automatically create indexed evidence cards.

Use original papers that you are authorized to access and retain matching source files. The application checks supported source bindings, page references, and file fingerprints when importing evidence. Optional page-image provenance checks require `pymupdf`.

## Retrieval and answers

1. Ask a growing question or open the paper-evidence search.
2. The application retrieves relevant local evidence using keywords and synonym rules.
3. During AI chat, relevant excerpts are sent with field context to the configured text service.
4. Expand citations and inspect the original paper and study conditions.

Retrieval matches and citations actually used in an answer may differ. Saved citation snapshots help preserve the source version associated with an answer. Older conversations without source metadata do not acquire fabricated citations.

## Interpreting evidence

Automated evidence extraction and review do not establish that a study is reliable or applicable to a particular field. Check the original design, treatment conditions, units, comparisons, and limits. Important recommendations require review against local conditions.

Missing or changed source files can make evidence unavailable. Avoid editing original papers to resolve version mismatches; preserve provenance and regenerate compatible records when needed.

The paper library provides context for answers. It does not train a validated yield or harvest-date prediction model.

## Developer review interface

The release includes the review-page shell with an empty offline snapshot. When opened through the application's developer entry, it reads the locally available evidence. Review notes saved in the browser do not independently change evidence-admission status.
