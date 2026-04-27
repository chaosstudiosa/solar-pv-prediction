# Solar PV Prediction (History Based)

A Home Assistant custom integration that forecasts PV power using your own
recorder history (no cloud, no model training). Creates three sensors and one
Number entity per instance:

- **PV Max** — raw hour-of-day max (current hour), from the last N days of
  long-term statistics.
- **PV Max Spline Smoothed** — Hermite spline over the 24 hourly points with
  configurable tension and ± time shift.
- **PV Max Trimmed** — smoothed value × trim factor.
- **Trim Factor** (number) — persists across restarts, resets at sunrise from
  your weather entity, auto-adjusts each minute with separate up/down rates
  and curtailment-aware gating.

Fully config-flow driven. See the README for setup and test instructions.
