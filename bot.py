from datetime import datetime, timezone, timedelta
import json
import os
import re
import statistics
import time
import xml.etree.ElementTree as ET
import requests
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types

# ================= CONFIGURAZIONE DA GITHUB SECRETS =================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemma-4-26b-a4b-it")
STATE_FILE = "tracked_bets.json"
RUN_ONCE = os.environ.get("RUN_ONCE", "true").lower() in ("1", "true", "yes")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "1800"))

# Limiti Gemini (al minuto): nessun taglio per numero di partite, attesa/retry su 429
GEMINI_DELAY_SECONDS = float(os.environ.get("GEMINI_DELAY_SECONDS", "4.0"))
MAX_GEMINI_CALLS_PER_CYCLE = int(os.environ.get("MAX_GEMINI_CALLS_PER_CYCLE", "9999"))

# Limiti The Odds API (protezione plafond mensile)
ODDS_API_MIN_REMAINING = int(os.environ.get("ODDS_API_MIN_REMAINING", "25"))

# Finestra notturna: include partite fino alle 05:00 locali del giorno successivo
NIGHT_CUTOFF_HOUR = int(os.environ.get("NIGHT_CUTOFF_HOUR", "5"))

# Soglia minima di variazione quota per rianalizzare un match precedentemente rifiutato o revocato
REANALYZE_ODDS_DELTA = float(os.environ.get("REANALYZE_ODDS_DELTA", "0.08"))

# Filtri quantitativi di quota e vantaggio rispetto al consenso di mercato (%)
MIN_ODDS_THRESHOLD = float(os.environ.get("MIN_ODDS_THRESHOLD", "1.35"))
MIN_MARKET_EDGE_PCT = float(os.environ.get("MIN_MARKET_EDGE_PCT", "1.5"))
# ====================================================================

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
gemini_calls_this_cycle = 0
odds_remaining_requests = None
odds_quota_exhausted = False
_rss_cache = {}

LEAGUE_MAPPING = {
    "SA": "soccer_italy_serie_a",
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CLI": "soccer_usa_mls",
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "TE": "soccer_uefa_europa_conference_league",
}

ALWAYS_ACTIVE_SPORTS = [
    "basketball_nba",
    "icehockey_nhl",
]

RSS_FEEDS_BY_SPORT = {
    "soccer": [
        "https://www.espn.com/espn/rss/soccer/news",
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
        "https://www.skysports.com/rss/0,20514,11095,00.xml",
    ],
    "nba": [
        "https://www.espn.com/espn/rss/nba/news",
    ],
    "nhl": [
        "https://www.espn.com/espn/rss/nhl/news",
    ],
}

TEAM_ALIASES = {
    "paris saint germain": ["psg", "paris"],
    "inter milan": ["inter", "internazionale"],
    "ac milan": ["milan"],
    "atletico madrid": ["atletico"],
    "bayern munich": ["bayern"],
    "borussia dortmund": ["dortmund"],
    "manchester united": ["man utd", "manchester united"],
    "manchester city": ["man city", "manchester city"],
    "tottenham hotspur": ["tottenham", "spurs"],
    "wolverhampton wanderers": ["wolves", "wolverhampton"],
    "newcastle united": ["newcastle"],
    "west ham united": ["west ham"],
    "brighton and hove albion": ["brighton"],
    "nottingham forest": ["forest", "nottingham"],
    "real sociedad": ["sociedad"],
    "athletic bilbao": ["bilbao", "athletic club"],
    "los angeles lakers": ["lakers"],
    "los angeles clippers": ["clippers"],
    "golden state warriors": ["warriors"],
    "new york knicks": ["knicks"],
    "brooklyn nets": ["nets"],
    "boston celtics": ["celtics"],
    "new york rangers": ["rangers"],
    "new york islanders": ["islanders"],
    "toronto maple leafs": ["maple leafs", "leafs"],
    "vegas golden knights": ["golden knights", "vegas"],
}

GENERIC_TEAM_WORDS = {
    "fc", "cf", "ac", "as", "ss", "ssc", "us", "cd", "ud", "rc", "rcd",
    "club", "calcio", "sport", "united", "city", "real", "inter", "athletic",
    "atletico", "saint", "de", "la", "el", "los", "las", "new", "york",
}

INJURY_KEYWORDS = (
    "injur", "out", "questionable", "doubtful", "ruled out", "suspend",
    "ban", "hamstring", "knee", "ankle", "concussion", "illness",
    "lineup", "starting", "miss", "return", "fitness", "sidelined",
)


def safe_float(val, default=0.0):
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace("%", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return default


def is_draw_outcome(name: str) -> bool:
    if not name:
        return False
    return name.strip().lower() in ("draw", "pareggio", "x", "tie")


def now_rome_iso() -> str:
    return datetime.now(ZoneInfo("Europe/Rome")).isoformat(timespec="seconds")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"⚠️ Errore nel caricamento dello stato: {e}")
            return {}
    return {}


def save_state(state):
    try:
        tmp_file = f"{STATE_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        print(f"⚠️ Errore nel salvataggio dello stato: {e}")


def get_existing_key(tracked_bets, home, away):
    target = f"{home}vs{away}".replace(" ", "").replace("_", "").lower()
    for key in tracked_bets.keys():
        normalized_key = key.replace(" ", "").replace("_", "").lower()
        if normalized_key == target:
            return key
    return f"{home}vs{away}".replace(" ", "")


def send_discord_alert(message):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK_URL non configurato. Messaggio non inviato.")
        return False

    safe_message = message if len(message) <= 1990 else message[:1987] + "..."
    payload = {"content": safe_message}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code in (200, 204):
            print("📲 Notifica inviata con successo su Discord!")
            return True
        else:
            print(f"❌ Errore Discord (Codice {response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"❌ Errore di rete Discord: {e}")
        return False


def calculate_stake(prob):
    if prob >= 82.0:
        return "1/2 unità (0.50)"
    elif prob >= 76.0:
        return "1/3 unità (0.33)"
    elif prob >= 70.0:
        return "1/4 unità (0.25)"
    else:
        return "1/5 unità (0.20)"


def format_match_time(iso_time_str):
    try:
        dt_utc = datetime.fromisoformat(iso_time_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("Europe/Rome"))
        return dt_local.strftime("%d/%m/%Y alle %H:%M")
    except Exception:
        return iso_time_str


def extract_json_dict(text: str) -> dict:
    if not text:
        return {}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}


def get_h2h_market_outcomes(bookmaker):
    if not isinstance(bookmaker, dict):
        return []
    for mkt in bookmaker.get("markets", []):
        if mkt.get("key") == "h2h" and isinstance(mkt.get("outcomes"), list) and len(mkt["outcomes"]) >= 2:
            return mkt["outcomes"]
    return []


def compute_no_vig_probs_for_outcomes(outcomes):
    """Calcola la probabilità no-vig (%) per ciascun esito di un mercato h2h."""
    valid = []
    for out in outcomes:
        name = out.get("name")
        price = safe_float(out.get("price"), 0.0)
        if name and price > 1.0:
            valid.append((name, price, 1.0 / price))

    if len(valid) < 2:
        return {}

    total_implied = sum(item[2] for item in valid)
    if total_implied <= 0:
        return {}

    no_vig = {}
    for name, _, raw_p in valid:
        no_vig[name] = min(round((raw_p / total_implied) * 100.0, 1), 95.0)
    return no_vig


def find_best_or_valid_bookmaker(bookmakers):
    """Restituisce il bookmaker valido con probabilità no-vig per ciascun esito."""
    if not isinstance(bookmakers, list):
        return None, [], {}
    for bm in bookmakers:
        outcomes = get_h2h_market_outcomes(bm)
        if outcomes:
            no_vig_map = compute_no_vig_probs_for_outcomes(outcomes)
            if len(no_vig_map) >= 2:
                return bm.get("title", "Bookmaker"), outcomes, no_vig_map
    return None, [], {}


def find_outcome_price_across_bookmakers(bookmakers, outcome_name, preferred_book=None):
    if not isinstance(bookmakers, list) or not outcome_name:
        return None
    target_low = outcome_name.strip().lower()

    if preferred_book:
        for bm in bookmakers:
            if bm.get("title") == preferred_book:
                for out in get_h2h_market_outcomes(bm):
                    if (out.get("name") or "").strip().lower() == target_low:
                        price = safe_float(out.get("price"), 0.0)
                        if price > 0:
                            return price

    for bm in bookmakers:
        for out in get_h2h_market_outcomes(bm):
            if (out.get("name") or "").strip().lower() == target_low:
                price = safe_float(out.get("price"), 0.0)
                if price > 0:
                    return price
    return None


def compute_market_consensus_for_outcome(bookmakers, outcome_name):
    """Calcola quota mediana, quota media e probabilità implicita media del mercato."""
    if not isinstance(bookmakers, list) or not outcome_name:
        return {}
    target_low = outcome_name.strip().lower()
    prices = []
    implied_probs = []

    for bm in bookmakers:
        for out in get_h2h_market_outcomes(bm):
            if (out.get("name") or "").strip().lower() == target_low:
                price = safe_float(out.get("price"), 0.0)
                if price > 1.0:
                    prices.append(price)
                    implied_probs.append((1.0 / price) * 100.0)

    if not prices:
        return {}

    return {
        "books_count": len(prices),
        "median_price": round(statistics.median(prices), 3),
        "avg_price": round(statistics.mean(prices), 3),
        "avg_implied_prob": round(statistics.mean(implied_probs), 2),
    }


def update_odds_quota_from_headers(response):
    global odds_remaining_requests, odds_quota_exhausted
    rem_header = response.headers.get("x-requests-remaining")
    used_header = response.headers.get("x-requests-used")
    if rem_header is not None:
        try:
            odds_remaining_requests = int(float(rem_header))
            print(f"📉 Odds API quota residua: {odds_remaining_requests} (usate: {used_header or 'N/D'})")
            if odds_remaining_requests <= ODDS_API_MIN_REMAINING:
                print(
                    f"🛑 Soglia di sicurezza Odds API raggiunta ({odds_remaining_requests} <= {ODDS_API_MIN_REMAINING}). "
                    "Blocco ulteriori chiamate Odds API per preservare il limite mensile."
                )
                odds_quota_exhausted = True
        except ValueError:
            pass


def _sport_category(sport_key: str) -> str:
    s = (sport_key or "").lower()
    if "nba" in s or "basketball" in s:
        return "nba"
    if "nhl" in s or "icehockey" in s:
        return "nhl"
    return "soccer"


def _build_team_terms(team_name: str) -> list[str]:
    clean = re.sub(r"[^a-z0-9\s]", " ", (team_name or "").lower()).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        return []

    terms = {clean}
    if clean in TEAM_ALIASES:
        terms.update(TEAM_ALIASES[clean])

    tokens = [w for w in clean.split() if len(w) > 3 and w not in GENERIC_TEAM_WORDS]
    terms.update(tokens)
    return sorted(terms, key=len, reverse=True)


def _load_feed_items(feed_url: str) -> list[tuple[str, str]]:
    if feed_url in _rss_cache:
        return _rss_cache[feed_url]

    items = []
    try:
        resp = requests.get(
            feed_url,
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; OddsBot/1.0)"},
        )
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:30]:
                title = (item.findtext("title") or "").strip()
                desc = re.sub(r"<[^>]+>", " ", (item.findtext("description") or "")).strip()
                if title:
                    items.append((title, desc[:220]))
    except Exception:
        pass

    _rss_cache[feed_url] = items
    return items


def fetch_free_news_snippets(home: str, away: str, sport: str = "soccer", max_items: int = 4) -> str:
    category = _sport_category(sport)
    feeds = RSS_FEEDS_BY_SPORT.get(category, RSS_FEEDS_BY_SPORT["soccer"])

    home_terms = _build_team_terms(home)
    away_terms = _build_team_terms(away)
    all_terms = home_terms + away_terms

    scored = []
    seen_titles = set()

    for feed_url in feeds:
        for title, desc in _load_feed_items(feed_url):
            if title in seen_titles:
                continue
            seen_titles.add(title)

            blob = f" {title.lower()} {desc.lower()} "
            team_hits = sum(1 for t in all_terms if re.search(rf"\b{re.escape(t)}\b", blob))
            if team_hits == 0:
                continue

            injury_hits = sum(1 for kw in INJURY_KEYWORDS if kw in blob)
            score = (team_hits * 10) + (injury_hits * 4)
            snippet = f"{title} ({desc})" if desc and injury_hits > 0 else title
            scored.append((score, snippet))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_snippets = [s for _, s in scored[:max_items]]

    if not top_snippets:
        return f"Nessuna notizia specifica trovata nei feed RSS per {home} o {away}."
    return " | ".join(top_snippets)


def _sport_specific_guidance(sport_key: str) -> str:
    cat = _sport_category(sport_key)
    if cat == "nba":
        return (
            "Criteri specifici NBA: verifica back-to-back, trasferte consecutive, gestione riposo/infortuni "
            "(questionable/out di stelle o titolari) e ritmo/punti segnati-subiti."
        )
    if cat == "nhl":
        return (
            "Criteri specifici NHL: considera back-to-back, trasferte, portieri titolari/backup probabile, "
            "equilibrio naturale del ghiaccio e varianza elevata."
        )
    return (
        "Criteri specifici Calcio (1X2): considera importanza della competizione (campionato vs coppa), "
        "possibile turnover, assenze chiave (portiere, centravanti, difensori centrali) e peso del fattore campo."
    )


def call_gemini_with_retry(prompt: str, context_label: str) -> str:
    global gemini_calls_this_cycle
    if not client:
        return ""

    if gemini_calls_this_cycle >= MAX_GEMINI_CALLS_PER_CYCLE:
        print(f"⚠️ Raggiunto limite configurato di {MAX_GEMINI_CALLS_PER_CYCLE} chiamate su {context_label}.")
        return ""

    for attempt in range(3):
        try:
            time.sleep(GEMINI_DELAY_SECONDS)
            gemini_calls_this_cycle += 1
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.15,
                ),
            )
            return getattr(response, "text", None) or ""
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                wait_s = 65 if attempt == 0 else 75
                print(f"⏳ Limite al minuto/richieste (429) su {context_label} (tentativo {attempt + 1}/3): attendo {wait_s}s...")
                time.sleep(wait_s)
                continue
            print(f"⚠️ Errore Gemini su {context_label}: {e}")
            return ""
    print(f"⚠️ Limite 429 non superato dopo 3 tentativi su {context_label}; proseguo con i match successivi.")
    return ""


def analyze_match_1x2_with_gemini(sport, home, away, match_time_local, standard_candidate, news_context=""):
    if not client:
        print("⚠️ GEMINI_API_KEY non configurato.")
        return {}

    if standard_candidate["is_draw"]:
        role_label = "Pareggio (X)"
    elif standard_candidate["is_away"]:
        role_label = "Trasferta"
    else:
        role_label = "Casa"

    sport_guide = _sport_specific_guidance(sport)
    consensus = standard_candidate.get("consensus", {})
    median_p = consensus.get("median_price", standard_candidate["quota"])
    edge_pct = standard_candidate.get("market_edge_pct", 0.0)

    prompt = f"""
Sei un analista sportivo specializzato ESCLUSIVAMENTE sul mercato 1X2 (esito finale) per lo sport: {sport}.
Data/ora locale partita: {match_time_local}.
Partita: {home} (Casa) vs {away} (Trasferta).
{sport_guide}

Candidatura 1X2 da valutare:
- Esito proposto: {standard_candidate['esito']} ({role_label})
- Quota reale: {standard_candidate['quota']} ({standard_candidate['book']}) | Mediana mercato: {median_p} | Edge quota vs mercato: {edge_pct}%
- Probabilità base no-vig da quota: {standard_candidate['base_prob']}%
- Soglia minima richiesta per approvare: {standard_candidate['min_prob']}%

Notizie recenti da feed RSS:
{news_context}

Analizza con rigore e prudenza (NON inventare dati: se non hai certezza su un dato, indicalo chiaramente e abbassa `certezza_dati`):
1. Classifica attuale e ultime 5 partite di {home} e {away}.
2. Ultimi scontri diretti (H2H) tra {home} e {away}.
3. Assenze, squalifiche o infortuni rilevanti per {home} e {away}.
4. Gol o punti fatti e subiti (media/totali) della squadra di casa ({home}) e della squadra ospite ({away}).
5. Fattore campo e fattore stanchezza (trasferte lunghe, back-to-back o 3 partite in 4-5 giorni).

Imposta `approvata: true` SOLO se `probabilita_finale` >= {standard_candidate['min_prob']}% e i fattori analizzati non sconsigliano la giocata.

Rispondi ESCLUSIVAMENTE con un oggetto JSON valido:
{{
  "certezza_dati": "alta",
  "stats": {{
    "sconfitte_ultime_5_casa": 0,
    "sconfitte_ultime_5_trasferta": 0,
    "tre_in_4_5_giorni_casa": false,
    "tre_in_4_5_giorni_trasferta": false,
    "classifica_e_forma": "Sintesi classifica e ultime 5 partite",
    "scontri_diretti": "Sintesi ultimi scontri diretti",
    "gol_o_punti_fatti_subiti": "Casa GF/GS o PF/PS vs Trasferta GF/GS o PF/PS",
    "fattore_campo_e_stanchezza": "Fattore campo, trasferte e carico partite negli ultimi 5 giorni"
  }},
  "assenze_infortuni": "Infortuni, squalifiche o assenze note o da verificare",
  "standard_1x2": {{
    "approvata": true,
    "probabilita_finale": 68.0,
    "motivazione": "Spiegazione chiara del perché e del come è stata fatta la scelta 1X2"
  }}
}}
""".strip()

    raw_text = call_gemini_with_retry(prompt, f"{home} vs {away}")
    if not raw_text:
        return {}

    print(f"🤖 [{sport}] {GEMINI_MODEL} su {home} vs {away}: {raw_text[:160].replace(chr(10), ' ')}")
    parsed = extract_json_dict(raw_text)
    if not parsed:
        print(f"⚠️ [{sport}] JSON non valido per {home} vs {away}")
    return parsed


def passes_hard_stats_filter(stats, is_away_team, is_draw=False, certainty="media"):
    cert_low = (certainty or "media").strip().lower()
    if cert_low in ("bassa", "low"):
        return False, "Certezza dei dati troppo bassa per confermare"

    if not isinstance(stats, dict) or not stats:
        return False, "Statistiche mancanti o non valide"
    try:
        if is_draw:
            home_losses = int(safe_float(stats.get("sconfitte_ultime_5_casa", 0), 0))
            away_losses = int(safe_float(stats.get("sconfitte_ultime_5_trasferta", 0), 0))
            home_heavy = bool(stats.get("tre_in_4_5_giorni_casa", False))
            away_heavy = bool(stats.get("tre_in_4_5_giorni_trasferta", False))
            if home_heavy or away_heavy:
                return False, "Stanchezza / carico ravvicinato su una delle squadre"
            if home_losses >= 4 and away_losses >= 4:
                return False, "Entrambe le squadre in forte crisi di forma"
            return True, "OK"

        if is_away_team:
            losses = int(safe_float(stats.get("sconfitte_ultime_5_trasferta", 0), 0))
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_trasferta", False))
            ctx = "trasferta"
        else:
            losses = int(safe_float(stats.get("sconfitte_ultime_5_casa", 0), 0))
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_casa", False))
            ctx = "casa"

        if heavy_schedule:
            return False, f"Stanchezza / carico ravvicinato (3 partite in 4-5 giorni) in {ctx}"
        if losses >= 3:
            return False, f"Forma negativa ({losses} sconfitte nelle ultime 5)"
        return True, "OK"
    except Exception as e:
        return False, f"Errore verifica stats: {e}"


def extract_standard_candidate(match, home, away):
    bookmakers = match.get("bookmakers", [])
    book, outcomes, no_vig_probs = find_best_or_valid_bookmaker(bookmakers)
    if not outcomes or len(no_vig_probs) < 2:
        return None

    sorted_p = sorted(no_vig_probs.values(), reverse=True)
    if (sorted_p[0] - sorted_p[1]) < 12.0:
        return None

    for out in outcomes:
        name = out.get("name")
        if name not in no_vig_probs:
            continue
        price = safe_float(out.get("price"), 0.0)
        if price <= MIN_ODDS_THRESHOLD:
            continue

        consensus = compute_market_consensus_for_outcome(bookmakers, name)
        median_price = consensus.get("median_price", price)
        market_edge_pct = round(((price - median_price) / median_price) * 100.0, 2) if median_price > 0 else 0.0

        if consensus.get("books_count", 0) >= 2 and market_edge_pct < MIN_MARKET_EDGE_PCT:
            continue

        base_prob = no_vig_probs[name]
        draw_flag = is_draw_outcome(name)
        is_away_team = (not draw_flag) and (name.strip().lower() == away.strip().lower())
        min_prob_required = 70.0 if is_away_team else 65.0

        if base_prob >= min_prob_required:
            return {
                "esito": name,
                "is_away": is_away_team,
                "is_draw": draw_flag,
                "quota": price,
                "book": book,
                "base_prob": base_prob,
                "min_prob": min_prob_required,
                "market_edge_pct": market_edge_pct,
                "consensus": consensus,
            }

    return None


def should_skip_candidate_from_state(tracked_bets, match_key, current_quota):
    entry = tracked_bets.get(match_key)
    if not isinstance(entry, dict):
        return False

    status = entry.get("status")
    if status == "active":
        return True

    if status in ("rejected", "revoked"):
        ref_quota = safe_float(entry.get("revoked_price") if status == "revoked" else entry.get("quota"), 0.0)
        if ref_quota > 0 and abs(current_quota - ref_quota) < REANALYZE_ODDS_DELTA:
            return True

    return False


def update_closing_line_tracking(tracked_bets, match, match_key, dt_local):
    """Aggiorna l'ultima quota osservata (closing line) e il delta rispetto alla quota iniziale."""
    entry = tracked_bets.get(match_key)
    if not isinstance(entry, dict):
        return False

    target_outcome = entry.get("esito")
    preferred_book = entry.get("book")
    latest_price = find_outcome_price_across_bookmakers(
        match.get("bookmakers", []),
        target_outcome,
        preferred_book=preferred_book,
    )
    if not latest_price or latest_price <= 1.0:
        return False

    initial_quota = safe_float(entry.get("initial_quota", entry.get("quota")), 0.0)
    changed = False

    if entry.get("initial_quota") is None and initial_quota > 0:
        entry["initial_quota"] = initial_quota
        changed = True

    if entry.get("closing_quota") != latest_price:
        entry["closing_quota"] = latest_price
        entry["closing_updated_at"] = now_rome_iso()
        entry["commence_time_local"] = dt_local.isoformat(timespec="seconds")
        if initial_quota > 0:
            entry["odds_move_pct"] = round(((latest_price - initial_quota) / initial_quota) * 100.0, 2)
        changed = True

    if entry.get("status") == "active" and entry.get("result") is None:
        entry["result"] = "pending_result"
        changed = True

    return changed


def run_bot_cycle(tracked_bets):
    global gemini_calls_this_cycle, odds_quota_exhausted, _rss_cache
    gemini_calls_this_cycle = 0
    _rss_cache = {}

    print(f"🔄 Avvio ciclo 1X2 completo (Calcio, NBA, NHL | Odds API + {GEMINI_MODEL})...")
    sports_to_check = list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

    italy_tz = ZoneInfo("Europe/Rome")
    now_local = datetime.now(italy_tz)

    if now_local.hour < NIGHT_CUTOFF_HOUR:
        start_local = (now_local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    # Include partite fino alle 05:00 locali del giorno successivo per coprire NBA e NHL notturne
    end_local = start_local + timedelta(hours=24 + NIGHT_CUTOFF_HOUR)
    min_new_match_time = now_local - timedelta(minutes=5)

    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_label = f"{start_local.strftime('%Y-%m-%d %H:%M')} -> {end_local.strftime('%Y-%m-%d %H:%M')}"

    print(f"📅 Finestra locale di riferimento: {window_label} (ora attuale: {now_local.strftime('%H:%M')})")
    queued_candidates = []

    if not ODDS_API_KEY:
        print("⚠️ ODDS_API_KEY non configurato.")
        return

    if odds_quota_exhausted:
        print("🛑 Quota Odds API già esaurita o sotto soglia di sicurezza; salto le chiamate Odds API.")
        return

    for sport in sports_to_check:
        if odds_quota_exhausted:
            break

        if odds_remaining_requests is not None and odds_remaining_requests <= ODDS_API_MIN_REMAINING:
            odds_quota_exhausted = True
            print(f"🛑 Soglia Odds API ({ODDS_API_MIN_REMAINING}) raggiunta prima di {sport}.")
            break

        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        params = {
            "api_key": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "commenceTimeFrom": start_utc,
            "commenceTimeTo": end_utc,
        }
        try:
            res = requests.get(url, params=params, timeout=10)
            update_odds_quota_from_headers(res)

            if res.status_code != 200:
                print(f"⚠️ Errore Odds API per {sport}: Codice {res.status_code}")
                if res.status_code in (401, 429):
                    odds_quota_exhausted = True
                    break
                continue

            data = res.json()
            if not isinstance(data, list) or not data:
                continue

            window_matches = []
            for match in data:
                commence_time = match.get("commence_time", "")
                if not commence_time:
                    continue
                try:
                    dt_utc = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                    dt_local = dt_utc.astimezone(italy_tz)
                    if start_local <= dt_local <= end_local:
                        window_matches.append((match, dt_local))
                except Exception:
                    continue

            print(f"📌 [{sport}] Trovati {len(data)} match API, {len(window_matches)} nella finestra fino alle {NIGHT_CUTOFF_HOUR:02d}:00.")

            # 1) Aggiorna chiusura quota e controlla revoca value bet attive
            for match, dt_local in window_matches:
                home = match.get("home_team")
                away = match.get("away_team")
                if not home or not away:
                    continue

                match_key = get_existing_key(tracked_bets, home, away)
                if update_closing_line_tracking(tracked_bets, match, match_key, dt_local):
                    save_state(tracked_bets)

                tracked_entry = tracked_bets.get(match_key)
                if isinstance(tracked_entry, dict) and tracked_entry.get("status") == "active":
                    target_outcome = tracked_entry.get("esito")
                    preferred_book = tracked_entry.get("book")
                    current_price = find_outcome_price_across_bookmakers(
                        match.get("bookmakers", []),
                        target_outcome,
                        preferred_book=preferred_book,
                    )
                    if current_price and current_price > 1.0:
                        outcomes_for_book = []
                        for bm in match.get("bookmakers", []):
                            if bm.get("title") == preferred_book:
                                outcomes_for_book = get_h2h_market_outcomes(bm)
                                break
                        no_vig_map = compute_no_vig_probs_for_outcomes(outcomes_for_book)
                        current_prob = no_vig_map.get(target_outcome)
                        if current_prob is None:
                            current_prob = min(round((1.0 / current_price) * 100.0, 1), 95.0)
                        min_p = safe_float(tracked_entry.get("min_prob"), 65.0)
                        if current_prob < min_p:
                            msg = (
                                "----------------------------------------\n"
                                "🛑 [STOP SCOMMESSA / REVOCA VALUE BET 1X2] 🛑\n"
                                f"⚔️ Match: {home} vs {away}\n"
                                f"🎯 Esito Precedente (1X2): {target_outcome}\n"
                                f"💰 Nuova Quota: {current_price}\n"
                                f"📊 Nuova Probabilità: {current_prob}% (minimo richiesto: {min_p}%)\n"
                                "❌ CONSIGLIO: NON SCOMMETTERE PIÙ O CHIUDI LA POSIZIONE!"
                            )
                            if send_discord_alert(msg):
                                tracked_bets[match_key]["status"] = "revoked"
                                tracked_bets[match_key]["revoked_price"] = current_price
                                tracked_bets[match_key]["revoked_at"] = now_rome_iso()
                                save_state(tracked_bets)
                                time.sleep(1)

            # 2) Raccoglie tutte le candidature 1X2 valide della finestra non ancora iniziate
            for match, dt_local in window_matches:
                if dt_local < min_new_match_time:
                    continue

                commence_time = match.get("commence_time", "")
                home = match.get("home_team")
                away = match.get("away_team")
                if not home or not away:
                    continue

                match_key = get_existing_key(tracked_bets, home, away)
                candidate = extract_standard_candidate(match, home, away)
                if not candidate:
                    continue

                if should_skip_candidate_from_state(tracked_bets, match_key, candidate["quota"]):
                    continue

                queued_candidates.append({
                    "sport": sport,
                    "home": home,
                    "away": away,
                    "match_key": match_key,
                    "formatted_time": format_match_time(commence_time),
                    "commence_time_local": dt_local.isoformat(timespec="seconds"),
                    "candidate": candidate,
                })

        except Exception as e:
            print(f"❌ Errore generale nel controllo per {sport}: {e}")

    queued_candidates.sort(key=lambda item: item["candidate"]["base_prob"], reverse=True)
    print(f"🎯 Candidature 1X2 valide da analizzare in questo ciclo: {len(queued_candidates)}")

    for item in queued_candidates:
        sport = item["sport"]
        home = item["home"]
        away = item["away"]
        match_key = item["match_key"]
        formatted_time = item["formatted_time"]
        standard_candidate = item["candidate"]

        news_ctx = fetch_free_news_snippets(home, away, sport=sport)
        ai_result = analyze_match_1x2_with_gemini(
            sport,
            home,
            away,
            formatted_time,
            standard_candidate=standard_candidate,
            news_context=news_ctx,
        )
        if not ai_result:
            continue

        certainty_txt = str(ai_result.get("certezza_dati", "media")).strip().lower()
        stats_obj = ai_result.get("stats", {})
        assenze_txt = ai_result.get("assenze_infortuni", "Da verificare")
        std_eval = ai_result.get("standard_1x2", {})
        if not isinstance(std_eval, dict):
            std_eval = {}

        final_prob = min(round(safe_float(std_eval.get("probabilita_finale"), 0.0), 1), 95.0)
        min_req = safe_float(standard_candidate["min_prob"], 65.0)

        hard_ok, hard_reason = passes_hard_stats_filter(
            stats_obj,
            standard_candidate["is_away"],
            is_draw=standard_candidate["is_draw"],
            certainty=certainty_txt,
        )
        ai_approved = bool(std_eval.get("approvata", False)) and (final_prob >= min_req)

        if hard_ok and ai_approved:
            recommended_stake = calculate_stake(final_prob)
            classifica_forma = stats_obj.get("classifica_e_forma", "N/D") if isinstance(stats_obj, dict) else "N/D"
            h2h_txt = stats_obj.get("scontri_diretti", "N/D") if isinstance(stats_obj, dict) else "N/D"
            gol_punti_txt = stats_obj.get("gol_o_punti_fatti_subiti", "N/D") if isinstance(stats_obj, dict) else "N/D"
            campo_stanchezza_txt = stats_obj.get("fattore_campo_e_stanchezza", "N/D") if isinstance(stats_obj, dict) else "N/D"
            motivazione = std_eval.get("motivazione", "")

            if standard_candidate["is_draw"]:
                side_tag = "(Pareggio / X)"
            elif standard_candidate["is_away"]:
                side_tag = "(Trasferta)"
            else:
                side_tag = "(Casa)"

            msg = (
                "----------------------------------------\n"
                "💎 [VALUE BET 1X2 CONFERMATA] 💎\n"
                f"🏆 Sport: {sport.replace('_', ' ').upper()}\n"
                f"⚔️ Match: {home} vs {away}\n"
                f"⏰ Inizio: {formatted_time}\n"
                f"🎯 Esito (1X2): **{standard_candidate['esito']}** {side_tag}\n"
                f"💰 Quota Reale: {standard_candidate['quota']} ({standard_candidate['book']}) | Edge: +{standard_candidate.get('market_edge_pct', 0.0)}%\n"
                f"📊 Probabilità Stimata: {final_prob}% (No-vig: {standard_candidate['base_prob']}%)\n"
                f"💵 Stake: **{recommended_stake}**\n"
                f"📊 Classifica & Ultime 5: {classifica_forma}\n"
                f"🆚 Scontri Diretti (H2H): {h2h_txt}\n"
                f"⚽🏀 Gol/Punti Fatti e Subiti: {gol_punti_txt}\n"
                f"🏟️🔋 Campo & Stanchezza: {campo_stanchezza_txt}\n"
                f"🩺 Assenze & Infortuni: {assenze_txt} (Certezza: {certainty_txt})\n"
                f"📝 Perché e come abbiamo scelto: {motivazione}\n"
                "✅ Tutti i Filtri 1X2 Superati!"
            )
            if send_discord_alert(msg):
                tracked_bets[match_key] = {
                    "source": "odds_api_1x2",
                    "sport": sport,
                    "home": home,
                    "away": away,
                    "mercato": "1X2",
                    "esito": standard_candidate["esito"],
                    "book": standard_candidate["book"],
                    "quota": standard_candidate["quota"],
                    "initial_quota": standard_candidate["quota"],
                    "closing_quota": standard_candidate["quota"],
                    "market_edge_pct": standard_candidate.get("market_edge_pct", 0.0),
                    "no_vig_prob": standard_candidate["base_prob"],
                    "prob": final_prob,
                    "min_prob": min_req,
                    "certainty": certainty_txt,
                    "commence_time_local": item["commence_time_local"],
                    "created_at": now_rome_iso(),
                    "status": "active",
                    "result": "pending_result",
                }
                save_state(tracked_bets)
                time.sleep(1)
        else:
            reason = hard_reason if not hard_ok else f"Prob/approvazione non superata ({final_prob}% vs {min_req}%)"
            print(f"⏭️ [{sport}] {home} vs {away} non confermata ({reason}).")
            tracked_bets[match_key] = {
                "source": "odds_api_1x2",
                "sport": sport,
                "home": home,
                "away": away,
                "mercato": "1X2",
                "esito": standard_candidate["esito"],
                "book": standard_candidate["book"],
                "quota": standard_candidate["quota"],
                "initial_quota": standard_candidate["quota"],
                "market_edge_pct": standard_candidate.get("market_edge_pct", 0.0),
                "no_vig_prob": standard_candidate["base_prob"],
                "prob": final_prob,
                "min_prob": min_req,
                "certainty": certainty_txt,
                "commence_time_local": item["commence_time_local"],
                "created_at": now_rome_iso(),
                "status": "rejected",
                "reason": reason,
            }
            save_state(tracked_bets)

    print(
        f"📊 Riepilogo ciclo — Chiamate Gemini: {gemini_calls_this_cycle} | "
        f"Quote residue Odds API: {odds_remaining_requests if odds_remaining_requests is not None else 'N/D'}"
    )


def main():
    tracked_bets = load_state()
    while True:
        try:
            run_bot_cycle(tracked_bets)
        except Exception as e:
            print(f"❌ Errore critico durante il ciclo: {e}")

        if RUN_ONCE or odds_quota_exhausted:
            print("✅ Esecuzione completata (RUN_ONCE attivo o soglia quota Odds API raggiunta).")
            break

        print(f"⏳ In attesa di {POLL_INTERVAL_SECONDS} secondi prima del prossimo ciclo...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
