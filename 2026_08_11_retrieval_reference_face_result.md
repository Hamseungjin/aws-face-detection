# Transaction-Bound Reference Face Retrieval — Result Report

**Date:** 2026-08-11  
**Branch:** `devhsj`  
**Commit:** none (explicitly not committed)

---

## 1. Summary

Implemented server-enforced retrieval:

`retrieval_code` → one STORED transaction → transaction reference face → exact current capture FRAME-B → Rekognition CompareFaces ≥ 90 → locker open.

| Item | Result |
|---|---|
| Code implementation | **Done** |
| SQLite migration | **Done** (non-destructive ALTER) |
| STORE trusted face bind | **Done** (`/api/kiosk/store/face-verify` + session) |
| Durable S3 reference | **Done** (`locker-references/<tx>/reference.jpg`) |
| RETRIEVE face gate | **Done** (`retrieve/start` requires code + targetFrameId) |
| RETRIEVE UX (no ID / no locker typing) | **Done** |
| facecompare reference S3 mode | **Done** (legacy mode preserved) |
| Unit / integration tests | **108 passed** |
| Live positive/negative E2E | **Blocked** — data stack resources absent |
| CloudFormation change | **None** (lifecycle already prefix-scoped) |
| Git commit | **None** |

---

## 2. Files changed (this feature)

| Path | Change |
|---|---|
| `web-ui/backend/kiosk_store.py` | Schema migration; reference fields; lookup/cleanup helpers |
| `web-ui/backend/config.py` | Frame bucket, facecompare, threshold, cleanup config |
| `web-ui/backend/reference_face.py` | **New** — S3 copy/delete, Lambda facecompare invoke |
| `web-ui/backend/app.py` | face-verify, trusted complete, face-gated retrieve, cleanup |
| `web-ui/backend/.env.example` | New env vars documented |
| `lambda/facecompare/facecompare.py` | Optional `referenceS3Bucket`/`referenceS3Key` mode |
| `web-ui/src/kiosk.js` | STORE via face-verify; RETRIEVE code→face→open |
| `web-ui/kiosk.html` | RETRIEVE copy / progress wording |
| `tests/test_kiosk_store.py` | Migration + reference store tests |
| `tests/test_app_kiosk.py` | Full API face-gate matrix |
| `tests/test_facecompare_correlation.py` | Reference S3 mode + legacy bytes mode |
| `tests/kiosk_correlation_test.js` | store/face-verify path |
| `docs/KIOSK_UI.md` | New STORE/RETRIEVE architecture |
| `2026_08_10_grok_report_01.md` | Section append |
| `2026_08_11_retrieval_reference_face_result.md` | This report |

---

## 3. SQLite migration

On `KioskStore.initialize()`:

1. `CREATE TABLE IF NOT EXISTS` includes new columns for fresh DBs.
2. `_migrate_transaction_columns()` runs `PRAGMA table_info` and
   `ALTER TABLE transactions ADD COLUMN ...` for any missing:
   - `reference_frame_id TEXT`
   - `reference_face_s3_key TEXT`
   - `reference_face_created_at TEXT`
   - `reference_face_deleted_at TEXT`
3. Ensures index `idx_transactions_retrieval_code_status`.
4. Never drops tables or rewrites existing rows.

---

## 4. Final transaction schema fields (relevant)

| Field | Role |
|---|---|
| `transaction_id` | Internal UUID |
| `retrieval_code` | 8-digit lookup (UNIQUE) |
| `locker_id` | Resource to open after auth |
| `status` | RESERVED/STORED/RETRIEVING/RETRIEVED/… |
| `reference_frame_id` | FRAME-A correlation |
| `reference_face_s3_key` | Durable compare source |
| `reference_face_created_at` / `reference_face_deleted_at` | Lifecycle audit |

Not stored: embeddings, vectors, image BLOBs, ID image, base64.

---

## 5. How verified STORE frame is bound

1. Kiosk posts ID image + `targetFrameId=FRAME-A` to **FastAPI**
   `POST /api/kiosk/store/face-verify` (not client-chosen reference at complete time).
2. FastAPI invokes facecompare Lambda (ID bytes vs exact frame).
3. On `matched` + similarity ≥ 90: session keys  
   `verified_store_frame_id`, `verified_store_at` (no image data in cookie).
4. `store/complete` reads only that session frame; resolves DynamoDB/S3; copies
   reference; writes SQLite. Arbitrary client frame selection is rejected.

---

## 6. Durable beyond general frame TTL?

**Yes.** Bucket lifecycle (CFN) expires **`frames/`** and **`logging/`** only.  
`locker-references/` is outside those prefixes, so general frame expiry does not
remove transaction references. Application deletes them after RETRIEVED
(demo: immediate).

**No new bucket. No CFN deploy in this task.**

---

## 7. S3 reference strategy

| Item | Value |
|---|---|
| Bucket | `FRAME_S3_BUCKET` (same frames bucket) |
| Key | `locker-references/<transaction_id>/reference.jpg` |
| Idempotency | `head_object` then `copy_object`; reuse if exists |
| Public access | None (private bucket; keys never returned to browser) |

---

## 8. RETRIEVE API flow

```http
POST /api/kiosk/retrieve/start
{ "retrievalCode": "12345678", "targetFrameId": "<FRAME-B uuid>" }
```

Server:

1. Rate-limit check  
2. Validate 8-digit code + UUID frame  
3. `SELECT … WHERE retrieval_code=? AND status='STORED'`  
4. Require `reference_face_s3_key`  
5. facecompare reference mode vs FRAME-B  
6. On match only: `start_retrieval` → RETRIEVING  
7. Safe JSON: success/matched/similarity/threshold/reason/transactionId/lockerId  

`POST /api/kiosk/retrieve/complete` → RETRIEVED + optional reference delete.

---

## 9–10. UX removals

| Item | Status |
|---|---|
| ID verification during RETRIEVE | **Removed** (STORE only) |
| Manual locker number entry | **Removed** (from transaction) |

---

## 11–12. FRAME-A / FRAME-B

| Capture | Role |
|---|---|
| FRAME-A | Verified STORE live face → reference_frame_id + durable S3 copy |
| FRAME-B | Current RETRIEVE capture; exact target; expected ≠ FRAME-A |

Phase 2A exact-frame retries reuse the **same** FRAME-B until ready or timeout.

---

## 13. Face result contract (safe fields)

`success`, `matched`, `similarity`, `threshold`, `reason`,  
`targetFrameId`, and after auth only: `transactionId`, `lockerId`, `additionalFee`, `status`.

Reasons include:  
`SIMILARITY_ABOVE_THRESHOLD`, `SIMILARITY_BELOW_THRESHOLD`,  
`TARGET_FRAME_NOT_READY`, `REFERENCE_FACE_MISSING`,  
`NO_FACE_IN_REFERENCE`, `NO_FACE_IN_TARGET`, `INVALID_RETRIEVAL_CODE`, …

Never: S3 keys, base64, images, session secrets, API keys.

---

## 14. Bypass prevention

- Old code-only start → `400 TARGET_FRAME_ID_REQUIRED`
- Wrong code → `409 RETRIEVAL_UNAVAILABLE` (no locker fields)
- Face fail / not ready / missing reference → remains **STORED**
- No browser-supplied reference S3 key accepted

---

## 15–16. Not-ready + threshold

| Case | Behavior |
|---|---|
| `TARGET_FRAME_NOT_READY` | STORED; same frame retry; not a biometric failure / not rate-limited as code fail |
| Threshold | **90** (config `FACE_SIMILARITY_THRESHOLD`) |

---

## 17. Reference cleanup

| Status | Reference |
|---|---|
| STORED / RETRIEVING | Must remain |
| RETRIEVED | Eligible for delete |
| Demo default | Immediate delete on complete + `reference_face_deleted_at` |

Configurable: `KIOSK_REFERENCE_DELETE_ON_RETRIEVE`,  
`KIOSK_REFERENCE_POST_RETRIEVAL_RETENTION_SECONDS`.

No legal retention duration was invented. Project docs did not define a
conflicting post-retrieval legal period for locker references (only general
`frames/` 30-day style lifecycle).

---

## 18. Privacy / security checks

- No embeddings  
- No ID image in SQLite/session  
- No logging of images, base64, signed URLs, cookies, credentials  
- Safe logs only  

---

## 19–20. Test results

```text
Targeted: 96 passed (store/app/facecompare/js/session subset earlier)
Full suite: 108 passed in ~2.7s
py_compile: OK
node --check kiosk.js app.js: OK
git diff --check: OK
Hanging tests: none observed
```

---

## 21–22. Live E2E

**Not run.** Read-only AWS inspection (2026-08-11):

| Resource | State |
|---|---|
| `video-analyzer-stack` | Not present |
| FrameStream | Not found |
| EnrichedFrame | Not found |
| frames S3 bucket | Not found |
| facecompare Lambda | Not found |

Instruction: do not recreate that stack in this task.  
Unit tests fully mock DynamoDB/S3/Lambda paths.

---

## 23. AWS / infra changes

| Change | Done? |
|---|---|
| CFN lifecycle for `locker-references/` exclusion | **Not needed** (prefix already exclusive) |
| New bucket | **No** |
| IAM broaden | **No code-driven IAM deploy** |
| Runtime note | When stack is restored, FastAPI needs: DynamoDB GetItem on EnrichedFrame, S3 Get/Put/Delete/Copy on frames bucket (at least `frames/*` + `locker-references/*`), Lambda Invoke on facecompare. Current hackathon role already has broad project S3/DynamoDB/Lambda on named resources when they exist. |

---

## 24. Documentation

- `docs/KIOSK_UI.md` updated  
- `2026_08_10_grok_report_01.md` section appended  
- This file created  

---

## 25. Commit

**Nothing was committed.**
