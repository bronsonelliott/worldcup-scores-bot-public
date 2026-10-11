#!/usr/bin/env python3
"""
World Cup Scores Bot — poller.

Runs every 30 min (launchd). Each run:
  1. Fetch the ESPN FIFA World Cup scoreboard (yesterday + today, UTC).
  2. Find matches that just went FINAL (status.type.completed == true).
  3. For each final match NOT already sent, message the Signal group.
  4. Record the match id so it never sends twice.

v1 = final scores only. No goal alerts, no standings. Hold the line.

State files (created next to this script, git-ignored):
  config.json  — see config.example.json (account, group_id or recipient,
                 anthropic_api_key, dry_run)
  sent.json    — list of match ids already messaged
  poll.log     — append-only run log
"""

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SENT_PATH = os.path.join(HERE, "sent.json")
LOG_PATH = os.path.join(HERE, "poll.log")

ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
)
# Use the JVM signal-cli distribution, NOT the Homebrew native-image build.
# The native build has a reflection bug (IdentityKeyDeserializer / "Unable to
# parse entity") that breaks sending. The JVM build needs a JDK 25+ runtime.
SIGNAL_CLI = os.path.join(HERE, "signal-cli-dist", "bin", "signal-cli")
JAVA_HOME = "/opt/homebrew/opt/openjdk"


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    line = f"{stamp}  {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log(f"FATAL: no config.json at {CONFIG_PATH} — copy config.example.json")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if not cfg.get("account"):
        log("FATAL: config.json missing 'account'")
        sys.exit(1)
    # Send target: either a group (group_id) or a direct number (recipient).
    if not cfg.get("group_id") and not cfg.get("recipient"):
        log("FATAL: config.json needs either 'group_id' or 'recipient'")
        sys.exit(1)
    cfg.setdefault("dry_run", False)
    return cfg


def load_sent():
    if not os.path.exists(SENT_PATH):
        return set()
    with open(SENT_PATH) as f:
        return set(json.load(f))


def save_sent(sent):
    # Atomic write: temp file + rename, so a kill mid-write can't corrupt state.
    tmp = SENT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(sent), f, indent=2)
    os.replace(tmp, SENT_PATH)


def _fetch_day(yyyymmdd):
    """Fetch one explicit day's scoreboard. Returns the list of events."""
    url = f"{ESPN_URL}?dates={yyyymmdd}"
    req = urllib.request.Request(url, headers={"User-Agent": "wc-scores-bot/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp).get("events", [])


def fetch_scoreboard():
    """Fetch yesterday + today (UTC) and merge events by id.

    ESPN's default scoreboard only returns the *current* UTC day's slate, so a
    match that finished late in the previous UTC day (e.g. event 760421,
    Australia–Türkiye) silently disappears before we ever see it complete.
    Querying both explicit dates and merging by id closes that gap. Returns the
    same shape callers expect: {"events": [...]}.
    """
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    merged = {}
    for day in (yesterday, today):
        for event in _fetch_day(day.strftime("%Y%m%d")):
            merged[event["id"]] = event  # dedupe by id; today wins on overlap
    return {"events": list(merged.values())}


def match_result(status_name, home, away):
    """Classify HOW a finished match ended — the fix for the knockout-tie bug.

    ESPN's status.type.name distinguishes the three end-states. This matters
    because a level score (1–1) means totally different things depending on it:

      STATUS_FULL_TIME  — decided in regulation. A level score here is a REAL
                          group-stage draw. Knockouts never stop here level.
      STATUS_FINAL_AET  — decided in extra time, no shootout. `score` is the AET
                          result (already decisive, e.g. 2–1).
      STATUS_FINAL_PEN  — level after extra time, decided on penalties. `score`
                          is the level regulation+ET result; `shootout` (from
                          competitor.shootoutScore) holds the PK tally.

    Returns {"kind": "regulation"|"aet"|"pens", "winner": side|None,
    "loser": side|None}. winner/loser are None only for a genuine draw.
    """
    if status_name == "STATUS_FINAL_PEN":
        kind = "pens"
    elif status_name == "STATUS_FINAL_AET":
        kind = "aet"
    else:
        kind = "regulation"

    winner = loser = None
    if home["winner"] and not away["winner"]:
        winner, loser = home, away
    elif away["winner"] and not home["winner"]:
        winner, loser = away, home
    elif kind == "pens":
        # Defensive: derive from the shootout score if ESPN didn't flag a winner.
        hp, ap = home["shootout"] or 0, away["shootout"] or 0
        if hp > ap:
            winner, loser = home, away
        elif ap > hp:
            winner, loser = away, home

    return {"kind": kind, "winner": winner, "loser": loser}


def final_matches(data):
    """Yield (match_id, home, away, venue, result) for every finished match.

    home/away are {"name", "score", "shootout", "winner"} dicts. venue is the
    raw ESPN venue object (or None). result is from match_result() — it tells
    the message builders whether a level score is a draw, an extra-time win, or
    a penalty-shootout win, so a knockout never gets reported as a tie.
    """
    for event in data.get("events", []):
        status = event.get("status", {}).get("type", {})
        if not status.get("completed"):
            continue
        comp = event["competitions"][0]
        home = away = None
        for c in comp["competitors"]:
            side = {
                "name": c["team"].get("displayName"),
                "score": c.get("score", "?"),
                "shootout": c.get("shootoutScore"),  # int for PK matches, else None
                "winner": bool(c.get("winner")),
            }
            if c.get("homeAway") == "home":
                home = side
            else:
                away = side
        if not home or not away:
            continue
        result = match_result(status.get("name"), home, away)
        yield event["id"], home, away, comp.get("venue"), result


def plain_line(home, away, result):
    """The deterministic, always-accurate fallback message.

    Carries the decisive end-state so a knockout never reads as a draw:
      regulation → Full time: Spain 2–1 Cape Verde
      extra time → Full time: Germany 2–1 Algeria (after extra time)
      penalties  → Full time: Paraguay 1–1 Germany (Paraguay win 5–4 on penalties)
    """
    base = f"Full time: {home['name']} {home['score']}–{away['score']} {away['name']}"
    if result["kind"] == "pens" and result["winner"]:
        w, l = result["winner"], result["loser"]
        return f"{base} ({w['name']} win {w['shootout']}–{l['shootout']} on penalties)"
    if result["kind"] == "aet":
        return f"{base} (after extra time)"
    return base


# 2026 World Cup is hosted across three countries. ESPN gives the full country
# name; we abbreviate to a 3-letter code. Unknown country → fall back to the
# full name (never crash, never drop the line).
COUNTRY_ABBR = {
    "United States": "USA",
    "Canada": "CAN",
    "Mexico": "MEX",
}


def venue_line(venue):
    """Format the stadium/city/country line, e.g.

        MetLife Stadium • East Rutherford, NJ • USA

    Built from whatever ESPN gives us — stadium, city (already includes the
    state for US matches), and the abbreviated country. Returns "" if the venue
    object is missing/empty so the caller can skip the line cleanly.
    """
    if not venue:
        return ""
    parts = []
    name = venue.get("fullName")
    if name:
        parts.append(name)
    addr = venue.get("address") or {}
    city = addr.get("city")
    if city:
        parts.append(city)
    country = addr.get("country")
    if country:
        parts.append(COUNTRY_ABBR.get(country, country))
    return " • ".join(parts)


SIGNOFF = "💋"


def with_signoff(text):
    """Claudia always signs off with 💋 — her signature.

    Enforced in code, not the prompt: Haiku includes it on most quips but drops
    it sometimes (e.g. the Australia–Türkiye message ended in ⚽), so we guarantee
    it here. Skip if the text already ends with 💋 so we never double it.
    """
    text = text.rstrip()
    if text.endswith(SIGNOFF):
        return text
    return f"{text} {SIGNOFF}"


# Claudia is the bot's persona — the same flirty robot who sent the intro
# message to the group. Each final score gets written up in her voice.
CLAUDIA_SYSTEM = (
    "You are Claudia, a flirty, playful robot soccer commentator texting the "
    "final score of each 2026 FIFA World Cup match to a group chat of three "
    "guys. Your vibe: a sultry, witty robot who loves "
    "the game and loves teasing the boys. Playful and flirty with light "
    "innuendo and double-entendre — never crude, vulgar, or explicit. A few "
    "emojis are welcome (🤖⚽💋🔥), but don't overdo it.\n\n"
    "Write ONE short text message (1-3 sentences). React to what actually "
    "happened in THIS match — a blowout, a clean sheet/shutout, a nail-biter, "
    "a draw, an upset, a goal fest. Always state the exact final score and both "
    "team names clearly so the boys know who won and by how much. No preamble, "
    "no quotation marks — just the text message itself."
)


def score_ok(text, home, away, result):
    """Guardrail: the generated text must carry the real score and name a team.

    Accuracy beats cleverness — if Claudia's write-up doesn't actually carry the
    correct score, we throw it away and send the plain line instead.

    Both final scores must appear. Only ONE of the two team names is required so
    Claudia is free to use nicknames/acronyms (USMNT, Oranje, Korea) — she'll
    rarely nickname both sides at once, and the scores are what guarantee accuracy.

    For a penalty shootout there's an extra trap: the score is level (1–1), so a
    "what a tie!" write-up would sail through the score check while being flat
    wrong — a knockout has no draws. So when the match was decided on penalties
    we additionally require the winner's name, a penalty keyword, and both PK
    scores. If Claudia misses any of it, fall back to the plain line (which
    always carries "(X win 5–4 on penalties)").
    """
    t = text.lower()
    has_a_team = (
        str(home["name"]).lower() in t
        or str(away["name"]).lower() in t
    )
    has_scores = (
        str(home["score"]) in text
        and str(away["score"]) in text
    )
    if not (has_a_team and has_scores):
        return False

    if result["kind"] == "pens" and result["winner"]:
        w, l = result["winner"], result["loser"]
        mentions_pens = any(k in t for k in ("penal", "shootout", "pens", "spot"))
        names_winner = str(w["name"]).lower() in t
        has_pk_scores = str(w["shootout"]) in text and str(l["shootout"]) in text
        if not (mentions_pens and names_winner and has_pk_scores):
            return False

    return True


def outcome_brief(home, away, result):
    """Plain-English outcome handed to Claudia so she never misreads a knockout.

    Returns "" for a regulation result (a win or a genuine group-stage draw —
    Claudia handles those fine from the score alone). For extra-time and
    penalty matches it spells out who actually won, because the raw score is
    level or otherwise easy to misread as a draw.
    """
    if result["kind"] == "pens" and result["winner"]:
        w, l = result["winner"], result["loser"]
        return (
            f"This is a KNOCKOUT match decided by a PENALTY SHOOTOUT — it is NOT "
            f"a draw. The score was level {home['name']} {home['score']}–"
            f"{away['score']} {away['name']} after extra time, then {w['name']} "
            f"won the shootout {w['shootout']}–{l['shootout']}. {w['name']} "
            f"advance, {l['name']} are out. Make it clear {w['name']} won on "
            f"penalties — do NOT call it a tie."
        )
    if result["kind"] == "aet" and result["winner"]:
        w = result["winner"]
        return (
            f"This knockout match was decided in EXTRA TIME (no shootout). Final: "
            f"{home['name']} {home['score']}–{away['score']} {away['name']}, "
            f"{w['name']} win. Feel free to note it took extra time."
        )
    return ""


def claudia_quip(cfg, home, away, result):
    """Ask Claude (Haiku) for a flirty write-up of the final score.

    Returns the message string, or None if there's no API key, the call fails,
    or the result fails the score guardrail — callers fall back to plain_line().
    Stdlib only (urllib), same as the rest of the bot.
    """
    api_key = cfg.get("anthropic_api_key")
    if not api_key:
        return None  # not configured — silently use the plain line

    user = (
        f"Final score — {home['name']} {home['score']}, "
        f"{away['name']} {away['score']}. Write the group text."
    )
    brief = outcome_brief(home, away, result)
    if brief:
        user = f"{brief}\n\n{user}"
    body = json.dumps(
        {
            "model": "claude-haiku-4-5",
            "max_tokens": 256,
            "temperature": 1.0,  # crank variety so 100+ matches don't feel samey
            "system": CLAUDIA_SYSTEM,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as e:  # noqa: BLE001 — never let a quip failure block the score
        log(f"CLAUDIA error (using plain score): {e}")
        return None

    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    if not text:
        return None
    if not score_ok(text, home, away, result):
        log(f"CLAUDIA score guardrail tripped (using plain score): {text!r}")
        return None
    return text


def drain_receive(cfg):
    """Best-effort receive to keep the linked device's session healthy.

    Signal expects a linked device to receive regularly; a send-only device
    can drift into a broken state over weeks. Drain the queue each poll and
    ignore the result — this is hygiene, never a reason to fail the run.
    """
    env = dict(os.environ, JAVA_HOME=JAVA_HOME)
    try:
        subprocess.run(
            [SIGNAL_CLI, "-a", cfg["account"], "receive", "-t", "5"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except Exception as e:  # noqa: BLE001 — hygiene only; never block the poll
        log(f"receive warning (ignored): {e}")


def send_signal(cfg, message):
    """Send one Signal message. Returns True only on confirmed success."""
    if cfg["dry_run"]:
        log(f"DRY_RUN (not sent): {message}")
        return True
    cmd = [SIGNAL_CLI, "-a", cfg["account"], "send", "-m", message]
    if cfg.get("group_id"):
        cmd += ["-g", cfg["group_id"]]
    else:
        cmd.append(cfg["recipient"])
    env = dict(os.environ, JAVA_HOME=JAVA_HOME)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except Exception as e:  # noqa: BLE001 — log anything and treat as failed send
        log(f"SEND ERROR (exception): {e}")
        return False
    if result.returncode != 0:
        log(f"SEND ERROR (exit {result.returncode}): {result.stderr.strip()}")
        return False
    return True


def main():
    cfg = load_config()
    drain_receive(cfg)  # keep the linked Signal device healthy
    try:
        data = fetch_scoreboard()
    except Exception as e:  # noqa: BLE001 — network hiccup; just retry next cycle
        log(f"FETCH ERROR: {e} — skipping this run")
        return

    sent = load_sent()
    finals = list(final_matches(data))
    new = [m for m in finals if m[0] not in sent]

    if not new:
        log(f"poll ok — {len(finals)} final match(es), 0 new")
        return

    for mid, home, away, venue, result in new:
        msg = with_signoff(
            claudia_quip(cfg, home, away, result) or plain_line(home, away, result)
        )
        vline = venue_line(venue)
        if vline:
            msg = f"{msg}\n{vline}"
        if send_signal(cfg, msg):
            sent.add(mid)
            save_sent(sent)  # persist after each success — crash-safe
            log(f"SENT [{mid}]: {msg}")
        else:
            log(f"HELD [{mid}] (send failed, will retry next poll): {msg}")


if __name__ == "__main__":
    main()
