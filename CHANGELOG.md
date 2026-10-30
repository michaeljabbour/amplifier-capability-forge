# Changelog

## 2026-10-30 — Stable discovery identity

- Verify the declared and installed `amplifier-skill-forge` directory rather than assuming a checkout basename matches it. The public repository can use its capability name while existing discovery routes stay compatible.
- Keep local `.research` files out of the legacy installer's real copies, including its tar fallback. If an older real copy contains local research, refuse installation until it is preserved outside the discovery directory. Source links retain their existing behavior; the shared pinned installer copies only declared tracked files.

## 2026-10-28 — Film sound

- Remix the film so the score plays through without skips and ends on its own final chord instead of restarting under the end card. Smoother ducking under the voice, sound effects kept below the narrator, and crossfaded joins in the 15-second cut.

## 2026-10-28 — Film

- Add a 62-second film to the site, with captions, a transcript, a poster and a 15-second cut. The example in it is fictional, and it reports no results.

## 2026-10-28 — Public site and first-use guide

- Lead with the intended user, concrete benefits, a fictional output preview and a first task.
- Document canonical public clone/install steps, optional runtime requirements and the boundary of existing verification.
- Preserve native interfaces, source history and the retained integration guide.
