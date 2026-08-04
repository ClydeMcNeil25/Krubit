# Enhanced Twitch Preview Embed Design

**Date:** 2026-08-04  
**Status:** Approved  
**Selected direction:** Enhanced Single Embed (visual option A)

## Purpose

Make Krubit's live announcement feel as polished as a premium Twitch notification while preserving the operational information and creature-language identity already approved for Phase 2A.

## User Experience

Each qualifying Twitch live transition produces the existing single Discord message in `#live-notifications`:

1. Krubit's creature-language content line intentionally mentions `@everyone`.
2. One purple Discord embed presents:
   - the linked title `🔮 LIVE SIGNAL FOUND`;
   - the existing description;
   - Creator, Platform, Title, Category, and Status fields;
   - the existing compact Twitch thumbnail;
   - a large 640×360 Twitch stream preview beneath the fields;
   - the existing Krubit footer.
3. The existing **Fetch the Stream** link button opens the creator's canonical Twitch URL.

The embed title also opens that same canonical Twitch URL. The large preview is a Twitch-generated still image, not an inline playable video. Playback remains on Twitch through the title or button.

## Rendering and Data Flow

The existing Twitch enrichment remains the only source of stream metadata. No additional API integration, permission, database table, or background task is introduced.

When Twitch returns a live stream:

- the renderer uses the already-normalized creator URL as the embed URL;
- the existing thumbnail canonicalization replaces Twitch's width and height placeholders with `640` and `360`;
- the canonical HTTPS thumbnail is supplied as both the compact thumbnail and the embed's large image;
- the existing sanitized values populate all five statistics fields;
- the durable delivery and reconciliation services continue to own announcement deduplication.

If the initial Discord presence arrives before Twitch enrichment, Krubit may publish the existing honest reduced card. When enrichment succeeds, the existing announcement-edit path upgrades that same message with the linked title, final metadata, and large preview. It must not create a second message.

## Failure and Safety Behavior

- Missing, malformed, non-HTTPS, or overlong Twitch thumbnail URLs produce no image rather than a broken or unsafe preview.
- Twitch lookup failure retains the existing honest `Unavailable` values and does not fabricate stream metadata.
- The fixed creature-language line and controlled `AllowedMentions` behavior remain unchanged.
- Creator-provided text remains escaped, mention-safe, null-safe, and bounded to Discord field limits.
- The existing delivery key, nonce, recovery scan, and one-row-per-stream behavior remain unchanged.
- An image or embed-rendering failure follows the existing durable delivery failure and retry path.

## Component Boundaries

- `krubit.discord.live_signals.render_live_embed` owns the linked title, compact thumbnail, large image, fields, and honest fallback.
- `krubit.discord.live_signals._thumbnail_url` remains the sole preview URL canonicalization and validation boundary.
- `krubit.discord.live_runtime.LiveSignalRuntime` continues to send and edit the rendered embed without presentation-specific branching.
- Twitch, Discord transport, storage, role assignment, and session lifecycle behavior remain outside this presentation-only change.

## Test Contract

Automated tests must verify:

- a valid Twitch stream renders the canonical 640×360 URL as a large embed image;
- the embed title links to the normalized Twitch channel URL;
- Creator, Platform, Title, Category, and Status remain present and correct;
- the compact thumbnail and large preview use only the validated HTTPS URL;
- malformed or unsafe thumbnails render no preview;
- the reduced Twitch-unavailable card remains honest and contains no broken preview;
- enrichment edits the existing message instead of announcing again;
- existing controlled-mention, role ownership, nonce, recovery, and deduplication tests remain green.

## Acceptance Criteria

- The live announcement visually matches option A approved in the brainstorming companion.
- One Krubit message contains the alien alert, statistics embed, large Twitch preview, and stream button.
- A user can reach the Twitch stream from both the embed title and button.
- No new Discord permissions or secrets are required.
- Full tests, Ruff, and Pyright pass before runtime rollout.

## Out of Scope

- Inline Twitch video playback inside Discord.
- A separate second Twitch embed.
- Reliance on Discord's optional raw-link unfurl behavior.
- Periodic screenshot refreshes during a stream.
- New Twitch profile-image or channel-branding lookups.
