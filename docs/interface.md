# FaceID HOMEii workspace (6.5.0)

The interface follows the user's HOMEii Analytics reference: charcoal/graphite,
restrained orange, compact Hebrew typography and subtle borders. Existing React
components, state and APIs are extended; no identity/access subsystem is replaced.
Heebo fonts are bundled locally under SIL OFL (static/licenses/OFL-Heebo.txt).

## Shared visual contract

`workspace.css` is imported last and owns shared typography, controls, panels,
badges and responsive sizing across every route and dialog. Page styles own only
their task layouts. The Analytics foundation is the reference: Heebo 400/500/600,
19px page headings, 14px section headings, 13px controls, 500 emphasis, 60px
horizontal header, HOMEii mark and consistent Lucide strokes. Top navigation uses plain outline icons; topic directories use colored circles in
the Analytics navigation palette; status colors retain their semantic meaning.
Mobile inputs use 16px text and 44px controls. Both themes use shared tokens.

## Task areas

- Activity: overview, decisions awaiting review, events/investigation.
- People: registered people, reference gallery and face search.
- Cameras: cameras and entry/intercom inspection.
- Management: health, Access1 connection, guests, automations, settings. Expert
  tools are disclosed on demand; direct expert URLs still open correctly.

The overview retains horizontal swipeable detection and last-location rails.
Selecting a person filters their chronological appearances; detection clicks
open video immediately. Detailed evidence and enrollment stay in existing flows.
Assigned event history and operational reference photos are visibly separate.
Blacklist outlines and security/liveness decisions retain their existing meaning.

The management entry presents topic rows with colored circular icons and thin
dividers, matching the supplied Analytics screenshot. Header secondary actions
and person edit/favorite actions share an ellipsis menu with keyboard navigation,
Escape and outside-click dismissal. One concise information line accompanies
page headings. Evidence cards and wide video dialogs receive most of the space.

## Navigation and accessibility

All previous hash URLs and query parameters remain valid. Ctrl/Cmd+K searches
screens; title matches rank before descriptive matches. Enter selects the first
result, Escape closes, Tab stays in the dialog and focus returns afterward.
A skip link reaches main content. Expert collapse retains the active form rather
than navigating away. Theme choices persist when storage is available, with a
safe dark fallback otherwise. Narrow layouts retain full navigation and swipes.

## Verification boundary

Use the local operational QA fixture with synthetic identities and media for
browser checks. Verify desktop/mobile, dark/light, person/event selection,
reference enrollment, video startup, keyboard search and advanced routing.
Live deployments, pairing, cameras, models and permission rules are unchanged.
