# SatView Orbit Engine — Astrodynamics & SGP4 Propagation

## Overview

The `orbit` module retrieves, validates, caches, and propagates General Perturbations (GP) orbital element sets using the Simplified General Perturbations-4 (SGP4) mathematical model.

## Data Source & Attribution

- **Primary Source**: [CelesTrak](https://celestrak.org/)
- **Author / Curator**: Dr. T.S. Kelso
- **Endpoint**: `https://celestrak.org/NORAD/elements/gp.php`
- **Citation**: Orbital element sets provided courtesy of CelesTrak.

## Data Format

SatView uses the modern **CCSDS OMM (Orbit Mean-Elements Message) JSON format** instead of legacy two-line element (TLE) strings. This allows:
- Handling catalog numbers above 99,999 (TLEs are limited to 5 digits).
- High-precision epoch timestamps (ISO 8601 UTC).
- Native numeric typing for Mean Motion, Eccentricity, Inclination, RAAN, Argument of Pericenter, Mean Anomaly, and BSTAR drag terms.

---

## SGP4 Orbital Propagation (`OrbitPropagator`)

The propagation engine computes satellite ephemerides from validated OMM element sets using Skyfield and SGP4.

### Coordinate Systems & Reference Frames

* **Coordinate Frame**: **GCRS (Geocentric Celestial Reference System)**
  * An Earth-centered, non-rotating inertial reference frame aligned with the International Celestial Reference System (ICRS).
  * Converts from SGP4's native True Equator Mean Equinox (TEME) frame using IAU precession and nutation models in Skyfield.
* **Origin**: Center of mass of the Earth (Geocentric).

### Units

* **Position Vector**: Kilometres ($[X, Y, Z]$ in $\text{km}$).
* **Velocity Vector**: Kilometres per second ($[V_X, V_Y, V_Z]$ in $\text{km/s}$).
* **Time**: UTC timestamps in ISO 8601 representation (e.g. `2026-11-15T05:00:00+00:00`).

### Mathematical Model

* **Propagator**: SGP4 (for Low Earth Orbit / periods $< 225\text{ min}$) and SDP4 (Deep Space Perturbations for high/geostationary orbits).
* **Gravity Constant**: WGS-72 (standard for NORAD GP elements).
* **Atmospheric Drag**: Modeled via the $B^*$ drag term in SGP4.

---

## Caching & Rate Limits

- **CelesTrak Fair-Use Policy**: Request element sets at most once per update cycle (approximately every 2 hours).
- **Caching**: All requests are cached in `TTLCache` with a default TTL of 7,200 seconds (2 hours). Group queries automatically prime individual satellite cache entries to minimize external round trips.

---

## Known Limitations

1. **Current Orbits Only**: CelesTrak provides current and near-real-time element sets. Historical orbits for past dates require Space-Track (Phase 2).
2. **Catalog Scope**: Covers public catalog objects only. Classified or unlisted military satellites are omitted.
3. **Temporal Degradation**: SGP4 is an analytical theory whose accuracy degrades with elapsed time $\Delta t$ from the element set epoch. There is no universal accuracy window: Low Earth Orbit (LEO) satellites experience rapid along-track error accumulation (often several kilometres within 1–2 days or hours during elevated solar activity and atmospheric expansion), whereas high-altitude MEO and GEO satellites with negligible drag retain accuracy over longer periods. Furthermore, active station-keeping maneuvers are unmodeled and immediately invalidate pre-maneuver element sets.

4. **Observer Geometry**: Observer topocentric calculations (Azimuth, Elevation, Range) and pass predictions (rise/set/culmination) are implemented in Task 1.3.
