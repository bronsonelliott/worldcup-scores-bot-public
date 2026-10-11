# World Cup Scores Bot

Texted the final score of each 2026 FIFA World Cup match to a Signal group chat of three friends. One message per match, no duplicates. v1 = final scores only; everything else was v2 (see backlog below).

## 🏁 Retired 2026-07-19

The tournament is over and the bot is off. LaunchDaemon booted out and the plist deleted from `/Library/LaunchDaemons/` — nothing runs on a timer anymore. Claudia sent a farewell message to the group after the final (last line of `poll.log`).

**Final tally** (from `poll.log`, all times UTC):

| | |
|---|---|
| Matches sent | 104 — every match of the tournament, zero duplicates |
| First send | 2026-06-11 21:14Z — Mexico 2–0 South Africa |
| Last send | 2026-07-19 22:08Z — Spain 1–0 Argentina (AET), MetLife Stadium |
| Run window | 39 days |
| Polls | 1,767 |
| Send errors | 0 |
| Claudia write-ups | 101 of 104 (3 fell back to the plain `Full time:` line) |

Note on dates: the log is UTC, so it opens `2026-06-11 01:56Z` — that's `2026-06-10 20:56` local, which is why the original launch date was recorded as 2026-06-10. Both are right; they're different clocks.

Everything below is how it **worked** — kept for the 2030 rebuild.

## How it works

A LaunchDaemon runs `poll.py` every 30 min. Each run:
1. `drain_receive()` — quick `signal-cli receive` to keep the linked device healthy (hygiene; never fails the run).
2. Fetch ESPN scoreboard (`fifa.world` slug, no API key) for **yesterday + today (UTC)** via explicit `?dates=YYYYMMDD`, merged by match id — see the date-rollover gotcha below.
3. Find matches with `status.type.completed == true`.
4. For each one **not** in `sent.json`: write the message, send to the Signal group, then record its id.

**The message is written by "Claudia"** — the flirty robot persona from the intro message. Each final score is sent to Claude (Haiku, via `claudia_quip()`) to be written up in her voice, reacting to the actual result (blowout, shutout, draw, upset). A score guardrail (`score_ok`) throws away any write-up that doesn't contain the real score + both team names, and **any failure — no API key, API error, guardrail trip — falls back to the plain `Full time: …` line.** The score is never wrong or missing; worst case it's just not funny.

A match id is recorded **only after a confirmed successful send**, so a failed send retries next poll instead of being lost. It never sends the same match twice.

## Files

| File | Role |
|---|---|
| `poll.py` | The poller. Stdlib only. |
| `config.json` | `account`, `group_id`, `anthropic_api_key`, `dry_run`. Git-ignored (has the Signal numbers + API key). |
| `sent.json` | Match ids already sent (the dedup state). Git-ignored. |
| `poll.log` | Run log — one line per poll. |
| `signal-cli-dist/` | The **JVM** signal-cli build (git-ignored, ~104 MB). |
| `worldcup-scores-bot.plist` | The LaunchDaemon (30-min interval). Still in the repo; **no longer installed** in `/Library/LaunchDaemons/`. |
| `link-signal.sh` | Re-link the device if the link ever breaks (opens a QR). |

## Gotchas (these cost real time — read before debugging)

- **Use the JVM signal-cli, never Homebrew's.** The Homebrew build is GraalVM native-image and has a reflection bug that breaks *sending* (`IdentityKeyDeserializer ... no default constructor` → "Network failure"). Everything here points at `signal-cli-dist/bin/signal-cli` on **JDK 25+** (`JAVA_HOME=/opt/homebrew/opt/openjdk`). JDK 21 is too old (needs class-file v69).
- **ESPN date rollover — always fetch two days.** The default `/scoreboard` (no `?dates=`) returns only the *current UTC day's* slate. A match that finishes late in the previous UTC day drops off before the poller ever sees it complete — symptom is `poll ok — N final, 0 new` while a real final never sends (this missed Australia 2–0 Türkiye, event 760421, on 2026-06-14). Fix in `fetch_scoreboard()`: query yesterday + today explicitly and merge by id; `sent.json` dedup handles the overlap. Don't revert to a single-day fetch.
- **Rate limiting.** A burst of failed sends trips Signal's send limit (HTTP 429, timed cooldown, no captcha). Back off — don't hammer; each retry re-extends it.
- **launchd uses a stripped environment.** That's why `JAVA_HOME` is set both in `poll.py` and the plist. If you change paths, change both.
- **macOS load command.** `launchctl load` is deprecated (errors `5: Input/output error`). Use `sudo launchctl bootstrap system <plist>` / `bootout` to unload.
- **Claudia is optional + fail-safe.** The flirty write-up needs `anthropic_api_key` in `config.json`. Blank key, API down, or a write-up that fails the score guardrail → it silently sends the plain `Full time: …` line (logged as `CLAUDIA …`). The key reads from `config.json` (not the plist), so launchd's stripped env doesn't matter here. Model is `claude-haiku-4-5`, `temperature: 1.0` for variety; one short Haiku call per match (~104 matches = pennies). To turn Claudia off, blank the key.

## Operating (historical — nothing is scheduled now)

```bash
# Bring it back from the dead (2030). The plist is still in the repo:
sudo cp worldcup-scores-bot.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/worldcup-scores-bot.plist
# ...then delete sent.json, or it'll think all 104 of the 2026 matches are done.
# Also re-check the ESPN slug — `fifa.world` is the 2026 tournament.

# Watch it
tail -f poll.log
sudo launchctl list | grep worldcup        # 2nd column 0 = last run OK

# Test send WITHOUT spamming the group (set "dry_run": true in config.json, then)
/opt/homebrew/bin/python3 poll.py          # logs DRY_RUN lines, sends nothing

# Change cadence: edit StartInterval (seconds) in the plist, then reload:
sudo launchctl bootout system /Library/LaunchDaemons/worldcup-scores-bot.plist
sudo cp worldcup-scores-bot.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/worldcup-scores-bot.plist

# Reset dedup (re-send everything currently final): delete sent.json
```

## Knockout end-states (built 2026-06-15)

ESPN's `status.type.name` distinguishes how a finished match ended, and `match_result()` classifies it so a knockout is never reported as a draw:

| End-state | `status.type.name` | What `competitor.score` means | PK score |
|---|---|---|---|
| Regulation | `STATUS_FULL_TIME` | the result (a level score here = a real group-stage draw) | — |
| Extra time, no PKs | `STATUS_FINAL_AET` | the AET result (decisive, e.g. 2–1) | — |
| Penalty shootout | `STATUS_FINAL_PEN` | the level regulation+ET result (e.g. 1–1) | `competitor.shootoutScore` (int) |

Winner is `competitor.winner` (bool). The plain fallback carries the decisive clause (`(Paraguay win 5–4 on penalties)` / `(after extra time)`), and Claudia gets the outcome spelled out via `outcome_brief()` so her flirty write-up names the real winner. A **PK-specific guardrail** in `score_ok()` rejects any shootout write-up that doesn't name the winner + mention penalties + carry both PK scores — a level 1–1 could otherwise sail through as a "tie." Fixed the Paraguay–Germany "1-1 with Paraguay" tie bug.

## v2 backlog (not built, by design)

- Flag emoji per team (dropped to avoid a 48-country lookup).
- Goal alerts, live standings, a predictions game for the group.
