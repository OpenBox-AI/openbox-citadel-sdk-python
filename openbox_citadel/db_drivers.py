"""Instrument the DB-API drivers, so the governance patch above them has work.

The base SDK's ``install_dbapi`` patches
``opentelemetry.instrumentation.dbapi.CursorTracer.traced_execution`` — the
governance seam. A ``CursorTracer`` only ever exists if a DRIVER instrumentor
built one, and the base SDK installs none: ``install_redis`` calls
``RedisInstrumentor().instrument()`` and ``install_asyncpg`` patches
``Connection._execute`` directly, but the DB-API family (sqlite3, psycopg2,
mysql, pymysql) has no equivalent step anywhere.

The result is an instrumentation manager that reports ``dbapi`` among its
installed targets while no SQL statement produces a span at all. The legacy
``openbox_langgraph.otel_setup.setup_database_instrumentation`` this SDK used to
call did instrument the drivers; when that entry point became an error shim,
that half went missing with it.

Each driver is optional and instrumented independently: an absent package is
skipped, and one failure never stops the rest. Instrumenting is idempotent —
the OTel instrumentors refuse a second call and say so.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("openbox_citadel.db_drivers")

_installed = False

DRIVERS: tuple[tuple[str, str, str], ...] = (
    ("sqlite3", "opentelemetry.instrumentation.sqlite3", "SQLite3Instrumentor"),
    ("psycopg2", "opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor"),
    ("pymysql", "opentelemetry.instrumentation.pymysql", "PyMySQLInstrumentor"),
    ("mysql", "opentelemetry.instrumentation.mysql", "MySQLInstrumentor"),
)
"""(label, module, instrumentor class) for every DB-API driver worth trying."""


def install_db_driver_instrumentation() -> list[str]:
    """Instrument every available DB-API driver. Returns the labels that took.

    Safe to call after the manager's own install: the governance patch is on the
    ``CursorTracer`` CLASS and is resolved per call, so a driver instrumented
    afterwards still runs through it.
    """
    global _installed
    if _installed:
        return []

    installed: list[str] = []
    for label, module_name, class_name in DRIVERS:
        try:
            module = __import__(module_name, fromlist=[class_name])
        except Exception:  # noqa: BLE001 — driver or its instrumentor absent
            logger.debug("%s instrumentation unavailable", label)
            continue
        try:
            getattr(module, class_name)().instrument()
            installed.append(label)
        except Exception:  # noqa: BLE001 — already instrumented, or refused
            logger.debug("%s instrumentor declined", label, exc_info=True)

    _installed = True
    if installed:
        logger.info("db drivers instrumented: %s", ", ".join(installed))
    else:
        logger.info(
            "no DB-API drivers instrumented; SQL through sqlite3/psycopg2/mysql "
            "will produce no spans even though the dbapi governance patch is in place"
        )
    return installed
