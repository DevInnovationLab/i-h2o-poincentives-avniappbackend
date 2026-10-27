# 12-avni

Data source: **Avni**, the open-source field data collection platform the study
uses to run the pump-operator app.

| | |
|---|---|
| Platform | Avni (`app.avniproject.org`) |
| Organisation | `po_uat` |
| Access | REST API, Cognito-authenticated (`auth-token` header, an **id** token) |
| Grain pulled | one *subject* per pump, plus its encounters |
| Mirrors | the project's Box folder of the same name |

## What a "pump" is here

A **Pump** is an Avni *subject*: one physical village water pump fitted with an
in-line chlorination device, with an address hierarchy attached (the
administrative location the pump sits in) and a set of observations recorded
when it was registered.

Everything that happens *to* a pump afterwards is an **encounter** — a visit
logged by a pump operator through the app. The study's three encounter forms
are the self-report survey (the routine visit: chlorine reading, cartridge
state, runtime), the grievance form, and the payment rate form. Operators are
not a first-class entity in Avni; they are derived from the "last modified by"
audit field on encounters. "Created by" is not the submitter: Avni creates each
scheduled survey when the previous one is submitted, or when the pump is
registered.

Some subjects in the instance are test records created during setup rather
than real pumps. They are pulled and kept — the pipeline never silently drops
rows — and filtering them is an analysis decision, not a pipeline one.

## Layout

```
120-raw/     append-only snapshots of what the API returned
121-clean/   the derived tables, the report and the localised photos
123-reviews/ reviewer decisions on submissions (human input, append-only)
```

Each has its own sidecar `.md`.

## Where to read more

- `4-documentation/40-data-sources/avni-api.md` — how the API behaves,
  pagination and versioning, authentication, and what the error codes mean.
- `4-documentation/41-data-dictionaries/` — what every column in every output
  table contains.

## Privacy

Everything under this folder is gitignored except the directory structure,
these `.md` sidecars and `.gitkeep` markers. The repository is public; the
data is not. See `AGENTS.md > Data privacy`.
