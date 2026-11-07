---
title: Reading a health report
summary: Interpret EDC data-characterisation checks and Ovid2 health panels — outcomes, thresholds, and what to do next.
audience: [researcher, user]
---

A `health` or `dq_checks` run produces:

- a **results table**, `dcq_results`, at its output address: one row per
  check per metric;
- a **report** at `/runs/<instance_id>/report` in the web panel.

## Outcomes

| Outcome | Meaning | Typical action |
| --- | --- | --- |
| `pass` | within threshold | none |
| `warn` | outside the warning threshold | review; may be expected for your site |
| `fail` | outside the failure threshold | investigate before using the affected table |
| `not_applicable` | the check's input tables or columns are absent | confirm the table is genuinely out of scope |
| `error` | the check couldn't run | read the message; usually a missing table or permission |

## Check families

### PCORnet EDC / Data Characterization (`DC x.yy`)

These are the network's standard data-curation queries, grouped into four
parts:

| Part | Focus | Examples |
| --- | --- | --- |
| part1 | data model conformance | required tables present (DC 1.01), required fields, field types, valueset conformance |
| part2 | data plausibility | future dates, records before birth or after death, extreme values |
| part3 | data completeness | missing or unknown shares, mapping to standard terminologies (e.g. DC 3.09), orphan records |
| part4 | data persistence | volume trends over refreshes |

### Ovid2 health panels (`health.*`)

| Panel | Asks |
| --- | --- |
| `pk_uniqueness` | is every table's key unique? |
| `valueset_conformance` | what share of bounded values are outside the CDM valueset? |
| `null_rate` | how often are important fields null or flavour-of-null? |
| `future_date` | how many dates lie after the refresh date? |
| `freshness` | how does volume compare with the previous refresh? (needs `--prev_input_address`) |
| `mapping_coverage` | what share of codes resolve in their bound vocabulary? |
| `orphan_fk` | how many child rows reference a missing parent? |

## Reading one result

Each result shows the check id and name, its description, the threshold,
the **metrics** (counts, rates, or per-table breakdowns) and a message.
Click a check to expand its per-table detail.

## From a finding to a fix

| Finding | Usual cause | Remedy in the pipeline |
| --- | --- | --- |
| values outside a valueset | an unmapped local code | add it to `dictionaries.py` or the mapper |
| orphan records | a child extract wider than the parent extract | enable an `*_orphans` fixer, or fix the extract |
| future dates | data-entry or timezone issues | `future_date_to_null` |
| low mapping coverage | local or obsolete codes | map to standard codes; check the code type column |
| missing required table | the source doesn't provide it | confirm with the data owner; mark as not provided |

Report cycles (`20r1`, `dcq_v7_03`, …) are listed by
`ovid2 health --list-packages`. Each is a YAML list of check ids, so you
can see exactly which checks a cycle contains.
