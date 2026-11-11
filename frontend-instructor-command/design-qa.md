# Dayweave Earth-Tone Redesign QA

**Source visual truth path**

- `/Users/clementbowe/.codex/generated_images/01a02ba4-22c4-7c10-9cd2-782629d5e73c/exec-d9a74134-72ff-480e-aa29-268e420f2653.png`

**Implementation evidence**

- Mobile viewport: `/Users/clementbowe/.codex/visualizations/2026/08/22/01a02ba4-22c4-7c10-9cd2-782629d5e73c/dayweave-earth-tone/dayweave-mobile-viewport.png`
- Desktop full page: `/Users/clementbowe/.codex/visualizations/2026/08/22/01a02ba4-22c4-7c10-9cd2-782629d5e73c/dayweave-earth-tone/dayweave-desktop-full.png`
- Normalized side-by-side comparison: `/Users/clementbowe/.codex/visualizations/2026/08/22/01a02ba4-22c4-7c10-9cd2-782629d5e73c/dayweave-earth-tone/dayweave-qa-comparison.png`

**Viewport and normalization**

- State: mock-data Schedule screen with three appointments, travel, recovery buffer, safe opening, break, and two pending requests.
- Source pixels: `852 × 1846`.
- Source normalized copy: `390 × 844` using proportional `sips` downsampling.
- Implementation pixels: `390 × 844`.
- CSS viewport: `390 × 844`.
- Implementation `deviceScaleFactor`: `1` for the Playwright QA capture.
- Desktop implementation evidence: `1440 × 1588` full-page capture at a `1440 × 1000` CSS viewport.

**Full-view comparison evidence**

- The final normalized comparison shows the same product hierarchy as the selected direction: brand/request utility header, prominent date and interactive seven-day strip, green route card, continuous travel-aware timeline, terracotta request cue, and persistent one-handed navigation.
- The selected fixture is historical relative to the QA date, so the implementation truthfully labels the hero as a `Past date` route recap instead of copying the source's live `Leave in 12 min` state.
- The implementation intentionally contains eight schedule blocks from the real fixture instead of the shorter illustrative route in the source. This pushes the request cue below the first viewport, but preserves all operational information and keeps it reachable above the fixed navigation.
- The fifth Settings destination is retained as an intentional product constraint because account, scheduling-policy, and payment workflows already depend on it.

**Focused region comparison evidence**

- No separate crop was required. At the normalized `390 × 844` size, the brand mark, date hierarchy, departure card, schedule-node icons, labels, and bottom navigation remain readable at 1:1. The live browser was additionally scrolled to the final request card to verify safe-opening, edit, request, and bottom-navigation controls without overlap.

**Required fidelity surfaces**

- Fonts and typography: heavy sans-serif display hierarchy now matches the source direction; small uppercase labels use increased tracking; timeline metadata stays legible without competing with names and times. Long locations truncate instead of widening the viewport.
- Spacing and layout rhythm: `14–18px` mobile gutters, a consistent `15–22px` card rhythm, 44px-or-larger controls, quiet dividers, and a continuous node rail preserve the source composition while supporting the denser real fixture.
- Colors and visual tokens: warm ivory, forest, eucalyptus, ochre, and terracotta are centralized as earth-tone tokens and maintain readable foreground contrast. Semantic travel, buffer, request, opening, and danger states remain distinct without aquatic blue.
- Image quality and asset fidelity: the Dayweave woven mark is a source-derived raster asset at `public/assets/dayweave-mark.png`; interface symbols use the Phosphor icon library. No emoji, handcrafted SVG, or CSS-drawn replacement imagery is used.
- Copy and content: product-owned copy is rebranded to Dayweave and generalized from instructor-only rhetoric to provider language. Provider business names, client data, feasibility evidence, and backend contracts remain unchanged.

**Findings**

- No actionable P0, P1, or P2 findings remain.
- P3 follow-up: live-adapter end-to-end execution still requires an absolute reachable backend URL; mock, authenticated-route, provider-settings, and scheduling-policy browser coverage is complete.

**Comparison history**

1. Initial browser capture
   - [P1] Provider schedule instants rendered in the browser timezone. Fixed by allowing timezone-aware formatting and rendering schedule/departure times in `snapshot.instructor.timezone`.
   - [P2] The compact safe-opening action lost its visible icon below 420px. Fixed by separating its text and icon and hiding only the text at the smallest breakpoint.
   - [P2] The generic path icon drifted from the selected Dayweave mark. Replaced it with the source-derived woven brand asset.
   - Evidence after fixes: `dayweave-mobile-viewport.png` and `dayweave-qa-comparison.png` at the paths above.
2. Typography comparison
   - [P2] Editorial serif display text diverged from the source's practical heavy sans hierarchy. Replaced display serif declarations with the product sans stack and explicit 900 weights.
   - Post-fix evidence: the final normalized side-by-side comparison listed above.
3. Independent testing and code review
   - [P1] The authenticated suite retained old shell dimensions and broad Find locators. Updated the redesign assertions and made all navigation selectors exact; all 35 authenticated browser checks now pass.
   - [P1] Historical and canceled lessons could be presented as an `On time` departure. The route card now filters canceled lessons, selects the actual next active lesson on the provider's current date, and distinguishes past, current, and future copy.
   - [P1] The seven-column strip overflowed at 320px. Tracks now shrink with `minmax(0, 1fr)`, and a `320 × 568` no-overflow/action-clearance regression is part of the mock suite.
   - [P1] Request labels could use the browser timezone. All schedule, request, finder, and client-detail surfaces now pass the provider timezone while preserving timezone-less local wall-time fixtures.
   - [P2] Active navigation lacked state and focus restoration. Active items now expose `aria-current="page"`, and SPA transitions restore focus to the visible selected navigation control.
   - [P2] The week strip was passive and ambiguous. Every day is now a button with a full accessible date name and loads that schedule date while preserving focus and URL state.
   - [P2] The misleading safe-opening shortcut discarded its time and opened unrelated recommendations. The shortcut was removed; the safe opening remains visible evidence while Find remains a deliberate workflow.
   - [P2] The large cascade override was split into `dayweave-theme.css` and `dayweave-schedule.css` so legacy compatibility, global tokens, and schedule-specific rules have explicit ownership.
   - Post-fix evidence: final mobile, desktop, comparison, browser-console, and regression results listed in this report.

**Primary interactions tested**

- Add schedule event opens and closes the focused editor.
- Schedule, Find, Requests, Clients, and Settings navigation remains reachable from the one-handed navigation.
- Find renders the safe-start workflow; Requests renders delivery management; Schedule returns to the departure/timeline view.
- Safe opening routes to Find, break edit remains available, and the page scroll reaches the pending request card above the fixed navigation.
- The mock Playwright suite passed all 14 end-to-end checks, including 320px containment/date navigation, schedule-event CRUD, narrow-width controls, requests, direct booking, recurring handoff, and calendar handoff.
- The authenticated provider/settings/policy suite passed all 35 checks.

**Console errors checked**

- Browser console errors and warnings: none.

**Implementation checklist**

- [x] Replace aquatic shell with earth-tone Dayweave tokens.
- [x] Preserve travel, buffer, open-gap, request, settings, and booking workflows.
- [x] Add source-faithful brand asset and library icons.
- [x] Verify `320px`, `375px`, `390px`, and desktop layouts.
- [x] Pass typecheck, 75 unit tests, production build, 14 mock browser checks, 35 authenticated browser checks, and live visual QA.

final result: passed
