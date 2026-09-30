# Design note: the local console (30 Sep 2026)

**Goal.** Upload a recording or a note on a local page and read the result,
through the real service path (API, queues, workers), not a shortcut.

**What existed.** `call_e2e.py` starts the API, the call workers and one file
server per recording, mints the service token from `.env.demo`, pushes the
call as the CRM would, follows it and stops everything, one command per run.
Notes have the direct route, `POST /api/v1/notes/judgements/direct`.

**Options.**
- A. Wrap `call_e2e` (about half a day): the page runs it per upload. Every
  call starts and stops the whole stack (about 20 s), and notes need a
  second stack.
- B. One console, one stack (about a day and a half, picked): Start brings
  up the API, the normal and stage-2 workers and one file-and-callback
  server once, with `call_e2e`'s own pieces (token, refusals, health-key
  claim). Calls are pushed and followed; notes go to the direct route.
- C. Point at the Docker stack (about a day plus compose changes): the
  worker containers cannot fetch audio from the host without `extra_hosts`,
  and the demo override sets the demo flag on the API only.

**Pick: B.** One start, many uploads, both units in one place, nothing new
in the service.

**Safety, since uploads are real calls and notes.** Streamlit listens on
127.0.0.1 only (its default is every interface), usage statistics off, no
Deploy button; the page stops if served elsewhere. Uploads, reports, answers
and logs go to a folder outside the repository (refused inside it); nothing
is logged. `files.py` serves only the random names the console gave. Notes
are capped at 20 per click and never retried. Streamlit sits in the
`console` dependency group: never in `uv sync`, CI or the image.
