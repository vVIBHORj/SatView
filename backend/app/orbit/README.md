# SatView Orbit Engine — CelesTrak Integration

## Overview

The `orbit` module retrieves, validates, and caches General Perturbations (GP) orbital element sets from public astrodynamics data providers.

## Data Source & Attribution

- **Primary Source**: [CelesTrak](https://celestrak.org/)
- **Author / Curator**: Dr. T.S. Kelso
- **Endpoint**: `https://celestrak.org/NORAD/elements/gp.php`
- **Citation**: Orbital element sets provided courtesy of CelesTrak.

## Data Format

SatView uses the modern **CCSDS OMM (Orbit Mean-Elements Message) JSON format** instead of legacy two-line element (TLE) strings. This allows:
- Handling catalog numbers above 99,999 (TLEs are limited to 5 digits).
- High precision epoch timestamps (ISO 8601 UTC).
- Native numeric typing for Mean Motion, Eccentricity, Inclination, RAAN, Argument of Pericenter, Mean Anomaly, and BSTAR drag terms.

### Key Fields

| Field | Type | Description |
| :--- | :--- | :--- |
| `OBJECT_NAME` | string | Common satellite name (e.g. `ISS (ZARYA)`) |
| `OBJECT_ID` | string | International COSPAR identifier (e.g. `1998-067A`) |
| `EPOCH` | string (ISO) | UTC epoch of the element set |
| `MEAN_MOTION` | float | Revolutions per day |
| `ECCENTRICITY` | float | Orbital eccentricity (0 for circular) |
| `INCLINATION` | float | Inclination relative to equator in degrees |
| `RA_OF_ASC_NODE` | float | Right Ascension of the Ascending Node (degrees) |
| `ARG_OF_PERICENTER` | float | Argument of Pericenter (degrees) |
| `MEAN_ANOMALY` | float | Mean Anomaly at epoch (degrees) |
| `NORAD_CAT_ID` | integer | NORAD Catalog identifier (e.g. `25544`) |
| `BSTAR` | float | SGP4 atmospheric drag coefficient |

## Caching & Rate Limits

- **CelesTrak Fair-Use Policy**: Request element sets at most once per update cycle (approximately every 2 hours).
- **Caching**: All requests are cached in `TTLCache` with a default TTL of 7,200 seconds (2 hours). Group queries automatically prime individual satellite cache entries to minimize external round trips.

## Known Limitations

1. **Current Orbits Only**: CelesTrak provides current and near-real-time element sets. Historical orbits for past dates require Space-Track (Phase 2).
2. **Catalog Scope**: Covers public catalog objects only. Classified or unlisted military satellites are omitted.
3. **Propagator Boundary**: This module provides the input element sets. SGP4 propagation and pass prediction are implemented in subsequent tasks.
