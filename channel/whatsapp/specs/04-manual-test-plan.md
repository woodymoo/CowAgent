# WhatsApp Channel — Manual Test Plan

English | [中文](zh/04-manual-test-plan.md)

Goal: verify the channel end to end against the **real** Meta Cloud API, complementing the automated unit and integration tests (`tests/test_whatsapp_*.py`, which run offline against a fake Graph API).

Setup instructions live in the user manual: `docs/channels/whatsapp.mdx`. Each test lists **steps → expected result → evidence** (what to see on the phone and/or in the CowAgent log). Record Pass/Fail in the last column of the summary table (§9).

## 0. Test environment

| Item | Value |
|---|---|
| Meta app | Created per manual §3 |
| Number | Phase A: Meta **test number**. Phase B (optional): your registered number. Phase C (groups): an **OBA** number |
| Tester phones | **Phone 1** = your WhatsApp (added as a test recipient). **Phone 2** = a second WhatsApp account (for groups and multi-user tests) |
| Tunnel | `cloudflared tunnel --url http://localhost:9893` (or ngrok) |
| CowAgent | Branch `feat/whatsapp-channel`, `channel_type: "whatsapp"`, `debug: true` (for status logs) |
| Log | Run in a terminal or follow the log file; grep `[WhatsApp]` |

For the security tests, export these helpers:

```bash
export WA_URL="https://<tunnel-host>/whatsapp/webhook"
export WA_SECRET="<whatsapp_app_secret>"
sign() { printf '%s' "$1" | openssl dgst -sha256 -hmac "$WA_SECRET" | sed 's/^.* //'; }
```

---

## 1. Startup and configuration

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| S1 | Start with all 4 required keys set | Starts | Log: `✅ webhook listening on http://0.0.0.0:9893/whatsapp/webhook (phone_number_id=…)` |
| S2 | Remove `whatsapp_app_secret`, start | Startup fails clearly; other channels unaffected | Log: `missing required config: whatsapp_app_secret` |
| S3 | Occupy the port (`python3 -m http.server 9893`), start | Startup fails (not a false "ready") | Log: `startup failed: … address already in use` |
| S4 | Web Console → Channels → Add WhatsApp, enter the credentials | Channel card shows connected; secrets are masked after save | Console UI + S1 log line |
| S5 | Put secrets only in env vars (`WHATSAPP_ACCESS_TOKEN`, …), none in config.json | Starts as in S1 | Log: `override config by environ args` (values masked) |

## 2. Webhook verification and security

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| W1 | Meta → Configuration → Webhook → Verify and save | Meta accepts | Meta UI shows a green check |
| W2 | `curl "$WA_URL?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=42"` | Body `42`, HTTP 200 | curl output |
| W3 | Same as W2 with a wrong token | HTTP 403 | `curl -i` + log `webhook verification refused` |
| W4 | Unsigned POST: `curl -i -X POST "$WA_URL" -d '{"object":"whatsapp_business_account","entry":[]}'` | HTTP 403, nothing processed | Log: `bad or missing X-Hub-Signature-256` |
| W5 | Wrongly signed POST: add `-H "X-Hub-Signature-256: sha256=00"` | HTTP 403 | as W4 |
| W6 | Correctly signed but non-WhatsApp body: `B='{"object":"page"}'; curl -i -X POST "$WA_URL" -H "X-Hub-Signature-256: sha256=$(sign "$B")" -d "$B"` | HTTP 200 (so Meta won't retry), ignored | Log: `signed webhook payload rejected` |
| W7 | Oversized body: `head -c 5000000 /dev/zero \| curl -i -X POST "$WA_URL" --data-binary @-` | HTTP 413, no crash | curl output |
| W8 | Browse to `https://<tunnel-host>/static/` or `/` | 404, no file listing, no traceback page | Browser |

## 3. Private chat: inbound (Phone 1 → bot)

Precondition: Phone 1 has messaged the number in the last 24 h (reply to the hello_world template first).

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| P1 | Send `hello` | Agent answers in the same chat | Phone 1; log `sent 1 part(s) to individual <wa_id>` |
| P2 | Ask for something long ("write a 1500-word essay about tea") | Answer arrives as several messages, in order, nothing lost | Phone 1; log `sent N part(s)` with N>1 |
| P3 | Send a photo **with caption** "what is in this picture?" | One answer describing the photo | Phone 1 |
| P4 | Send a photo **without caption**, then text "describe it" | No reply to the photo itself; the text gets an answer about the photo | Log `image cached for session …`, then the answer |
| P5 | Send a PDF with caption "summarize" | Summary of the PDF | Phone 1 |
| P6 | Send a voice note "what time is it" (STT provider configured) | Spoken question answered | Phone 1 |
| P7 | Send a sticker, then a location | No reply, no error | Log (debug): `unsupported message type 'sticker' skipped` |
| P8 | Send `/cancel` while a long task runs | Task stops; confirmation message | Phone 1 |
| P9 | Phone 1 and Phone 2 (both test recipients) chat in parallel | Each gets their own answers; context is not mixed | Both phones |
| P10 | Restart CowAgent between two messages | Second message is still answered, after restart | Phone 1 |

## 4. Private chat: outbound media (bot → Phone 1)

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| O1 | Ask the Agent to create and send an image (or send a local PNG/JPG) | Image arrives as a photo | Phone 1 |
| O2 | Ask the Agent to write a markdown report and send it as a file | `.md` arrives as a document with its filename | Phone 1 |
| O3 | Ask for a PDF or Excel file | Arrives as a document | Phone 1 |
| O4 | Ask it to send a GIF or ZIP file | Not sent; you receive "WhatsApp can't deliver this file type: <name>" | Phone 1; log warning `is a type WhatsApp does not accept` |
| O5 | Enable `always_reply_voice: true`, ask a question | Voice/audio reply (mp3/ogg TTS), or the notice if TTS produces WAV | Phone 1 |
| O6 | Ask it to send an image from a public URL | Image arrives | Phone 1 |
| O7 | Start CowAgent with `WEB_SECURITY_SSRF_PROTECTION=true` (the project-wide guard is **off by default**), then ask it to send `http://127.0.0.1:…` or `http://169.254.169.254/…` as an image | Nothing fetched or sent; error logged; no crash | Log `cannot prepare … reply: ValueError` |

## 5. Rate limiting and anti-ban

For visibility, temporarily set `whatsapp_pair_burst: 2`, `whatsapp_pair_interval_seconds: 10`.

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| R1 | Ask for a long answer that splits into ≥4 parts (as P2) | First 2 parts arrive at once; later parts arrive about 10 s apart. None lost | Phone timestamps |
| R2 | During R1, Phone 2 sends a message | Phone 2 is answered without waiting for Phone 1's pacing (separate buckets) | Phone 2 timestamp |
| R3 | Set `whatsapp_max_wait_seconds: 5`, repeat R1 | Parts that would wait > 5 s are abandoned instead of blocking | Log `abandoned after N part(s)` |
| R4 | Over the test day, check WhatsApp Manager → quality rating | Stays green; no `131048` in the log | WhatsApp Manager, `grep 131048` |

Restore the defaults afterwards.

## 6. 24-hour window

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| T1 | Ask the Agent to schedule a message to Phone 1 in 2 minutes, while inside the window | Message arrives at the scheduled time | Phone 1 |
| T2 | Schedule a message to a recipient who has **not** written in the last 24 h (or wait > 24 h) | Not delivered; clear log explanation; not retried | Log `24-hour customer service window is closed (131047)`, exactly once |

## 7. Groups (Phase C: requires an Official Business Account)

Subscribe the `group_*` webhook fields first (manual §6.2).

| ID | Steps | Expected | Evidence |
|---|---|---|---|
| G1 | On a **non-OBA** number: "create a WhatsApp group called Test" | Agent reports Meta's eligibility error (no crash) | Agent reply quoting the error |
| G2 | On an OBA number: "create a WhatsApp group called Launch crew, approval required" | Group created | Agent reply; log `group event group_lifecycle_update: groups=['…']` (no invite link in the log) |
| G3 | "List my WhatsApp groups" | Shows Launch crew with its id | Agent reply |
| G4 | "Give me the invite link for Launch crew" | `https://chat.whatsapp.com/…` link | Agent reply |
| G5 | Phone 2 opens the link and requests to join; "who wants to join?" then "approve them" | Join request listed, then approved; Phone 2 is in the group | Agent replies; Phone 2 |
| G6 | Phone 2 writes in the group: "hi bot" | Agent answers **in the group**, without a literal "@name" prefix | Group chat; log `sent … to group <id>` |
| G7 | Set `whatsapp_group_trigger: "prefix"`; Phone 2 writes "hello" then "@bot hello" | Only the second is answered | Group chat |
| G8 | "Rename the group to Launch team and set the description to …" | Name and description change on the phones | Phones |
| G9 | "Reset the invite link"; try the old link from a third phone | Old link no longer works | Phone 3 |
| G10 | "Remove <Phone 2 number> from the group" | Phone 2 removed (and cannot rejoin via link) | Phone 2 |
| G11 | Ask the Agent to delete group id `me`, or your WABA id | Refused ("not a WhatsApp group"); nothing deleted | Agent reply |
| G12 | "Delete the group Launch team" | Group deleted | Phones; log group event |
| G13 | Stop the WhatsApp channel and ask the Agent (via Web) to list WhatsApp groups | Agent has no WhatsApp group tool / says the channel isn't running | Agent reply |

## 8. Regression: other channels

| ID | Steps | Expected |
|---|---|---|
| X1 | Run `channel_type: "web,whatsapp"`; chat on Web | Web works as before (WhatsApp does not change global trigger config) |
| X2 | Run another IM channel (e.g. Telegram) alongside WhatsApp | Both answer independently |

## 9. Results

| Area | IDs | Pass/Fail | Notes |
|---|---|---|---|
| Startup | S1–S5 | | |
| Webhook security | W1–W8 | | |
| Private inbound | P1–P10 | | |
| Outbound media | O1–O7 | | |
| Rate limiting | R1–R4 | | |
| 24 h window | T1–T2 | | |
| Groups | G1–G13 | | |
| Regression | X1–X2 | | |

**Exit criteria**: all S, W, P, O, R, T and X tests pass on the test number. The G tests pass on an OBA number, or G1 passes and G2–G13 are marked N/A when no OBA number is available.
