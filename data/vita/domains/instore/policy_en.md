# Instore domain policy (English) — routed sections

This file is loaded only when `VITA_POLICY_MODE=routed` (gated architecture
escalation, VitaBench adapter §1.4). Sections are routed per-task on
**user-visible intent only** (the user's instructions / persona), never on
`evaluation_criteria` / rubrics / `required_orders`. When no section matches,
the loader falls back to ALL sections (safe degrade). When `VITA_POLICY_MODE`
is unset, this file is not loaded at all and the agent gets the pristine
generic `agent_system_prompt` (the benchmark-comparable path).

Each section is prepended to the generic agent instruction for tasks whose
user-facing text matches the section's trigger keywords (keyword map lives in
`get_agent_policy`). Sections are independent — edit one without perturbing
the tasks that don't trigger it.

## date-time

The "Current time" given in your instructions is the authoritative present
time for this conversation. Treat it as TODAY. Compute every relative date or
time the user mentions (today, tomorrow, tonight, this weekend, next week, the
day after, on a named holiday, etc.) from this Current time — never from any
other date you may know. Before ordering or reserving, if the user references
weather or a condition that depends on a date, call the weather tool for the
correct date derived from the Current time, and branch on its result. Pass the
resulting absolute date/time (format %Y-%m-%d %H:%M:%S) to reservation / booking
/ order tools.

## instore-procedure

1. **Select the shop that EXACTLY matches the user's stated criterion** — bestseller/top-selling tag, rating ≥ the stated threshold, the exact brand/branch name, or the minimal-distance shop (verify distance via `address_to_longitude_latitude` + `longitude_latitude_to_distance` before recommending). Do not pick a loosely-matching alternative, and do not let user agreement to a looser match discharge the criterion.

2. **Honor stated budget/price limits STRICTLY** — before any `create_instore_product_order`, enumerate the `instore_product_search_recommend` results and pick a product whose price (and whose order total) satisfies EVERY stated limit. Do NOT place an order exceeding a stated limit, even if the user agrees to the higher price. If nothing satisfies the limit, tell the user and do not order out-of-budget.

A seat reservation (`instore_book`) or service reservation (`instore_reservation`) still does NOT purchase a product — use `create_instore_product_order` to buy any meal/package/voucher.
