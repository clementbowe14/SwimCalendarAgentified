import assert from "node:assert/strict";
import test from "node:test";
import { trustedCalendarUrl, fetchCalendarDownload } from "../src/api/calendar.js";

test("calendar paths use backend origin without duplicating API prefixes", () => {
  for (const base of ["https://api.example/api/public", "https://api.example/api/public/"]) {
    assert.equal(trustedCalendarUrl("/api/public/requests/token/calendar.ics", "ics", base, "https://ui.example/?invite=secret"), "https://api.example/api/public/requests/token/calendar.ics");
    assert.equal(trustedCalendarUrl("api/public/requests/token/calendar.ics", "apple", base, "https://ui.example"), "https://api.example/api/public/requests/token/calendar.ics");
  }
});

test("calendar links reject unsafe schemes, credentials and untrusted origins", () => {
  for (const value of ["javascript:alert(1)", "data:text/calendar,hello", "//evil.example/file", "https://api.example.evil/file", "https://user@api.example/file"]) {
    let rejected = false;
    try { trustedCalendarUrl(value, "ics", "https://api.example/api/public", "https://ui.example"); } catch { rejected = true; }
    assert.equal(rejected, true, value);
  }
  for (const value of ["http://calendar.google.com/calendar/render", "https://calendar.google.com.evil/calendar/render", "https://calendar.google.com:444/calendar/render", "https://calendar.google.com/other"]) {
    let rejected = false;
    try { trustedCalendarUrl(value, "google", "https://api.example", "https://ui.example"); } catch { rejected = true; }
    assert.equal(rejected, true, value);
  }
  assert.equal(trustedCalendarUrl("https://calendar.google.com/calendar/render?action=TEMPLATE", "google", "https://api.example", "https://ui.example"), "https://calendar.google.com/calendar/render?action=TEMPLATE");
});

test("download refuses HTTP errors and non-calendar payloads and suppresses referrers/redirects", async () => {
  const original = globalThis.fetch;
  try {
    for (const [status, contentType] of [[409, "text/calendar"], [200, "text/html"], [200, "text/calendar;charset=utf-8"]] as const) {
      globalThis.fetch = async (_url, init) => {
        assert.equal(init?.referrerPolicy, "no-referrer");
        assert.equal(init?.redirect, "error");
        return new Response("BEGIN:VCALENDAR", { status, headers: { "content-type": contentType } });
      };
      let rejected = false;
      try { assert.equal(await (await fetchCalendarDownload("https://api.example/calendar")).text(), "BEGIN:VCALENDAR"); } catch { rejected = true; }
      assert.equal(rejected, status !== 200 || contentType === "text/html");
    }
  } finally { globalThis.fetch = original; }
});
