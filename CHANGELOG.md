# Changelog

All notable released changes to `bdo-toolkit` are documented here.

## Unreleased

### Added

- Add profile-based combat telemetry. An optional schema-1 `combat` layout
  supports `LiveCombatSession` and streaming `replay_combat`, with immutable
  spawn, hit and kill events of objects near the player. Spawns pair each
  object ID with its game data character key; hits carry attacker and target
  IDs, skill number, damage, the target's HP after the hit, critical, back
  and air attack, the target's position, and the target's character key when
  its spawn was seen; kills carry victim, killer, character key and position.
  Every nearby player's hits and kills are included. A spawn or kill the
  server sends again is reported once per connection. Unexpected record list
  shapes, non-finite or negative damage and HP, or non-finite positions fail
  closed. Loot is not linked to kills.
- Add combat layout relocation. `relocate_combat_replay` and
  `relocate_combat_live` find the combat opcodes, record list shapes and
  field offsets again after a patch from a short, clean capture of grinding.
  Record list geometry comes from message lengths and the count byte. A field
  is accepted only when its values pass a check that random bytes fail: a
  target's HP falls by the damage, a kill names a lethal hit's target at that
  moment, the killer is the lethal hit's attacker in most kills, the kill
  happens where the hit landed, and spawned objects are hit or killed later
  with the same character key. Hit ID, skill, critical and special attack
  move with their nearest checked field and must still read sensibly. The
  previous layout only breaks ties. No match, or two equally good messages,
  fails with what the capture lacked.
  `update_combat_profile` replaces only the combat layout, with a backup, an
  atomic write and an optional digest check, and never writes on its own.

## 1.0.8 - 2026-10-14

This patch adds raw server-message capture and corrects two reversed
storage-town names.

### Added

- Add opcode-free raw server message capture. The separate `bdo_toolkit.raw`
  API frames every server-to-client message without an opcode profile,
  decoding, or decryption, through streaming `replay_raw` for saved PCAP or
  PCAPNG files and single-use `LiveRawSession`. Immutable `RawMessage` values
  carry the whole frame with length, flag, opcode, and payload properties,
  plus timestamp, flow, connection epoch, per-epoch index, and the unwrapped
  TCP sequence of the first byte, so gaps stay visible. TCP gaps and flow
  evictions are counted in health rather than raised; native drops and queue
  overflows fail closed. Framing waits for a message boundary, so captures
  that attach mid-stream skip leading bytes, and messages accepted while
  resynchronizing after a TCP gap are not verified. The live event queue
  defaults to `DEFAULT_RAW_EVENT_QUEUE_SIZE` (16,384 messages).
  Contributed by @iSayZes in [#10](https://github.com/ychwu/bdo-toolkit/pull/10).

### Fixed

- Correct Valencia City and Shakatu storage names. Destination 229 (`0x00E5`)
  now reports Valencia City, and destination 601 (`0x0259`) reports Shakatu
  in storage lookups, decoded events, and item-state snapshots. Numeric IDs,
  item contents, and inferred name confidence remain unchanged. Applications
  maintaining their own town-name tables should apply the same correction.

## 1.0.7 - 2026-10-01

This release adds shared grind capture, profile-based solo XP tracking, and
explicit Agris calibration while improving capture-loss detection at shutdown.

### Added

- Add a shared-capture grind session. `bdo_toolkit.grind.GrindSession` streams
  confirmed mob-drop receipts with optional profile-based Agris and solo XP
  through one passive capture and TCP reassembly pipeline. Enabled readers
  require saved layouts, and Agris requires the caller's known maximum.
  Recognized feature decoding failures disable only that reader and emit an
  explicit error; shared capture or queue loss invalidates the whole session.
  The combined session is live and synchronous, with bounded delivery and
  optional non-overwriting recording. It does not attribute observations to
  individual kills; XP remains solo-only and character changes require restart.
- Add profile-based solo XP tracking. Optional schema-1 XP layouts support
  `LiveXPSession` and streaming `replay_xp`, with immutable level, current and
  required XP, percentage, signed changes, and session net XP readings. The
  first update establishes a baseline, duplicates add no gains, and one
  observed level rollover is accounted for. Unexplained state changes,
  connection changes, and known acquisition loss invalidate current totals.
  Explicit `update_xp_profile` preserves item/Agris and extension fields with
  backup, atomic replacement, and optional digest checks. Group play and
  automatic player identification are unsupported; no XP async facade is added.
- Add explicit Agris profile decoding and calibration. Live, async, and replay
  sessions can use a validated saved layout for immediate matching observations
  without discovery delay, retaining the required known maximum. Incompatible
  evidence fails closed without silently switching to discovery. Separate
  calibration APIs and CLI explicitly save clean finalized evidence into an
  existing profile with backup, atomic replacement, and optional digest checks,
  preserving item fields and existing cold-discovery support.

### Fixed

- Detect known missing TCP tails when capture stops. Finalization now reports
  a gap when an observed FIN lies beyond received stream bytes, including when
  capture stops before the live gap timeout and across sequence wrap. Loss is
  reported before connection-close notification; clean EOF and tails repaired
  before finalization retain their existing behavior.

## 1.0.6 - 2026-09-19

This patch adds experimental, session-local Agris balance observation and
hardens calibration and inventory-layout discovery after packet changes.

### Added

- Add session-local Agris balance discovery. The separate
  `bdo_toolkit.agris` API supports synchronous and asynchronous live sessions
  and saved-capture replay, while `agris live` and `agris replay` expose the
  same capability through the CLI. Every acquisition entry point requires the
  caller's known maximum points and learns a provisional layout without reading
  or writing opcode profiles. Outputs contain observed remaining and maximum
  points with timestamp and connection provenance, not inferred starting
  balances or consumption totals; ambiguous discovery or acquisition loss
  withholds current balance authority.

### Fixed

- Avoid mistaking receipt batch counts for storage destinations. Complete
  record geometry now prevents a batch-count field that matches a registered
  town ID from independently implying the opposite transfer direction. Live
  guidance, automatic completion, and offline calibration use the same
  corrected inference while separate conflicting evidence remains rejected.
- Reject incomplete inventory container columns. Dynamic tail-layout discovery
  now requires slot and container columns to cover every record in each
  hydration wrapper, preventing a following record header from masquerading as
  incomplete tail metadata and suppressing a valid header-based layout.

## 1.0.5 - 2026-09-15

This patch adds read-only Windows capture discovery for network setups where
automatic interface or port selection is unsuitable.

### Added

- Discover capture settings from running BDO connections. Applications can use
  the public `diagnose_capture()` API, and users can run `diagnose-capture` with
  human-readable or JSON output, to inspect candidate interfaces, local
  addresses, peer ports, proxy ownership, and grouped settings proposals.
  Candidates remain unverified and require explicit selection; discovery does
  not apply capture settings or change existing automatic detection. Other
  platforms and inspection failures return explanatory diagnostics.

## 1.0.4 - 2026-09-10

This release adds guided live calibration progress while hardening item
decoding, live TCP recovery, diagnostics, async completion, and worker-origin
performance.

### Added

- Observe calibration progress and stop when evidence is complete. Synchronous
  and asynchronous live sessions can report replaceable evidence assessments
  and optionally stop once calibration is ready. Final retained frames are
  revalidated before a result is returned, profile writes remain separate, and
  manual stopping remains the default.
- Expose provisional transfer observations during live calibration. Immutable
  progress snapshots can report structurally supported transfer candidates,
  including direction, watched-item counts, opcode, capture order, and
  session-scoped identity. Observations remain conservative and replaceable;
  they do not change calibration readiness, final schemas, or runtime decoding.

### Changed

- Avoid repeated structural companion discovery. Deposit-origin lookahead now
  reuses bounded positive and negative byte-level matches while continuing to
  evaluate ownership, conflicts, flow boundaries, operation identity, and
  ordering live. Public decoding and observation output remain unchanged.

### Fixed

- Require independent message boundaries for item activity. Nested signatures
  and differently segmented retransmissions can no longer create ordinary
  inventory or loot events without independent top-level framing evidence.
- Preserve inventory boundary recovery across TCP sequence wraps by comparing
  unwrapped absolute sequence positions while retaining the existing flow,
  generation, adjacency, and wrapper-validation requirements.
- Preserve queued TCP data before servicing idle gap deadlines. Live item
  capture now services the idle clock on the decoder worker at drained queue
  boundaries, preventing already accepted data from being abandoned while
  truly missing segments still time out.
- Report shortened storage messages as decoder incompatibility. Independently
  framed configured storage opcodes that are too short for layout validation
  now update decoder health and emit the existing out-of-band warning without
  misclassifying compact count-zero or valid shorter activity messages.
- Reject transfers after structural record validation fails. Invalid record
  markers, instances, and repeated-record geometry no longer retry through
  weaker configured-length or stride decoding; structurally unsupported valid
  layouts retain their existing fallback paths.
- Preserve calibration outcomes when async polling races completion.
  `AsyncCalibrationSession.wait()` now rechecks terminal state after an empty
  poll so successful results and terminal errors are delivered, discarded
  sessions still return `None`, and cancellation continues to settle the
  pending worker operation.

## 1.0.3 - 2026-09-03

This patch improves post-patch calibration and live Arena of Solare capture
reliability while documenting the current capture-environment and game-client
lifecycle boundaries.

### Fixed

- Recognize trailing-context storage decrement evidence. Transfer calibration
  now retains `SOURCE_CONTAINER_DECREMENT` evidence when a patch moves the
  source context after the removed quantity. The fallback requires one exact
  receipt-instance anchor and rejects ambiguous trailing contexts; historical
  layouts and runtime decoding remain unchanged.
- Prevent premature Solare live TCP gap recovery. Timeout-based recovery is now
  deferred until live finalization so Npcap batching, callback latency, or
  worker backlog cannot age a fresh out-of-order gap past its deadline and
  withhold an otherwise complete leaderboard snapshot. Bounded-memory recovery
  remains immediate, and deterministic capture-time replay is unchanged.

### Docs

- Clarify the toolkit capabilities in the README with a capability-led overview
  of item events, item-state snapshots, and Arena of Solare leaderboards, plus
  direct links to each workflow guide. Remove overlapping documentation and
  contributor sections so the README stays focused on package users.
- Document current storage and Solare capture boundaries. Current NA/EU town
  storage must be captured on the first world load after a fresh game launch;
  later character switches continue to hydrate inventory only. Arena of Solare
  guidance now recommends saving live traffic and reusing the completed
  recording for repeatable offline analysis.
- Document live-capture compatibility and interface selection, including the
  validated Windows/Npcap baseline, the IPv4/TCP boundary, VPN-aware automatic
  and explicit interface selection, and the limits of queue sizing and
  Python-side filtering. Expand acquisition troubleshooting and support-report
  guidance without exposing private packet contents.

### Tests

- Refresh full-width Silver fixture expectations so the reviewed July
  character-state assertions and August replay baseline retain complete
  unsigned 64-bit quantities instead of only their lower 32 bits.

## 1.0.2 - 2026-09-01

This patch hardens passive capture and character-load decoding across padded
packets, burst reordering, rapid storage transfers, and updated inventory
layouts.

### Fixed

- Preserve character-load snapshots during reordered receive bursts. Finite
  replay and live sessions now retain larger bounded Windows/Npcap receive-order
  inversions without changing the ordinary continuous-item allowance.
  Persistent gaps, timeouts, memory ceilings, and frame validation remain
  fail-closed.
- Restore inventory category detection after tail-field reordering. Hydration
  layouts with the validated container code immediately before the slot byte
  again classify Main, Pearl, Global Currencies, and Enhancement inventory
  records and recover known currency balances. Count-zero wrappers remain
  unclassified because they contain no record-level category evidence.
- Preserve full-width inventory snapshot quantities. Positive character-load
  amounts now retain their unsigned 64-bit wire values in low-level events and
  assembled item state; zero remains unresolved. Live receipts, loot previews,
  storage events, decrement evidence, and calibration quantities retain their
  existing unsigned 32-bit semantics.
- Preserve manual storage origins during rapid transfers. A bounded,
  generation-aware evidence ledger now retains unique manual attribution
  through rapid and fragmented traffic without allowing stale, ambiguous,
  reused, or pre-reset evidence to classify another deposit.
- Ignore Ethernet padding outside validated TCP payloads. Shared Scapy capture
  extraction now follows validated IPv4 and TCP header lengths so link-layer
  padding cannot advance sequence reassembly and hide later storage or worker
  events; selected-flow truncation and fragmentation fail closed.

### Tests

- Make the IPv6 scope regression independent of host routes by assigning
  explicit local Ethernet addresses to the synthetic exclusion packet.

### Maintenance

- Align GitHub release notes with the established `v1.0.0` house format:
  Highlights, a one-sentence theme, relevant New, Improvements, and Fixes
  sections, followed by the full-changelog link. Empty categories and routine
  internal detail remain omitted.

## 1.0.1 - 2026-08-31

This release moves the supported runtime to Python 3.14 and removes
compatibility branches that existed only for older interpreters. Package and
protocol behavior otherwise remain unchanged.

### Changed

- Require Python 3.14 or newer. Applications still using Python 3.10 through
  3.13 must upgrade their interpreter before installing this release.

### Maintenance

- Run CI on Python 3.14 across Windows and Ubuntu and configure mypy for the
  supported interpreter version.
- Adopt Python 3.14 type-parameter and type-alias syntax, `datetime.UTC`, and
  the guaranteed exception-note APIs throughout capture, calibration, item
  state, profile, and Arena of Solare code.
- Remove Python 3.10-specific async shutdown timing branches while preserving
  bounded cleanup, cancellation, and retry ownership behavior.

## 1.0.0 - 2026-08-30

The first stable release of `bdo-toolkit` establishes a passive, read-only
Python and CLI toolkit for live or recorded Black Desert telemetry. Validation
currently covers the NA/EU service. Item State and Arena of Solare remain
explicitly experimental APIs inside the stable package.

### Upgrade notes for pre-release consumers

- Item-event and item-state decoding now requires an explicit local
  `opcode_profile`; CLI `replay` and `live` commands require `--profile`.
  Profiles are never bundled, fetched implicitly, or merged across eras.
- Item-state serialization uses schema version 5. Consumers must use
  `storages`, `SnapshotItem.observed_at`, the `StorageContents` query surface,
  and opt-in typed diagnostics instead of removed schema-4 aliases.
- Storage events use `event_type` as the live/snapshot/unresolved discriminator,
  `source` as semantic origin, `storage_id` as authoritative destination, and
  `quantity` as the sole amount. The former storage-operation, deposit-origin,
  and quantity aliases and filters are removed.
- Raw context `0x3e010000` is labeled `Remote Inventory`; exact-source filters
  using `Magnus Remote Inventory` must be updated.
- Completed calibration results expose retention accounting only through the
  required `CalibrationResult.retention` object.
- Solare snapshot evidence is owned by `SolareCaptureResult.evidence`; it is no
  longer duplicated on `SolareLeaderboardSnapshot`.
- Direct `ProfileFetchResult` construction no longer accepts stored `path` or
  `profile_sha256` fields. Its read-only `path` derives from the installed
  profile.

### Added

- Introduce the first stable release with packet capture and PCAP replay,
  structured item and storage events, synchronous and asynchronous sessions,
  calibration and profile tooling, typed metadata, CLI workflows, experimental
  item-state snapshots, and experimental Arena of Solare snapshots.
- Add explicit verified HTTPS profile installation through
  `fetch_opcode_profile()` and `bdo-toolkit profile fetch`, with bounded
  schema-1 envelope validation, SHA-256 integrity verification, and atomic
  caller-owned installation.
- Register Angavu Outpost storage while keeping numeric `storage_id` as the
  authoritative endpoint identity.
- Add `CaptureEndpoint.to_dict()` and share its representation with the frozen
  `SolareCaptureEndpoint` subclass.

### Changed

- Require explicit opcode profiles and remove the packaged default profile.
- Compact the item-state result contract around schema version 5, canonical
  inventory and storage summaries, coverage, provenance, warnings, decoder
  health, and optional typed diagnostics.
- Make storage source, endpoint, event type, and quantity fields canonical.
- Canonicalize calibration retention under `CalibrationResult.retention`.
- Make Solare capture results the sole owner of classification evidence.
- Simplify fetched profile results while preserving verified installation.
- Rename the remote-inventory source label to `Remote Inventory`.
- Share capture endpoint serialization and the capture-health loss predicate
  across item and Solare acquisition.
- Mark the Arena of Solare domain public and experimental within the stable
  package; its Python and serialized contracts may still change before domain
  promotion.

### Fixed

- Keep live health sampling independent of packet decoding to prevent lock
  inversion and deadlock during active delivery.
- Preserve worker-deposit attribution through bounded interleaved storage
  traffic while failing closed on contested or ambiguous ownership.
- Correct Solare leaderboard guidance to capture the first load after a game
  restart rather than relying on cached refresh behavior.
- Refuse incomplete automatic transfer profiles unless manual-decrement
  evidence is present, before any profile backup or replacement occurs.
- Prevent Python 3.10 async Solare shutdown polling from busy-spinning on the
  coarse Windows monotonic clock.

### Docs

- Publish the reference-first documentation site and promote it as the
  canonical API reference with task-first navigation, stable deep links,
  searchable public symbols, and responsive calibration walkthroughs.
- Rework the repository README into a concise stable-package entry point with
  current capabilities, installation, examples, support, and documentation
  routes.
- Make the Quickstart and example catalog live-first and task-oriented across
  item activity, item state, calibration, asyncio, and Solare.
- Add game-context orientation and clarify item-event meanings, source labels,
  storage iteration, item-state acquisition, output, acceptance checks, and
  advanced diagnostics.
- Reconcile all profile and calibration guidance around explicit local
  authority, local patch-day calibration, verified optional fetching, schema-1
  validation, controlled transfer evidence, and loot-preview limitations.
- Document NA/EU as the validated service region and treat other regions as
  unverified.
- Consolidate callback, health, lifecycle, profile-verification, Solare-load,
  and troubleshooting guidance into their owning reference pages.
- Retire stale site-draft ledgers while preserving the retained redesign file
  only as a local historical source snapshot.

### Tests

- Refocus the test suite on current public contracts, distinct protocol
  geometries, lifecycle failures, concurrency boundaries, cross-patch behavior,
  and reviewed private-fixture regressions.
- Normalize test source formatting without changing coverage.

### Maintenance

- Establish the local changeset, changelog, version-source, and release
  governance workflow used to prepare this release.
- Add guarded PyPI Trusted Publishing for version-matched tags on `main`, with
  separate unprivileged build validation and OIDC-enabled publication jobs.
- Modernize MIT license metadata and the Setuptools build requirement for
  warning-free wheel and source-distribution builds.
- Clean generated caches and local scaffolding without changing package
  behavior.
- Remove local protocol-research helpers and private-fixture baseline tooling
  from repository tracking; maintainer-local copies remain ignored and no
  private capture evidence is published.
