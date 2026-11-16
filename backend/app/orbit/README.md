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

## Observer Geometry & Topocentric Coordinates (`ObserverService`)

The observer engine computes how a satellite appears to a ground observer at a specific geodetic position on Earth.

### Coordinate Systems & Reference Frames

* **Observer Location**: Referenced to the **WGS84 ellipsoidal model**:
  * **Latitude**: Geodetic latitude in degrees ($-90.0^\circ$ to $+90.0^\circ$, North positive).
  * **Longitude**: Geodetic longitude in degrees ($-180.0^\circ$ to $+180.0^\circ$, East positive).
  * **Elevation**: Height above WGS84 reference ellipsoid in metres ($\text{m}$).
* **Topocentric Horizon Frame**: Local Tangent Plane (East-North-Up / ENU):
  * **Azimuth**: Angle along the local horizon clockwise from True Geographic North ($0.0^\circ \le \text{Az} < 360.0^\circ$). $0^\circ = \text{North}$, $90^\circ = \text{East}$, $180^\circ = \text{South}$, $270^\circ = \text{West}$.
  * **Elevation**: Vertical angle above the local astronomical horizon ($-90.0^\circ \le \text{El} \le +90.0^\circ$).
  * **Geometric Elevation Convention**: Elevation returned is **pure geometric** (`temperature_C=None`), measuring true straight-line line-of-sight without variable atmospheric refraction distortion.
* **Observer-Relative Vectors**:
  * **Displacement Vector**: Observer-to-satellite relative position vector in GCRS ($\text{km}$).
  * **Relative Velocity Vector**: Relative velocity vector in GCRS ($\text{km/s}$), accounting for Earth's rotational velocity at the observer's latitude.
* **Slant Range**: Direct Euclidean distance from observer to satellite in kilometres ($\text{km}$).
* **Slant Range Rate**: Radial velocity in kilometres per second ($\text{km/s}$), calculated as $\dot{\rho} = \frac{\vec{\rho} \cdot \vec{v}_{\text{rel}}}{\rho}$. Negative indicates an approaching satellite; positive indicates a receding satellite.

---

## Satellite Pass Prediction (`PassPredictionService`)

The pass prediction engine identifies when and how a satellite traverses the sky above a specified ground station.

### Core Metrics Computed per Pass

* **AOS (Acquisition of Signal)**: Instant the satellite rises across the configurable minimum elevation threshold (`min_elevation_deg`).
* **LOS (Loss of Signal)**: Instant the satellite falls below the elevation threshold.
* **Maximum Elevation (Culmination)**: Peak geometric elevation reached during the pass, with its timestamp, azimuth, and slant range.
* **Closest Approach (TCA)**: Instant of minimum slant range between observer and satellite, with its timestamp, minimum range in kilometres, elevation, and azimuth.
  * *Astrodynamics Note*: In eccentric orbits, the instant of maximum angular elevation does **not** coincide with minimum Euclidean slant range (can differ by over 60 seconds). Both points are solved and reported independently.
* **Duration**: Total time in seconds between AOS and LOS.
* **Completeness & Continuity Flags**:
  * `is_complete`: `True` if both AOS and LOS occurred within the requested observation window; `False` if truncated by window boundaries.
  * `is_continuous`: `True` if the satellite remained continuously above the threshold without setting (e.g. Geostationary satellite overhead).

### Numerical Root-Finding & Optimization

* **Rise / Set Crossing Refinement**: Crossing times are solved using **Brent's method** ($f(t) = \text{el}(t) - h_{\text{min}} = 0$) down to a tolerance of $\Delta t \le 10^{-4}\text{ s}$ ($0.1\text{ ms}$), achieving crossing elevation accuracy of $|h(t) - h_{\text{min}}| < 10^{-4\circ}$ ($0.36\text{ arcsec}$).
* **Culmination & TCA Optimization**: Solved via **Golden Section search** to $\le 10^{-3}\text{ s}$ ($1\text{ ms}$).
* **Adaptive Scanning Grid**: Evaluated using vectorized Skyfield array operations: $\Delta t = 20\text{ s}$ for LEO ($P < 2\text{ h}$), and $\Delta t \le 60\text{ s}$ for higher orbits.
* **Grazing Pass Detection & Culmination-Anchored Bracketing**: Crests nearing the threshold ($\ge h_{\text{min}} - 2.5^\circ$) undergo local golden section extrema checks to capture short grazing passes without sampling gaps. For near-tangent passes where coarse samples remain below threshold, culmination is solved first and used as an above-threshold anchor to bracket AOS ($[t_{\text{sample}}, t_{\text{culm}}]$) and LOS ($[t_{\text{culm}}, t_{\text{sample}}]$) for Brent's method with guaranteed sign changes.

---

## Persistent Orbital Data Storage (`OrbitStorage`)

The storage engine persists validated orbital records into a local SQLite database, establishing a reliable offline cache and record repository that survives process restarts.

### Database Schema (`orbital_records`)

| Column Name | SQLite Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `norad_cat_id` | `INTEGER` | `PRIMARY KEY` | NORAD satellite catalog identifier (1 to 999,999,999) |
| `object_name` | `TEXT` | `NOT NULL` | Standard satellite name (e.g. `ISS (ZARYA)`) |
| `object_id` | `TEXT` | `NOT NULL` | International COSPAR designator (e.g. `1998-067A`) |
| `epoch` | `TEXT` | `NOT NULL` | UTC orbital epoch string in ISO 8601 format |
| `epoch_timestamp` | `REAL` | `NOT NULL` | Numeric Unix epoch timestamp (seconds) for atomic comparisons |
| `mean_motion` | `REAL` | `NOT NULL` | Mean motion in revolutions per day |
| `eccentricity` | `REAL` | `NOT NULL` | Orbital eccentricity ($0.0 \le e < 1.0$) |
| `inclination` | `REAL` | `NOT NULL` | Orbital inclination in degrees ($0.0^\circ \le i \le 180.0^\circ$) |
| `ra_of_asc_node` | `REAL` | `NOT NULL` | Right Ascension of Ascending Node in degrees |
| `arg_of_pericenter` | `REAL` | `NOT NULL` | Argument of Pericenter in degrees |
| `mean_anomaly` | `REAL` | `NOT NULL` | Mean Anomaly in degrees |
| `ephemeris_type` | `INTEGER` | `DEFAULT 0` | Ephemeris type code (0 = SGP4) |
| `classification_type` | `TEXT` | `DEFAULT 'U'` | Classification type (`U` = unclassified) |
| `element_set_no` | `INTEGER` | `NULLABLE` | NORAD element set sequence number |
| `rev_at_epoch` | `INTEGER` | `NULLABLE` | Revolution number at epoch |
| `bstar` | `REAL` | `DEFAULT 0.0` | SGP4 aerodynamic drag coefficient ($1 / \text{Earth radii}$) |
| `mean_motion_dot` | `REAL` | `DEFAULT 0.0` | First time derivative of mean motion |
| `mean_motion_ddot` | `REAL` | `DEFAULT 0.0` | Second time derivative of mean motion |
| `source` | `TEXT` | `NOT NULL DEFAULT 'celestrak'` | Provenance source of the element set |
| `retrieved_at` | `TEXT` | `NOT NULL` | UTC ISO 8601 timestamp of data ingestion |

### Indices
- `idx_orbital_records_epoch_ts`: Index on `epoch_timestamp` for temporal queries and sorting.
- `idx_orbital_records_object_name`: Index on `object_name` for fast text searches.

### Data Lifecycle & Stale Overwrite Protection
1. **Separation of Concerns**: The orbital element epoch (`epoch`), the data retrieval time (`retrieved_at`), and the upstream data provider (`source`) are strictly stored in separate fields.
2. **Atomic Upsert with Stale Protection**: Record ingestion uses SQLite's atomic upsert syntax:
   ```sql
   INSERT INTO orbital_records (...) VALUES (...)
   ON CONFLICT(norad_cat_id) DO UPDATE SET ...
   WHERE excluded.epoch_timestamp >= orbital_records.epoch_timestamp;
   ```
   - If an incoming record has a newer or identical epoch ($\text{epoch}_{\text{in}} \ge \text{epoch}_{\text{stored}}$), the stored row is updated.
   - If an incoming record is older ($\text{epoch}_{\text{in}} < \text{epoch}_{\text{stored}}$), the `WHERE` clause prevents overwrite, leaving the newer stored record intact and returning `False` (0 rows modified).
3. **Storage vs In-Memory Cache**:
   - `TTLCache`: Fast in-memory cache with a 2-hour TTL matching CelesTrak's publication cadence.
   - `OrbitStorage`: Persistent SQLite database holding the latest known orbital records across server restarts.
   - `fallback_to_storage`: `OrbitService.get_satellite_by_catnr(catnr, fallback_to_storage=True)` allows serving the stored record when upstream CelesTrak is unreachable or rate-limited.
4. **Error Isolation**:
   - Upstream HTTP failures raise `CelesTrakError`, `CelesTrakRateLimitError` (HTTP 429), or `CelesTrakTimeoutError` (HTTP 504).
   - Storage failures raise `DatabaseError` (HTTP 500). Database errors never mask upstream errors.
   - By default (`strict_storage=False`), database write failures log errors without interrupting successful live data responses. With `strict_storage=True`, database failures propagate immediately.

---

## Caching & Rate Limits

- **CelesTrak Fair-Use Policy**: Request element sets at most once per update cycle (approximately every 2 hours).
- **Caching**: All requests are cached in `TTLCache` with a default TTL of 7,200 seconds (2 hours). Group queries automatically prime individual satellite cache entries to minimize external round trips.

---

## Known Limitations

1. **Current Orbits Only**: CelesTrak provides current and near-real-time element sets. Historical orbits for past dates require Space-Track (Phase 2).
2. **Catalog Scope**: Covers public catalog objects only. Classified or unlisted military satellites are omitted.
3. **Temporal Degradation**: SGP4 is an analytical theory whose accuracy degrades with elapsed time $\Delta t$ from the element set epoch. There is no universal accuracy window: Low Earth Orbit (LEO) satellites experience rapid along-track error accumulation (often several kilometres within 1–2 days or hours during elevated solar activity and atmospheric expansion), whereas high-altitude MEO and GEO satellites with negligible drag retain accuracy over longer periods. Furthermore, active station-keeping maneuvers are unmodeled and immediately invalidate pre-maneuver element sets.
4. **Visual & Optical Pass Classification**: Satellite illumination conditions (sunlit passes, penumbra, Earth eclipse shadow entry/exit, observer twilight/night conditions) are deferred to Task 1.6.


