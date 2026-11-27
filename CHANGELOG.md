# Changelog

## 0.3.1 — Experimental diagnostics

- Adds a bounded, pseudonymized history of authenticated webhooks and timings from receipt to Home Assistant state publication.
- Separates actual Netatmo HTTP attempts from coordinator updates, grouped by origin, endpoint and method.
- Reconciles ambiguous successful registration responses with one delayed GET.
- Retains existing entities, controls, OAuth settings, polling and webhook refresh limits.

## 0.3.0 — Experimental

- Independent signed webhook v1 reception and optional rate-limited API refresh.
- Registration via GET and POST, no bulk deletion or replacement.
- Aggregated diagnostics, disabled by default.
- Retains 0.2.1 entities, OAuth scopes and tested controls.
- Live webhook delivery is not yet validated.
