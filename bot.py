from datetime import datetime, timezone, timedelta
import json
import os
import re
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

# Limiti Gemini (al minuto / ciclo)
GEMINI_DELAY_SECONDS = float(os.environ.get("GEMINI_DELAY_SECONDS", "4.0"))
MAX_GEMINI_CALLS_PER_CYCLE = int(os.environ.get("MAX_GEMINI_CALLS_PER_CYCLE", "14"))

# Limiti The Odds API (protezione plafond mensile)
ODDS_API_MIN_REMAINING = int(os.environ.get("ODDS_API_MIN_REMAINING", "25"))

# Finestra notturna: include partite fino alle 05:00 locali del giorno successivo
NIGHT_CUTOFF_HOUR = int(os.environ.get("NIGHT_CUTOFF_HOUR", "5"))
# ====================================================================

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
gemini_calls_this_cycle = 0
odds_remaining_requests = None
odds_quota_exhausted = False

LEAGUE_MAPPING = {
    "SA": "soccer_italy_serie_a",
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "CLI": "soccer_usa_mls",
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "TE": "soccer_uefa_conference_league",
}

ALWAYS_ACTIVE_SPORTS = [
    "basketball_nba",
    "icehockey_nhl",
]

FREE_RSS_FEEDS = [
    "https://www.espn.com/espn/rss/soccer/news",
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nhl/news",
]


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


def find_first_valid_bookmaker(bookmakers):
    if not isinstance(bookmakers, list):
        return None, []
    for bm in bookmakers:
        markets = bm.get("markets", [])
        if markets and isinstance(markets[0].get("outcomes"), list) and len(markets[0]["outcomes"]) >= 2:
            return bm.get("title", "Bookmaker"), markets[0]["outcomes"]
    return None, []


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


def fetch_free_news_snippets(home: str, away: str, max_items: int = 4) -> str:
    keywords = [w.lower() for w in f"{home} {away}".split() if len(w) > 3]
    matched = []
    fallback = []

    for feed_url in FREE_RSS_FEEDS:
        try:
            resp = requests.get(feed_url, timeout=6)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:15]:
                title = (item.findtext("title") or "").strip()
                if not title:
                    continue
                fallback.append(title)
                t_low = title.lower()
                if any(k in t_low for k in keywords) or any(
                    inj in t_low for inj in ("injur", "out", "questionable", "ruled out", "suspend")
                ):
                    matched.append(title)
        except Exception:
            continue

    chosen = matched[:max_items] if matched else fallback[:2]
    return " | ".join(chosen) if chosen else "Nessuna notizia RSS trovata."


def call_gemini_with_retry(prompt: str, context_label: str) -> str:
    global gemini_calls_this_cycle
    if not client:
        return ""

    if gemini_calls_this_cycle >= MAX_GEMINI_CALLS_PER_CYCLE:
        print(
            f"🛡️ Raggiunto tetto di {MAX_GEMINI_CALLS_PER_CYCLE} chiamate Gemini per questo ciclo. "
            "Mi fermo per rispettare i limiti."
        )
        return "CYCLE_CAP_REACHED"

    for attempt in range(2):
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
            return response.text or ""
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                if attempt == 0:
                    print(f"⏳ Limite al minuto/richieste (429) su {context_label}: attendo 65s e riprovo...")
                    time.sleep(65)
                    continue
                print(f"🛑 Limite 429 persistente su {context_label} con modello {GEMINI_MODEL}.")
                return "QUOTA_EXCEEDED"
            print(f"⚠️ Errore Gemini su {context_label}: {e}")
            return ""
    return ""


def analyze_match_1x2_with_gemini(sport, home, away, match_time_local, standard_candidate, news_context=""):
    if not client:
        print("⚠️ GEMINI_API_KEY non configurato.")
        return {}

    role_label = "Trasferta" if standard_candidate["is_away"] else "Casa"

    prompt = f"""
    Sei un analista sportivo specializzato ESCLUSIVAMENTE sul mercato 1X2 (esito finale) per lo sport: {sport}.
    Data/ora locale partita: {match_time_local}.
    Partita: {home} (Casa) vs {away} (Trasferta).

    Candidatura 1X2 da valutare:
    - Esito proposto: {standard_candidate['esito']} ({role_label})
    - Quota reale: {standard_candidate['quota']} ({standard_candidate['book']})
    - Probabilità base da quota: {standard_candidate['base_prob']}%
    - Soglia minima richiesta per approvare: {standard_candidate['min_prob']}%

    Notizie recenti da feed RSS:
    {news_context}

    Analizza con rigore e prudenza (usa solo informazioni note e affidabili, senza inventare dati):
    1. Classifica attuale e ultime 5 partite di {home} e {away}.
    2. Ultimi scontri diretti (H2H) tra {home} e {away}.
    3. Assenze, squalifiche o infortuni rilevanti per {home} e {away}.
    4. Gol o punti fatti e subiti (media/totali) della squadra di casa ({home}) e della squadra ospite ({away}).
    5. Fattore campo e fattore stanchezza (trasferte lunghe, back-to-back o 3 partite in 4-5 giorni).

    Imposta `approvata: true` SOLO se `probabilita_finale` >= {standard_candidate['min_prob']}% e i fattori analizzati non sconsigliano la giocata.

    Rispondi ESCLUSIVAMENTE con un oggetto JSON valido:
    {{
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
    """

    raw_text = call_gemini_with_retry(prompt, f"{home} vs {away}")
    if raw_text in ("QUOTA_EXCEEDED", "CYCLE_CAP_REACHED"):
        return {"_stop_cycle": True}
    if not raw_text:
        return {}

    print(f"🤖 [{sport}] {GEMINI_MODEL} su {home} vs {away}: {raw_text[:160].replace(chr(10), ' ')}")
    parsed = extract_json_dict(raw_text)
    if not parsed:
        print(f"⚠️ [{sport}] JSON non valido per {home} vs {away}")
    return parsed


def passes_hard_stats_filter(stats, is_away_team):
    if not isinstance(stats, dict) or not stats:
        return True, "Filtro quote attivo"
    try:
        if is_away_team:
            losses = int(stats.get("sconfitte_ultime_5_trasferta", 0) or 0)
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_trasferta", False))
            ctx = "trasferta"
        else:
            losses = int(stats.get("sconfitte_ultime_5_casa", 0) or 0)
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_casa", False))
            ctx = "casa"

        if heavy_schedule:
            return False, f"Stanchezza / carico ravvicinato (3 partite in 4-5 giorni) in {ctx}"
        if losses >= 3:
            return False, f"Forma negativa ({losses} sconfitte nelle ultime 5)"
        return True, "OK"
    except Exception as e:
        return False, f"Errore verifica stats: {e}"


def run_bot_cycle(tracked_bets):
    global gemini_calls_this_cycle, odds_quota_exhausted
    gemini_calls_this_cycle = 0

    print(f"🔄 Avvio ciclo 1X2 (Calcio, NBA, NHL | Odds API + {GEMINI_MODEL})...")
    sports_to_check = list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

    italy_tz = ZoneInfo("Europe/Rome")
    now_local = datetime.now(italy_tz)

    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Include partite fino alle 05:00 locali del giorno successivo per coprire NBA e NHL notturne
    end_local = start_local + timedelta(hours=24 + NIGHT_CUTOFF_HOUR)

    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_label = f"{start_local.strftime('%Y-%m-%d %H:%M')} -> {end_local.strftime('%Y-%m-%d %H:%M')}"

    print(f"📅 Finestra locale di riferimento: {window_label} (ora attuale: {now_local.strftime('%H:%M')})")
    stop_cycle = False

    if not ODDS_API_KEY:
        print("⚠️ ODDS_API_KEY non configurato.")
        return

    if odds_quota_exhausted:
        print("🛑 Quota Odds API già esaurita o sotto soglia di sicurezza; salto le chiamate Odds API.")
        return

    for sport in sports_to_check:
        if stop_cycle or odds_quota_exhausted:
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
                        window_matches.append(match)
                except Exception:
                    continue

            print(f"📌 [{sport}] Trovati {len(data)} match API, {len(window_matches)} nella finestra fino alle {NIGHT_CUTOFF_HOUR:02d}:00.")

            # 1) Controlla eventuali value bet attive per revoca se la quota cala sotto soglia
            for match in data:
                home = match.get("home_team")
                away = match.get("away_team")
                if not home or not away:
                    continue

                match_key = get_existing_key(tracked_bets, home, away)
                if match_key in tracked_bets and tracked_bets[match_key].get("status") == "active":
                    _, outcomes = find_first_valid_bookmaker(match.get("bookmakers", []))
                    for out in outcomes:
                        if out.get("name") == tracked_bets[match_key]["esito"]:
                            current_price = out.get("price")
                            if not current_price:
                                continue
                            implied_prob = (1 / current_price) * 100
                            current_prob = min(round(implied_prob + 4.5, 1), 95.0)
                            min_p = float(tracked_bets[match_key].get("min_prob", 65.0))
                            if current_prob < min_p:
                                msg = (
                                    "----------------------------------------\n"
                                    "🛑 [STOP SCOMMESSA / REVOCA VALUE BET 1X2] 🛑\n"
                                    f"⚔️ Match: {home} vs {away}\n"
                                    f"🎯 Esito Precedente (1X2): {tracked_bets[match_key]['esito']}\n"
                                    f"💰 Nuova Quota: {current_price}\n"
                                    f"📊 Nuova Probabilità: {current_prob}% (minimo richiesto: {min_p}%)\n"
                                    "❌ CONSIGLIO: NON SCOMMETTERE PIÙ O CHIUDI LA POSIZIONE!"
                                )
                                if send_discord_alert(msg):
                                    tracked_bets[match_key]["status"] = "revoked"
                                    save_state(tracked_bets)
                                    time.sleep(1)

            # 2) Valuta le partite della finestra corrente solo su mercato 1X2
            for match in window_matches:
                if stop_cycle:
                    break

                commence_time = match.get("commence_time", "")
                home = match.get("home_team")
                away = match.get("away_team")
                if not home or not away:
                    continue

                match_key = get_existing_key(tracked_bets, home, away)
                if match_key in tracked_bets:
                    continue

                formatted_time = format_match_time(commence_time)
                standard_candidate = None
                bookmakers = match.get("bookmakers", [])
                book, outcomes = find_first_valid_bookmaker(bookmakers)

                if outcomes:
                    outcomes_map = {}
                    for bookmaker in bookmakers:
                        markets = bookmaker.get("markets", [])
                        if markets:
                            for out in markets[0].get("outcomes", []):
                                name = out.get("name")
                                price = out.get("price")
                                if name and price:
                                    outcomes_map.setdefault(name, []).append(price)

                    probs_dict = {}
                    for out in outcomes:
                        name = out.get("name")
                        price = out.get("price")
                        if not name or not price:
                            continue
                        if name in outcomes_map and len(outcomes_map[name]) > 1:
                            avg_price = sum(outcomes_map[name]) / len(outcomes_map[name])
                            if price > avg_price * 1.15:
                                continue
                        p = round(((1 / price) * 100) + 4.5, 1)
                        probs_dict[name] = min(p, 95.0)

                    if len(probs_dict) >= 2:
                        sorted_p = sorted(probs_dict.values(), reverse=True)
                        if (sorted_p[0] - sorted_p[1]) >= 12.0:
                            for out in outcomes:
                                name = out.get("name")
                                if name not in probs_dict:
                                    continue
                                price = out.get("price")
                                base_prob = probs_dict[name]
                                is_away_team = (name == away)
                                min_prob_required = 70.0 if is_away_team else 65.0

                                if price > 1.29 and base_prob >= min_prob_required:
                                    standard_candidate = {
                                        "esito": name,
                                        "is_away": is_away_team,
                                        "quota": price,
                                        "book": book,
                                        "base_prob": base_prob,
                                        "min_prob": min_prob_required,
                                    }
                                    break

                if not standard_candidate:
                    continue

                news_ctx = fetch_free_news_snippets(home, away)
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
                if ai_result.get("_stop_cycle"):
                    stop_cycle = True
                    break

                stats_obj = ai_result.get("stats", {})
                assenze_txt = ai_result.get("assenze_infortuni", "Da verificare")
                std_eval = ai_result.get("standard_1x2", {})
                final_prob = min(round(float(std_eval.get("probabilita_finale", 0) or 0), 1), 95.0)
                min_req = standard_candidate["min_prob"]

                hard_ok, _ = passes_hard_stats_filter(stats_obj, standard_candidate["is_away"])
                ai_approved = bool(std_eval.get("approvata", False)) and (final_prob >= min_req)

                if hard_ok and ai_approved:
                    recommended_stake = calculate_stake(final_prob)
                    classifica_forma = stats_obj.get("classifica_e_forma", "N/D")
                    h2h_txt = stats_obj.get("scontri_diretti", "N/D")
                    gol_punti_txt = stats_obj.get("gol_o_punti_fatti_subiti", "N/D")
                    campo_stanchezza_txt = stats_obj.get("fattore_campo_e_stanchezza", "N/D")
                    motivazione = std_eval.get("motivazione", "")

                    msg = (
                        "----------------------------------------\n"
                        "💎 [VALUE BET 1X2 CONFERMATA] 💎\n"
                        f"🏆 Sport: {sport.replace('_', ' ').upper()}\n"
                        f"⚔️ Match: {home} vs {away}\n"
                        f"⏰ Inizio: {formatted_time}\n"
                        f"🎯 Esito (1X2): **{standard_candidate['esito']}** {'(Trasferta)' if standard_candidate['is_away'] else '(Casa)'}\n"
                        f"💰 Quota Reale: {standard_candidate['quota']} ({standard_candidate['book']})\n"
                        f"📊 Probabilità Stimata: {final_prob}%\n"
                        f"💵 Stake: **{recommended_stake}**\n"
                        f"📊 Classifica & Ultime 5: {classifica_forma}\n"
                        f"🆚 Scontri Diretti (H2H): {h2h_txt}\n"
                        f"⚽🏀 Gol/Punti Fatti e Subiti: {gol_punti_txt}\n"
                        f"🏟️🔋 Campo & Stanchezza: {campo_stanchezza_txt}\n"
                        f"🩺 Assenze & Infortuni: {assenze_txt}\n"
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
                            "quota": standard_candidate["quota"],
                            "prob": final_prob,
                            "min_prob": min_req,
                            "status": "active",
                        }
                        save_state(tracked_bets)
                        time.sleep(1)

        except Exception as e:
            print(f"❌ Errore generale nel controllo per {sport}: {e}")

    print(
        f"📊 Riepilogo ciclo — Chiamate Gemini: {gemini_calls_this_cycle}/{MAX_GEMINI_CALLS_PER_CYCLE} | "
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
