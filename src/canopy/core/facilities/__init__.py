"""Facility hierarchy — the ``core`` Postgres schema.

Facilities are a global, cross-module concept (an API key's ``facilities`` grant
names them directly), not any one module's private data — so, unlike the PV/tag
hierarchy Squirrel owns, the table lives here. Squirrel's ``pv_facility``/
``Snapshot.facility_id`` reference a facility by opaque id, resolved through
``canopy.core.facilities.gateway``, never a cross-schema foreign key.
"""
