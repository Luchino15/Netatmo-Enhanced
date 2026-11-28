# Changelog

## 0.5.0 — Experimental Smarther controls

- Recognizes heating Boost reported by Smarther, including a null target temperature and the device’s override expiry.
- Adds a 90-minute Boost preset and actions to select 30, 60 or 90 minutes, cycle the duration and stop Boost in either season. Confirmation uses the authenticated webhook or an API read.
- Adds an explicit-duration manual temperature action and a timestamp sensor for the confirmed override expiry. The standard temperature control remains available in both seasons.
- Adds experimental cooling Off and Auto commands to the room climate entity.
- Records recent control transitions and confirmation paths in the bounded diagnostics history.
- Documents the field test and Marketplace rollback to 0.4.0.

## 0.4.0 — Experimental Cloud Push

- Adds an opt-in mode that applies authenticated room, module and home changes directly to Home Assistant. The four webhook modes can be changed on the existing config entry.
- Recognizes home identifiers in the root payload and in `extra_params`. Tracks updates per field and rechecks ambiguous, conflicting or incomplete events before using their values.
- Preserves recent push state during stale API reads and keeps an independent 10-minute safety poll. Unknown schedules and topology changes use rate-limited reconciliation.
- Uses matching push confirmations for Home Assistant commands with API fallback, preserving the existing automatic schedule restoration flow.
- Adds bounded telemetry for direct updates, reconciliation, stale API results and local publication latency. Includes a field test and rollback guide.
- Retains existing entities, device identifiers, webhook registration, OAuth scopes and selected webhook mode on upgrade.

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
