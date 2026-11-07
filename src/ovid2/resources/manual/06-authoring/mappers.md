---
title: Mapper specs
summary: Translate column values with an ordered list of declarative ops — dictionaries, vocabularies, hashing, regex and transforms.
audience: [developer]
---

A mapper spec lists **ops** that the map stage applies to one table, in
order. It lives at `mappers/<table>.yaml`, or `mappers/<table>.py` with
`SPEC = {...}`.

```yaml
v: 1
table: condition
ops:
  - op: case_map
    column: CONDITION_STATUS
    mode: upper
  - op: translate_by_dict
    column: CONDITION_STATUS
    dict: condition_condition_status_dict
    default: OT

  - op: hash_id
    column: PATID

  - op: regex_replace
    column: CONDITION
    pattern: "\\."
    replacement: ""
```

## The ops

| Op | Use it to | Key params |
| --- | --- | --- |
| `translate_by_dict` | map values through a `dictionaries.py` dict | `column`, `dict`, `default`, `keep_null` |
| `translate_by_concept` | map values through a registered vocabulary | `column`, `vocabulary`, `match`, `target`, `domain_filter`, `keep_unmatched` |
| `hash_id` | deterministically hash an identifier with the pipeline salt | `column`, `salt_key`, `output_length` |
| `case_map` | upper, lower or title case | `column`, `mode` |
| `regex_replace` | regex substitution | `column`, `pattern`, `replacement` |
| `literal` | set a constant | `column`, `value` |
| `passthrough` | no-op (explicitly "nothing to map") | — |
| `apply_transforms` | run a chain from the [transforms library](transforms.md) | `steps` or `spec` |
| `legacy_select` | reproduce a legacy mapper's projection (generated ports) | `columns` |

Every op with every parameter, generated from the code: [Mapper ops](../10-reference/mapper-ops.md).

## Hashing identifiers

`hash_id` replaces a column with `sha256(salt + "|" + value)` (hex),
truncated to the CDM's identifier width when one is declared. The salt is read from
`secrets.d/<pipeline>.json` (key `id_salt` by default):

```json
{ "id_salt": "<a long random string>" }
```

If no salt is found, the op logs a warning and hashes **without** one.
Check the run log for that warning on your first run.

> [!CAUTION]
> Changing or losing the salt changes **every** hashed id. Linkage with
> earlier deliveries then breaks. Back up the salt with the same care as a
> private key.

## Escape hatch

`class: <dotted.path>` names a `BaseMapper` subclass, and the op list is
ignored. Use it only for logic the ops can't express. To add a new
*reusable* op instead, see [Extending Ovid2](../09-developers/extending.md#add-a-mapper-op).

## Tables without a spec

A table with no mapper spec falls back to the legacy family that
`mappers_type` names, if it has one. Otherwise the map stage passes the
table through unchanged.
