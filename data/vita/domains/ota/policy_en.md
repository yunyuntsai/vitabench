# OTA domain policy (English) — routed agent, gated escalation (VitaBench adapter §1.4)

# Anti-leakage: this policy uses ONLY user-visible signals (the user's
# stated request + the live conversation + the Current time). It never references
# evaluation_criteria, expected_states, rubrics, or required_orders. Routing
# is per-domain (loaded for all OTA tasks via the "ota-procedure" section triggers);
# no within-domain section routing is used in this file.

## ota-procedure: complete every order the user requested

OTA tasks usually require several orders — a hotel booking (often multiple nights), attraction tickets, flight tickets, and/or train tickets, sometimes for multiple people or legs of a trip. Before you end the conversation:

1. Place EVERY order the user actually asked for. Do not stop after placing just one order when the user's request includes more — e.g. a hotel AND an attraction, a multi-night stay, round-trip tickets, or tickets for several people. Track which requested items are still unbooked and keep going until each one is placed.

2. For a multi-night hotel stay, call create_hotel_order once per night, selecting the room_id whose date matches each night. The room list returned by get_ota_hotel_info has one room_id per available date per room type — a single call books one room for one night only.

3. If a tool returns no available inventory for a date you computed, do not tell the user to "try again later" and stop. First re-check that date against the Current time above (is the year correct? is the day correct?), then try the correct date or a nearby alternative date that has inventory. Only stop when every requested order is placed, or the user explicitly says to cancel the remaining items.

Use the user's stated request and the Current time as your only sources for what to book and when; do not guess what the user wants.
