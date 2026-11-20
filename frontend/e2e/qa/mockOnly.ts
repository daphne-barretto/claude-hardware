// Inventory entries and states the real backend can't reach with the scripted Claude
// (backend/app/e2e_api.py), because they need what only the mock API's recorded runs make.
// `make e2e` still checks them on the mock API (e2e/ui-inventory.spec.ts).

export const MOCK_ONLY: { entries: RegExp; states?: RegExp; why: string }[] = [
  { entries: /^conflict\./, states: /^session\.conflict/, why: "the can't-satisfy run is a recorded fixture (fixtures/conflict-events.jsonl)" },
  { entries: /^chat\.risk-/, why: "risk flags come from the recorded run (fixtures/risk-flags.json)" },
  { entries: /^chat\.found-/, states: /^session\.chat-found$/, why: "Claude's find in the chat is a recorded reply" },
  { entries: /^chat\.start-new-chat$/, states: /^gone$/, why: "the gone-session state is a mock API session ID" },
  {
    entries: /^parts\.find(-send)?$/,
    states: /^session\.found$/,
    why: "the fixture draft map flags low-draft faces to find; the real backend's map of the same build has none, so the find rightly finds nothing",
  },
  { entries: /^comments\.thread-send$/, why: "Claude's reply in the comment thread is the recorded run's" },
  {
    entries: /^(chat\.stop|queue\.)/,
    states: /^new\.(working|queued)/,
    why: "stopping or queueing behind a run needs the mock's slow recorded run; the scripted run ends before Stop lands",
  },
  { entries: /^failed-build\./, states: /^session\.failed/, why: "the failed build is the mock's recorded \"Break the build\" run" },
  {
    entries: /^constraint\.(edit|value|save|cancel|place)$/,
    states: /^new\.(editing|answered)$/,
    why: "on the real backend New starts a board-less session, where the board-only standard the entry edits is not among its constraints (it applies to board designs)",
  },
  {
    entries: /^changes\./,
    states: /^session\.changes$/,
    why: "the board example's change set is the recorded run's (fixtures/change-set.json); the real backend's change set is swept on a board-less design (states.ts, changes)",
  },
];
