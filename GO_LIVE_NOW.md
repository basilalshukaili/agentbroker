> **⚠️ HISTORICAL — NOT CURRENT STATE (stamped 2026-08-17).**
> This file is a point-in-time snapshot kept for history. Claims about deploys,
> pricing, endpoints, and status here may be stale or contradicted by later work.
> The authoritative current picture is **README.md** (what the service does and
> what it costs) and **docs/PRICING.md** (every price, derived from
> `billing/pricing.py`). Both are kept current. `git log` is the full record.

# GO_LIVE_NOW — archived

This guide described the initial launch steps (push to GitHub, deploy to Fly.io, map domain).
All of those steps are done. The service is live.

**Current live URLs:**
- Edge (primary): `https://agent-broker-edge.basil-agent.workers.dev`
- Origin (internal): `https://smb-broker.onrender.com`

**What's next:** see [docs/NEXT_STEPS.md](./docs/NEXT_STEPS.md) for the current priorities
(updating registry submissions with the edge URL, submitting to 4 remaining aggregators,
monitoring `total_agents_requested`).
