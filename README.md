# World Cup Scores Bot

A small AI agent that texted the final score of every 2026 FIFA World Cup match to a Signal group chat of three friends, each one written up by "Claudia," a flirty robot soccer commentator.

I built it with Claude Code in one evening after a friend asked what an AI agent actually is. The full story is on Substack: [I Built My Friend a Flirty Robot That Texts Him World Cup Scores](https://bronsonelliott.substack.com/p/i-built-my-friend-a-flirty-robot).

## How it did

It ran for the whole tournament, 2026-06-11 to 2026-07-19, and is now retired.

| | |
|---|---|
| Matches sent | **104 of 104**, zero duplicates |
| Send errors | **0** |
| Write-ups in Claudia's voice | 101 (the other 3 fell back to the plain score line) |
| Schedule | Checked every 30 minutes for 39 days |
| Cost | Pennies. One short Claude Haiku call per match |

The last message it sent, after the final:

> Spain just made Argentina sweat it out, boys—took extra time but La Roja sealed it 1-0 and sent Messi's crew home 🤖⚽ Sometimes the best things are worth the wait, if you know what I mean 💋
> MetLife Stadium • East Rutherford, New Jersey • USA

## How it works

Every 30 minutes, a macOS LaunchDaemon runs `poll.py`. Each run:

1. Pulls ESPN's public World Cup scoreboard. No API key needed.
2. Finds matches that have finished.
3. Skips any match it has already sent (tracked in `sent.json`).
4. Asks Claude Haiku to write the result up as Claudia.
5. Sends it to the Signal group with [`signal-cli`](https://github.com/AsamK/signal-cli).
6. Records the match as sent, but only after a confirmed successful send. A failed send just retries on the next poll.

That's what makes it an agent rather than a chatbot: nobody is at the keyboard. It wakes up on its own, checks the world, decides whether anything is worth saying, and acts.

## The guardrail: let it flirt, never let it get the score wrong

The score is the one thing the message has to get right. A creative AI is also the thing most likely to fumble it. So Claudia never gets the last word.

Every write-up goes through a plain, non-AI check (`score_ok()` in `poll.py`) before it sends:

- Both final scores must appear in the text.
- At least one team name must appear. Only one is required, so Claudia can still say "USMNT" or "La Roja."
- For a penalty shootout, the text must also name the winner, mention penalties, and include both shootout scores. Otherwise "what a 1–1 tie!" would pass the score check while being flat wrong.

If the write-up fails, or the API is down, or there's no API key, the bot sends the plain version instead. For a shootout it looks like this:

```
Full time: Team A 1–1 Team B (Team A win 5–4 on penalties)
```

Worst case, the message isn't funny. The score is never wrong.

## Lessons that cost real time

- **ESPN's default scoreboard only shows today's matches, in UTC.** A match that finishes late in the evening can drop off before the bot ever sees it as final. Always fetch yesterday and today and merge them.
- **Knockout matches aren't draws.** ESPN's `status.type.name` tells you how a match ended (`STATUS_FULL_TIME`, `STATUS_FINAL_AET`, `STATUS_FINAL_PEN`). Without checking it, a 1–1 penalty win reads as a tie. This one actually shipped: the group was told Paraguay–Germany ended 1–1, when Paraguay had won on penalties. Fixed the same day, along with the stricter shootout check above.
- **Use the JVM build of signal-cli, not Homebrew's.** The Homebrew build has a bug that silently breaks sending. The JVM build needs JDK 25 or newer.
- **Don't hammer Signal after failed sends.** A burst of retries trips a timed rate limit, and each retry extends it.
- **The first guardrail was too strict.** It demanded the exact team name ESPN uses, so a perfectly good "USMNT" write-up got thrown out. Fence the number, give the names some room.

## Running it yourself

This was built for one Mac, one group chat and one tournament, so expect to adapt it.

**You'll need:** macOS, Python 3 (standard library only), JDK 25+, the JVM build of signal-cli unpacked into `signal-cli-dist/`, a Signal account, and optionally an Anthropic API key for Claudia.

1. Copy `config.example.json` to `config.json` and fill in your Signal number, the group ID (from `signal-cli listGroups`), and your API key. Leave the key blank to send plain score lines only.
2. Link the Mac as a secondary Signal device with `./link-signal.sh`. It opens a QR code to scan from your phone.
3. Test it without sending anything: set `"dry_run": true` in `config.json` and run `python3 poll.py`.
4. In `worldcup-scores-bot.plist`, replace `YOUR_USERNAME` and the folder path with your own. Then install it:

```bash
sudo cp worldcup-scores-bot.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/worldcup-scores-bot.plist
```

For a different competition, change the ESPN league slug (`fifa.world`) in `poll.py` and delete `sent.json`.

## Files

| File | What it does |
|---|---|
| `poll.py` | The whole bot |
| `config.example.json` | Template for `config.json` (git-ignored, holds your number and API key) |
| `worldcup-scores-bot.plist` | The LaunchDaemon that runs it every 30 minutes |
| `link-signal.sh` | Links the Mac to your Signal account |
| `sample-espn-scoreboard.json` | A real ESPN response, handy for testing |
| `CLAUDE.md` | Working notes for Claude Code |
