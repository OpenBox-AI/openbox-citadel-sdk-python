"""Legacy-named semantic attributes on outgoing spans, so the platform can read them.

The base SDK sets OTel's CURRENT semantic conventions on an HTTP span —
``url.full`` and ``http.request.method`` — and separately lifts them, plus the
status code, into flat wire fields (``http_url``, ``http_method``,
``http_status_code``). That mapping runs one way only: attributes in, root
fields out (``openbox_core.contracts.otel_spans._SEMANTIC_ATTR_MAP``).

Core stores a span's ``attributes`` JSONB and has no columns for the flat HTTP
fields, so everything downstream of storage sees the new-convention keys only.
Everything downstream of storage reads the LEGACY ones:

* ``workflow-tree-view.tsx`` builds the request link from ``attrs["http.url"]``
  and the status badge from ``attrs["http.response.status_code"]`` or
  ``attrs["http.status_code"]`` — for ``llm_completion`` spans too, not just
  ``http_*`` ones.
* Core's own ``spanToSpanData`` extracts identity for fingerprinting from
  ``attrs["http.url"]`` and ``attrs["http.method"]``.
* Core's sandbox path copies its typed status into
  ``attrs["http.response.status_code"]`` and says why in a comment: "the FE's
  HTTP span reads http.response.status_code for the status badge".

The status code never reaches attributes at all, under either convention: it is
known only at completion, and the base SDK puts it in the flat field.

So the net effect is a span that renders with no link and no status, and a
fingerprint with no identity. This module copies the flat fields back into
attributes under the legacy keys, additively — the new keys stay exactly as the
base SDK set them, and any value already present is left alone.

Installed by `OpenBoxCitadelMiddleware.setup_instrumentation`, alongside
`file_spans.install_file_span_corrections`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("openbox_citadel.span_aliases")

_installed = False

ALIASES: dict[str, tuple[str, ...]] = {
    "http_url": ("http.url",),
    "http_method": ("http.method",),
    # Both spellings: the tree view tries `http.response.status_code` first and
    # falls back to `http.status_code`, while core's sandbox parity path writes
    # the former. Writing both costs two keys and removes the guesswork.
    "http_status_code": ("http.response.status_code", "http.status_code"),
    # Same shape of bug for the other hook families. Core's `spanToSpanData`
    # reads `db.system`, `db.name` and `db.statement`; without them a Redis SET
    # is stored indistinguishably from a SQL query, which is the "stored DB
    # spans do not name their backend" gap the demo README records.
    "db_system": ("db.system",),
    "db_name": ("db.name",),
    "db_operation": ("db.operation",),
    "db_statement": ("db.statement",),
    "server_address": ("net.peer.name",),
    "server_port": ("net.peer.port",),
    # File keys are the same under both conventions, so these are only ever
    # filled when the base SDK left them in the flat field alone.
    "file_path": ("file.path",),
    "file_mode": ("file.mode",),
    "file_operation": ("file.operation",),
}
"""Flat wire field -> the legacy attribute keys readers look for."""


def add_legacy_attributes(span: dict[str, Any]) -> dict[str, Any]:
    """Mirror a span's flat semantic fields into attributes under legacy keys.

    Additive and idempotent: an existing attribute is never overwritten, so a
    span that already carries `http.url` (a future base-SDK version, say) is
    left untouched.
    """
    attributes = span.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    added = False
    for field_name, attr_keys in ALIASES.items():
        value = span.get(field_name)
        if value is None:
            continue
        for key in attr_keys:
            if attributes.get(key) is None:
                attributes[key] = value
                added = True

    if added:
        span["attributes"] = attributes
    return span


def install_span_attribute_aliases() -> bool:
    """Wrap the wire normalizer so every span carries the legacy keys.

    Patched at the normalizer's point of USE rather than its definition: the
    payload builder holds its own module-level reference
    (`from .core_span import to_core_span_data`), so rebinding the definition in
    `core_span` alone would leave the live caller pointing at the original.
    """
    global _installed
    if _installed:
        return True
    try:
        from openbox_core.wire import evaluate_payload
    except Exception:  # noqa: BLE001 — base SDK absent or restructured
        logger.info(
            "span attribute aliases not installed: openbox_core.wire.evaluate_payload is "
            "unavailable, so spans will carry only current-convention attribute keys "
            "and render without a link or status."
        )
        return False

    original = getattr(evaluate_payload, "to_core_span_data", None)
    if original is None:
        logger.info("span attribute aliases not installed: no to_core_span_data to wrap")
        return False
    if getattr(original, "_openbox_http_aliased", False):
        _installed = True
        return True

    def aliased(span: dict[str, Any], **kwargs: Any) -> Any:
        wire_span, diagnostics = original(span, **kwargs)
        try:
            add_legacy_attributes(wire_span)
        except Exception:  # noqa: BLE001 — telemetry must not fail the call
            logger.debug("attribute aliasing failed; span sent as built", exc_info=True)
        return wire_span, diagnostics

    aliased._openbox_http_aliased = True  # type: ignore[attr-defined]
    evaluate_payload.to_core_span_data = aliased  # type: ignore[assignment]
    _installed = True
    logger.info("span attribute aliases installed (%d flat fields mirrored)", len(ALIASES))
    return True
