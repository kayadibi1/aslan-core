# Aslan Terminal Bitemporal Research API

The Bitemporal Research API is the read surface for **Moat 2** of the
Aslan Terminal platform: bitemporal point-in-time correctness across
every fact in the system. Given an `as_of` timestamp, the API answers
"what did the platform know at that wall-clock instant?" — not what it
has since been revised to. This is the property institutional research,
backtesting, and regulatory replay all need; Bloomberg's TR coverage
does not natively expose it. See
[ADR-003](../../decisions/ADR-003-bloomberg-bar.md) and
[SCOPE.md](./SCOPE.md) for the full design rationale.

---

## Authentication

Every authenticated endpoint expects an API key in the
`X-Aslan-Api-Key` header. The header value has the form
`<key_id>:<secret>` — both halves issued at key creation time, with the
secret stored only as an `argon2id` hash on the server.

Get a key by emailing `api@aslanterminal.com` (v1; self-service portal
deferred to v1.1). Each key carries a rate tier (`internal`, `partner`,
`public`) and an optional `pii_unredacted` scope; see SCOPE.md D5/D6/D30.

Public verification endpoints (`/v1/verify/moat-2`, `/v1/research/healthz`,
`/v1/research/version`) require no auth.

---

## Quickstart

All examples query the BIST close price for `GARAN` (Garanti BBVA, the
TR-listed bank ticker `BIST.GARAN`) as the platform knew it on
`2024-09-30T17:00:00Z` — the BIST market close on that date.

### curl

```bash
curl -sS https://api.aslanterminal.com/v1/research/observations \
  -H "X-Aslan-Api-Key: 9f3c2c4e-...:s3cr3t..." \
  --data-urlencode "series_id=BIST.GARAN.close" \
  --data-urlencode "as_of=2024-09-30T17:00:00Z" \
  -G | jq .
```

### Python — stdlib only (`urllib`)

```python
import json
import urllib.parse
import urllib.request

key_id = "9f3c2c4e-1a8b-4f0c-9d11-aa55cc77ee99"
secret = "s3cr3t..."  # never log this

params = urllib.parse.urlencode({
    "series_id": "BIST.GARAN.close",
    "as_of": "2024-09-30T17:00:00Z",
})
req = urllib.request.Request(
    f"https://api.aslanterminal.com/v1/research/observations?{params}",
    headers={"X-Aslan-Api-Key": f"{key_id}:{secret}"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    body = json.loads(resp.read())

print(body["data"][0]["value"])               # the close price as of 2024-09-30 17:00 UTC
print(body["metadata"]["as_of_resolved"])     # what the server resolved as_of to
```

### Python — `aslan_research_sdk`

```python
from datetime import datetime, timezone
from aslan_research_sdk import Client

client = Client(api_key="9f3c2c4e-...:s3cr3t...")

snapshot = client.observations.at(
    series_id="BIST.GARAN.close",
    as_of=datetime(2024, 9, 30, 17, 0, tzinfo=timezone.utc),
)
print(snapshot.data[0].value)
print(snapshot.metadata.as_of_resolved)
```

---

## Core concept — point-in-time semantics

Consider a real KAP-amendment scenario. On `2024-03-08` Garanti BBVA
files an `ozel_durum` (special-case material disclosure) with
disclosure id `KAP-2024-1018871` titled
**"Yönetim Kurulu Üyesi Atama Kararı Hk."** ("Re: Board of Directors
Member Appointment Resolution"). Three days later, on `2024-03-11`,
the issuer republishes a corrected version of the same disclosure.

The API records both states; nothing is overwritten:

```bash
# What the platform knew on 2024-03-09 (between filings):
curl -sS .../disclosures/KAP-2024-1018871?as_of=2024-03-09T12:00:00Z \
  -H "X-Aslan-Api-Key: ..." | jq '.data.title, .metadata.as_of_resolved'
# "Yönetim Kurulu Üyesi Atama Kararı Hk."
# "2024-03-08T15:42:11.043217Z"

# What the platform knew after the amendment:
curl -sS .../disclosures/KAP-2024-1018871?as_of=2024-03-12T12:00:00Z \
  -H "X-Aslan-Api-Key: ..." | jq '.data.title, .metadata.as_of_resolved'
# "Yönetim Kurulu Üyesi Atama Kararı Hk. (Düzeltme)"
# "2024-03-11T09:13:55.207844Z"
```

Same `disclosure_id`, two `as_of` answers, two truths. A backtest
running with `as_of=2024-03-09` cannot peek at the corrected text — by
design. This is the Moat 2 contract; the canary at
[/v1/verify/moat-2](#) verifies it continuously against
≥10 hand-curated amendment cases.

Turkish text is preserved verbatim (per ADR-002); we never auto-translate.

---

## Response envelope

Every successful response wraps payload data in a stable envelope:

```json
{
  "data": { "...endpoint-specific..." },
  "metadata": {
    "as_of_requested": "2024-09-30T17:00:00Z",
    "as_of_resolved":  "2024-09-30T17:00:00Z",
    "as_of_range":     null,
    "lineage": {
      "source_filing_id":         "KAP-2024-1018871",
      "raw_bytes_sha256":         "9b2e...c4f1",
      "extraction_code_version":  "1.4.2",
      "prompt_version":           "kap-bod-appointment.v3",
      "model_identity":           "claude-opus-4-7",
      "extraction_seed":          42
    },
    "pagination":          { "next_cursor": null, "has_more": false, "total_count_estimate": 1 },
    "feature_flags_active": ["BITEMPORAL_API_ENABLED"],
    "warnings":             [],
    "request_id":           "0c41a4c5-9c7b-4f2a-8b31-d3a5e9f2c1aa",
    "served_by":            "bitemporal-api/8a3c4f1"
  }
}
```

Lineage fields populate where the underlying row carries them
(`agg.filing_event` always; `ts.observation` rarely). See
[SCOPE.md D15](./SCOPE.md#d15--response-envelope).

---

## Rate limits

Three tiers, enforced via Postgres-backed token bucket. Per-key
overrides live in `aslan_core.api_key.rate_overrides`.

| Tier | Per-minute | Per-hour | Per-day | Audience |
|---|---|---|---|---|
| `internal` | 10 000 | 100 000 | unlimited | Aslan internal services / canary |
| `partner` | 600 | 5 000 | 30 000 | Trusted external customers |
| `public` | 60 | 500 | 2 000 | Unauthenticated public endpoints |

Throttled requests return `429 RATE_LIMITED` with a `Retry-After`
header. See [SCOPE.md D6](./SCOPE.md#d6--rate-limit-tiers-and-quotas).

---

## Error codes

Errors follow RFC 7807 `application/problem+json` with a stable
machine-readable `code` field. Most common codes:

| Code | HTTP | Meaning |
|---|---|---|
| `BITEMPORAL_AS_OF_NAIVE` | 400 | `as_of` missing timezone |
| `BITEMPORAL_AS_OF_FUTURE` | 400 | `as_of > NOW() + 60s` |
| `BITEMPORAL_PRE_BITEMPORAL_REGION` | 404 | No bitemporal data at requested `as_of` |
| `IDENTIFIER_AMBIGUOUS` | 409 | Multiple `ref.identifier` rows match |
| `RATE_LIMITED` | 429 | Bucket exhausted; check `Retry-After` |
| `AUTH_INVALID` | 401 | Bad / expired / missing API key |
| `AUTH_FORBIDDEN` | 403 | Key lacks scope (e.g. `pii_unredacted`) |
| `QUERY_TOO_LARGE` | 413 | Exceeds D19 cost cap |
| `FEATURE_DISABLED` | 503 | Master flag off or required feature flag false |

Full list and `extensions` schemas in
[OPENAPI.yaml](./OPENAPI.yaml#components/responses).

---

## OpenAPI / Swagger / ReDoc

- **OpenAPI 3.1 spec:** `https://api.aslanterminal.com/v1/research/openapi.json`
- **Swagger UI:** `https://api.aslanterminal.com/v1/research/docs`
- **ReDoc:** `https://api.aslanterminal.com/v1/research/redoc`
- **Source-of-truth file:** [`OPENAPI.yaml`](./OPENAPI.yaml) in this repo
