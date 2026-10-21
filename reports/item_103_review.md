# Item 1.03 scan: manual spot-check (2026-10-20)

Companion to the generated [item_103_spotcheck.md](item_103_spotcheck.md). The 8-Ks below were
opened and read on EDGAR. All of them are already recorded in `staging.quarantine`
(source `item_scan`). Nothing was changed in the data.

## Findings

| Company (CIK) | 8-K filed | What the filing actually says | Verdict |
|---|---|---|---|
| AT&T (0000732717) | 2012-01-13 | Labelled "Item 1.03" but describes cutting a $5B revolver to $3B (an Item 1.01/1.02 matter) | **Mis-tagged**, false positive |
| J.C. Penney (0001166126) | 2014-01-28 | Item list includes 1.03, but the document has no Item 1.03 section (credit-agreement 8-K) | **Mis-tagged**, false positive |
| Entergy Arkansas (0000007323) | 2005-09-28 | Joint 8-K filed by Entergy Corp for all co-registrants; the bankrupt entity is sister company Entergy New Orleans | **Co-registrant**, false positive for this CIK |
| Alto Ingredients / Pacific Ethanol (0000778164) | 2009-05-18 | Five indirect subsidiaries filed Chapter 11; the parent did not | **Subsidiary only**, ambiguous |
| Inhibitor Therapeutics (0001042418) | 2008-10-21 | UK subsidiary Exelgen entered administration | **Subsidiary only**, ambiguous |
| Big Lots (0000768835) | 2025-10-30 | Update on the 2024 case during the wind-down | **Same episode**, not a new event |

Hits that look genuine from public record (not opened): Dana 2006, Lee Enterprises 2011,
Battalion Oil (Halcón) 2016 and 2019, Thryv (Dex Media) 2013, Aspira (Vermillion) 2009,
INNOVATE (Primus Telecommunications) 2009.

## Implications for Phase 4 labels

1. **A raw Item 1.03 tag is not a reliable label.** In a 205-company sample, 2 of 11
   random-sample hits were mis-tags and 1 was a sibling company's bankruptcy, so the false
   positive rate is not negligible.
2. **Joint filings are common.** One accession can cover several co-registrants. The bankrupt
   entity has to be checked against the filing CIK, for example from the 8-K text.
3. **Subsidiary-only bankruptcies** need a decision: positive (real distress), negative (parent
   survives), or a separate secondary label. Recommendation: separate event type, off by
   default, like the other secondary labels.
4. **Episodes:** follow-up 1.03 filings (confirmation, emergence, wind-down) are not new events.

Proposed approach: keep the tag scan as the candidate generator. Add a text check (rule-based
first, then the Phase 5 LLM extractor) that confirms "this registrant filed a petition" and
records `label_source_accession` only when confirmed.
