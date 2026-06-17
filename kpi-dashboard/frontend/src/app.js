// KPI dashboard frontend (vanilla JS, no build step).
// Polls /api/kpis and renders. Handles partial failure: a section that comes back
// as {"error": "..."} shows "error" on its card (hover the value for the message)
// instead of breaking the whole page.
"use strict";

var REFRESH_MS = 30000; // overridden by response.refresh_interval_seconds

function fmt(v) { return (v === null || v === undefined) ? "—" : v; }

// Set one metric value from a (possibly errored / missing) section object.
function setVal(id, section, field) {
  var el = document.getElementById(id);
  if (!el) return;
  if (section && section.error) {
    el.textContent = "error";
    el.title = section.error;        // hover shows the AWS error (e.g. AccessDenied)
  } else {
    el.textContent = fmt(section ? section[field] : undefined);
    el.title = "";
  }
}

function render(d) {
  var src = (d.source || "").toLowerCase();
  var badge = document.getElementById("source-badge");
  badge.textContent = src.toUpperCase() || "—";
  badge.className = "badge " + (src === "mock" ? "mock" : (src === "error" ? "error" : "live"));

  var status = ["updated: " + (d.generated_at || "—")];
  if (d.cache_hit) status.push("cache");
  if (d.stale) status.push("STALE");
  document.getElementById("updated").textContent = status.join(" · ");
  document.getElementById("status").textContent = "";

  // Surface backend-level errors to the console for debugging (perms, etc.)
  if (d.error) console.warn("kpis error:", d.error);
  if (d.collect_error) console.warn("kpis collect_error:", d.collect_error);
  if (d.recent_errors_error) console.warn("recent_errors_error:", d.recent_errors_error);

  var k = d.kpis || {};
  setVal("kinesis-latency", k.kinesis, "put_latency_ms_avg");
  setVal("kinesis-records", k.kinesis, "incoming_records");
  setVal("kinesis-iterage", k.kinesis, "iterator_age_ms_max");
  setVal("ip-ok", k.imageprocessor, "succeeded");
  setVal("ip-fail", k.imageprocessor, "failed");
  setVal("rk-count", k.rekognition_detect_labels, "count");
  setVal("s3-ok", k.s3_put, "success");
  setVal("s3-fail", k.s3_put, "failed");
  setVal("ddb-ok", k.dynamodb_put, "success");
  setVal("ddb-fail", k.dynamodb_put, "failed");
  setVal("fc-count", k.facecompare, "count");
  setVal("fc-ok", k.facecompare, "success");
  setVal("fc-fail", k.facecompare, "failed");
  setVal("ef-count", k.enrichedframe, "count");
  setVal("fr-count", k.frames, "recent_count");
  setVal("fr-age", k.frames, "latest_frame_age_seconds");

  var rows = document.getElementById("error-rows");
  if (d.recent_errors_error) {
    rows.innerHTML = '<tr><td colspan="4">에러 로그 조회 실패: ' + fmt(d.recent_errors_error) + "</td></tr>";
  } else {
    var errs = d.recent_errors || [];
    if (!errs.length) {
      rows.innerHTML = '<tr><td colspan="4">에러 없음</td></tr>';
    } else {
      rows.innerHTML = errs.map(function (e) {
        return "<tr><td>" + fmt(e.timestamp) + "</td><td>" + fmt(e.component) +
               "</td><td>" + fmt(e.event) + "</td><td>" + fmt(e.error) + "</td></tr>";
      }).join("");
    }
  }

  if (d.refresh_interval_seconds) REFRESH_MS = d.refresh_interval_seconds * 1000;
}

function load() {
  fetch("api/kpis")
    .then(function (r) { return r.json(); })
    .then(render)
    .catch(function (e) {
      document.getElementById("status").textContent = "fetch error: " + e;
    });
}

load();
setInterval(load, REFRESH_MS);
