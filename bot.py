from datetime import datetime, timezone, timedelta
import json
import os
import re
import time
import requests
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types

# ================= CONFIGURAZIONE DA GITHUB SECRETS =================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
STATE_FILE = "tracked_bets.json"
# Se RUN_ONCE=true (es. su GitHub Actions cron), esegue un ciclo ed esce;
# altrimenti gira in loop continuo ogni POLL_INTERVAL_SECONDS.
RUN_ONCE = os.environ.get("RUN_ONCE", "true").lower() in ("1", "true", "yes")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "1800"))
# ====================================================================

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

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

ALWAYS_ACTIVE_SPORTS = ["basketball_nba", "icehockey_nhl"]
SEARCH_TOOLS = [{"google_search": {}}]


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

    # Limite Discord: 2000 caratteri per messaggio
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


def extract_json_list(text: str) -> list:
    if not text:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", cleaned)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []


def find_first_valid_bookmaker(bookmakers):
    if not isinstance(bookmakers, list):
        return None, []
    for bm in bookmakers:
        markets = bm.get("markets", [])
        if markets and isinstance(markets[0].get("outcomes"), list) and len(markets[0]["outcomes"]) >= 2:
            return bm.get("title", "Bookmaker"), markets[0]["outcomes"]
    return None, []


# ================= GEMINI UNIFICATO =================

def analyze_match_complete_with_gemini(sport, home, away, today_str, standard_candidate=None):
    if not client:
        print("⚠️ GEMINI_API_KEY non configurato.")
        return {}

    candidate_block = "Nessuna candidatura 1X2 standard da valutare."
    if standard_candidate:
        candidate_block = (
            f"Candidatura 1X2 standard da validare: esito='{standard_candidate['esito']}' "
            f"({'Trasferta' if standard_candidate['is_away'] else 'Casa'}), "
            f"quota={standard_candidate['quota']}, prob_base={standard_candidate['base_prob']}%, "
            f"soglia_minima={standard_candidate['min_prob']}%."
        )

    prompt = f"""
    Data odierna: {today_str}.
    Sei un analista sportivo professionista per lo sport: {sport}.
    Analizza la partita di oggi: {home} (Casa) vs {away} (Trasferta).
    {candidate_block}

    USA LA RICERCA GOOGLE PER VERIFICARE DATI REALI E AGGIORNATI:
    1. Classifica attuale (posizione di {home} e {away}).
    2. Rendimento specifico casa/trasferta nella stagione in corso:
       - Gol o punti fatti e subiti da {home} SOLO IN CASA (`gf_casa`, `gs_casa`).
       - Gol o punti fatti e subiti da {away} SOLO IN TRASFERTA (`gf_trasferta`, `gs_trasferta`).
    3. Ultime 5 partite (`sconfitte_ultime_5_casa`, `sconfitte_ultime_5_trasferta`) e carico ravvicinato di partite (includendo la partita di oggi e quelle recenti/imminenti): verifica se {home} oppure {away} ha giocato o deve giocare 3 partite in 4 giorni o 3 partite in 5 giorni (`tre_in_4_5_giorni_casa`, `tre_in_4_5_giorni_trasferta`).
    4. Infortuni, squalifiche e assenze pesanti confermate o molto probabili per oggi.

    REGOLE RIGIDE DI VALIDAZIONE:
    - Per la candidatura 1X2 standard (se presente): riduci `probabilita_finale` se ci sono infortuni/assenze importanti, forma scadente, carico ravvicinato (3 partite in 4-5 giorni) o differenza reti/punti negativa nel contesto casa/trasferta. Imposta `approvata: false` se scende sotto la soglia minima o se il contesto è sfavorevole.
    - Per i mercati avanzati / player props, includi SOLO esiti con probabilità reale >= 65.0% tra:
      - CALCIO: Over/Under gol, Goal/No Goal, Multigol casa o ospite, Giocatore chiave che segna o fa assist.
      - NHL: Over/Under gol incontro, Gol / Punti / Assist giocatore.
      - BASKETBALL_NBA: Esito finale, Punti / Assist / Rimbalzi giocatore, Tiri da 3 punti giocatore, Doppia/Tripla doppia, Punti+Assist o Assist+Rimbalzi giocatore.

    Rispondi ESCLUSIVAMENTE con un oggetto JSON valido con questa struttura esatta:
    {{
      "stats": {{
        "pos_casa": 0,
        "pos_trasferta": 0,
        "gf_casa": 0,
        "gs_casa": 0,
        "gf_trasferta": 0,
        "gs_trasferta": 0,
        "sconfitte_ultime_5_casa": 0,
        "sconfitte_ultime_5_trasferta": 0,
        "tre_in_4_5_giorni_casa": false,
        "tre_in_4_5_giorni_trasferta": false,
        "riepilogo_classifica_gol": "Es. Casa 3° (25 GF / 9 GS in casa) | Trasf 12° (11 GF / 20 GS fuori)"
      }},
      "assenze_importanti": "Elenco sintetico infortuni/squalifiche rilevanti per oggi",
      "standard_1x2": {{
        "approvata": true,
        "probabilita_finale": 68.5,
        "motivazione": "Motivazione su assenze, forma, carico gare e rendimento casa/trasferta"
      }},
      "mercati_avanzati": [
        {{
          "mercato": "Nome mercato",
          "esito": "Esito consigliato",
          "probabilita": 71.0,
          "motivazione": "Motivazione basata su gol casa/trasferta, classifica e assenze"
        }}
      ]
    }}
    """

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=SEARCH_TOOLS,
                temperature=0.1,
            ),
        )
        raw_text = response.text or ""
        print(f"🤖 [{sport}] Gemini su {home} vs {away}: {raw_text[:200].replace(chr(10), ' ')}")
        parsed = extract_json_dict(raw_text)
        if not parsed:
            print(f"⚠️ [{sport}] Impossibile leggere JSON valido da Gemini per {home} vs {away}")
        return parsed
    except Exception as e:
        print(f"⚠️ Errore analisi completa Gemini per {home} vs {away}: {e}")
        return {}


def passes_hard_stats_filter(sport, stats, is_away_team):
    if not isinstance(stats, dict) or not stats:
        return False, "Statistiche casa/trasferta non disponibili"

    try:
        if is_away_team:
            gf = float(stats.get("gf_trasferta", 0) or 0)
            gs = float(stats.get("gs_trasferta", 0) or 0)
            losses = int(stats.get("sconfitte_ultime_5_trasferta", 0) or 0)
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_trasferta", False))
            ctx = "trasferta"
        else:
            gf = float(stats.get("gf_casa", 0) or 0)
            gs = float(stats.get("gs_casa", 0) or 0)
            losses = int(stats.get("sconfitte_ultime_5_casa", 0) or 0)
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_casa", False))
            ctx = "casa"

        if heavy_schedule:
            return False, f"Carico ravvicinato in {ctx} (3 partite in 4-5 giorni)"

        if losses >= 3:
            return False, f"Forma negativa ({losses} sconfitte nelle ultime 5)"

        if gs > 0 and gf <= gs:
            return False, f"Rendimento {ctx} negativo ({gf} fatti vs {gs} subiti)"

        return True, stats.get("riepilogo_classifica_gol", "Statistiche OK")
    except Exception as e:
        return False, f"Errore verifica numerica stats: {e}"


# ================= SWISS NATIONAL LEAGUE (NLA) VIA GEMINI =================

def analyze_swiss_nla_with_gemini(today_local_str):
    if not client:
        return []

    prompt = f"""
    Data odierna locale: {today_local_str}.
    Sei un analista esperto di hockey su ghiaccio per la Swiss National League (NLA / NL).

    1. Cerca le partite ufficiali della National League svizzera in programma oggi ({today_local_str}). Se oggi non ci sono partite di NLA, restituisci [].
    2. Per ogni partita di oggi, verifica tramite ricerca Google:
       - Posizione in classifica attuale
       - Gol fatti e subiti dalla squadra di casa IN CASA (`gf_casa`, `gs_casa`) e dalla squadra ospite IN TRASFERTA (`gf_trasferta`, `gs_trasferta`)
       - Ultime 5 partite, se la squadra scelta ha 3 partite in 4 o 5 giorni (`tre_in_4_5_giorni_scelta`) e infortuni/squalifiche/assenze aggiornate
    3. Proponi SOLO esiti con probabilità reale >= 65.0% (e >= 70.0% se l'esito 1X2 è vittoria trasferta, con quota stimata > 1.29) esclusivamente su questi 3 mercati:
       - "1X2"
       - "Over/Under Gol Partita"
       - "Over/Under Gol Squadra"

    Rispondi ESCLUSIVAMENTE con una lista JSON:
    [
      {{
        "home": "Squadra Casa",
        "away": "Squadra Ospite",
        "ora_locale": "19:45",
        "mercato": "1X2",
        "esito": "Squadra Casa",
        "quota_stimata": 1.70,
        "probabilita": 68.5,
        "gf_casa": 55,
        "gs_casa": 34,
        "gf_trasferta": 38,
        "gs_trasferta": 52,
        "sconfitte_ultime_5_scelta": 1,
        "tre_in_4_5_giorni_scelta": false,
        "riepilogo_classifica_gol": "Casa 2° (55 GF / 34 GS in casa) | Ospite 11° (38 GF / 52 GS fuori)",
        "assenze_e_analisi": "Sintesi infortuni/assenze e motivazione"
      }}
    ]
    """

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=SEARCH_TOOLS,
                temperature=0.1,
            ),
        )
        raw_text = response.text or ""
        print(f"🏒 Gemini NLA: {raw_text[:200].replace(chr(10), ' ')}")
        return extract_json_list(raw_text)
    except Exception as e:
        print(f"⚠️ Errore analisi Swiss NLA con Gemini: {e}")
        return []


def monitor_ai_tracked_bets(tracked_bets, today_str):
    if not client:
        return

    ai_active = {
        k: v for k, v in tracked_bets.items()
        if v.get("status") == "active" and v.get("source") in ("gemini_advanced", "gemini_nla")
    }
    if not ai_active:
        return

    items_desc = [
        {
            "key": k,
            "sport": v.get("sport"),
            "match": f"{v.get('home')} vs {v.get('away')}",
            "mercato": v.get("mercato", ""),
            "esito": v.get("esito"),
            "prob_precedente": v.get("prob"),
            "min_prob": v.get("min_prob", 65.0),
        }
        for k, v in ai_active.items()
    ]

    prompt = f"""
    Data odierna: {today_str}.
    Verifica tramite ricerca Google se ci sono novità dell'ultimo minuto (infortuni, assenze improvvise, portieri/titolari fuori) per queste scommesse già inviate:
    {json.dumps(items_desc, ensure_ascii=False)}

    Per ciascuna chiave `key`, indica la `nuova_probabilita` aggiornata e `revoca` (true se scende sotto `min_prob`, altrimenti false).
    Rispondi ESCLUSIVAMENTE con una lista JSON:
    [
      {{
        "key": "...",
        "nuova_probabilita": 60.0,
        "revoca": true,
        "motivo": "Motivo dell'eventuale revoca"
      }}
    ]
    """

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=SEARCH_TOOLS,
                temperature=0.1,
            ),
        )
        updates = extract_json_list(response.text or "")
        for upd in updates:
            key = upd.get("key")
            if key in tracked_bets and tracked_bets[key].get("status") == "active":
                new_prob = float(upd.get("nuova_probabilita", tracked_bets[key]["prob"]) or 0)
                min_p = float(tracked_bets[key].get("min_prob", 65.0))
                if bool(upd.get("revoca", False)) or new_prob < min_p:
                    bet = tracked_bets[key]
                    msg = (
                        "----------------------------------------\n"
                        "🛑 [STOP SCOMMESSA / REVOCA AI] 🛑\n"
                        f"⚔️ Match: {bet.get('home')} vs {bet.get('away')}\n"
                        f"🎯 Mercato / Esito: {bet.get('mercato', '')} - {bet.get('esito')}\n"
                        f"📊 Nuova Probabilità: {round(new_prob, 1)}%\n"
                        f"🩺 Motivo: {upd.get('motivo', 'Cambio condizioni / assenze')}\n"
                        "❌ CONSIGLIO: NON SCOMMETTERE PIÙ O CHIUDI LA POSIZIONE!"
                    )
                    if send_discord_alert(msg):
                        tracked_bets[key]["status"] = "revoked"
                        save_state(tracked_bets)
                        time.sleep(1)
    except Exception as e:
        print(f"⚠️ Errore monitoraggio revoche AI: {e}")


# ==============================================================================

def run_bot_cycle(tracked_bets):
    print(f"🔄 Avvio ciclo completo (Odds API 1X2 + {GEMINI_MODEL} + Swiss NLA)...")
    sports_to_check = list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

    italy_tz = ZoneInfo("Europe/Rome")
    now_local = datetime.now(italy_tz)

    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(hours=31)

    start_of_day = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_of_day = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_local_str = start_local.strftime("%Y-%m-%d")

    print(f"📅 Data locale di riferimento: {today_local_str} (ora attuale: {now_local.strftime('%H:%M')})")
    monitor_ai_tracked_bets(tracked_bets, today_local_str)

    if not ODDS_API_KEY:
        print("⚠️ ODDS_API_KEY non configurato. Salto controllo Odds API.")
    else:
        for sport in sports_to_check:
            url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
            params = {
                "api_key": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "commenceTimeFrom": start_of_day,
                "commenceTimeTo": end_of_day,
            }
            try:
                res = requests.get(url, params=params, timeout=10)
                if res.status_code != 200:
                    print(f"⚠️ Errore Odds API per {sport}: Codice {res.status_code} - {res.text[:150]}")
                    continue

                data = res.json()
                if not isinstance(data, list) or not data:
                    print(f"ℹ️ [{sport}] Nessun match restituito da Odds API.")
                    continue

                today_matches = []
                for match in data:
                    commence_time = match.get("commence_time", "")
                    if not commence_time:
                        continue
                    try:
                        dt_utc = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                        dt_local = dt_utc.astimezone(italy_tz)
                        if dt_local.strftime("%Y-%m-%d") == today_local_str:
                            today_matches.append(match)
                    except Exception:
                        continue

                print(f"📌 [{sport}] Trovati {len(data)} match nella finestra, {len(today_matches)} con data locale {today_local_str}.")

                # 1. MONITORAGGIO REVOCA BET 1X2 STANDARD GIÀ SALVATE
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
                                        "🛑 [STOP SCOMMESSA / REVOCA VALUE BET] 🛑\n"
                                        f"⚔️ Match: {home} vs {away}\n"
                                        f"🎯 Esito Precedente: {tracked_bets[match_key]['esito']}\n"
                                        f"📊 Nuova Probabilità: {current_prob}%\n"
                                        "❌ CONSIGLIO: NON SCOMMETTERE PIÙ O CHIUDI LA POSIZIONE!"
                                    )
                                    if send_discord_alert(msg):
                                        tracked_bets[match_key]["status"] = "revoked"
                                        save_state(tracked_bets)
                                        time.sleep(1)

                # 2. RICERCA NUOVE VALUE BET PER OGNI PARTITA DI OGGI
                for match in today_matches:
                    commence_time = match.get("commence_time", "")
                    home = match.get("home_team")
                    away = match.get("away_team")
                    if not home or not away:
                        continue

                    match_key = get_existing_key(tracked_bets, home, away)
                    formatted_time = format_match_time(commence_time)

                    standard_candidate = None
                    bookmakers = match.get("bookmakers", [])
                    book, outcomes = find_first_valid_bookmaker(bookmakers)

                    if outcomes and match_key not in tracked_bets:
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

                    ai_result = analyze_match_complete_with_gemini(
                        sport, home, away, today_local_str, standard_candidate=standard_candidate
                    )
                    if not ai_result:
                        continue

                    stats_obj = ai_result.get("stats", {})
                    assenze_txt = ai_result.get("assenze_importanti", "Nessuna criticità segnalata")
                    riepilogo_stats = stats_obj.get("riepilogo_classifica_gol", "N/D")

                    # --- A. INVIO EVENTUALE BET 1X2 STANDARD ---
                    if standard_candidate and match_key not in tracked_bets:
                        std_eval = ai_result.get("standard_1x2", {})
                        final_prob = min(round(float(std_eval.get("probabilita_finale", 0) or 0), 1), 95.0)
                        min_req = standard_candidate["min_prob"]

                        hard_ok, hard_reason = passes_hard_stats_filter(
                            sport, stats_obj, standard_candidate["is_away"]
                        )
                        ai_approved = bool(std_eval.get("approvata", False)) and (final_prob >= min_req)

                        if hard_ok and ai_approved:
                            recommended_stake = calculate_stake(final_prob)
                            msg = (
                                "----------------------------------------\n"
                                "💎 [VALUE BET STANDARD OTTIMIZZATA] 💎\n"
                                f"🏆 Sport: {sport.replace('_', ' ').upper()}\n"
                                f"⚔️ Match: {home} vs {away}\n"
                                f"⏰ Inizio: {formatted_time}\n"
                                f"🎯 Esito: {standard_candidate['esito']} {'(Trasferta)' if standard_candidate['is_away'] else '(Casa)'}\n"
                                f"💰 Quota: {standard_candidate['quota']} ({standard_candidate['book']})\n"
                                f"📊 Probabilità: {final_prob}%\n"
                                f"💵 Stake: **{recommended_stake}**\n"
                                f"📈 Classifica & Gol Casa/Trasf: {riepilogo_stats}\n"
                                f"🩺 Assenze: {assenze_txt}\n"
                                f"📝 Analisi: {std_eval.get('motivazione', '')}\n"
                                "✅ Filtri Superati!"
                            )
                            if send_discord_alert(msg):
                                tracked_bets[match_key] = {
                                    "source": "odds_api_1x2",
                                    "sport": sport,
                                    "home": home,
                                    "away": away,
                                    "mercato": "1X2",
                                    "esito": standard_candidate["esito"],
                                    "prob": final_prob,
                                    "min_prob": min_req,
                                    "status": "active",
                                }
                                save_state(tracked_bets)
                                time.sleep(1)
                        else:
                            print(
                                f"⛔ Bloccata 1X2 {home} vs {away} ({standard_candidate['esito']}): "
                                f"hard_ok={hard_ok} ({hard_reason}), ai_approved={ai_approved}, prob={final_prob}%"
                            )

                    # --- B. INVIO MERCATI AVANZATI & PLAYER PROPS ---
                    for bet in ai_result.get("mercati_avanzati", []):
                        prob_stimata = round(float(bet.get("probabilita", 0) or 0), 1)
                        mercato_nome = str(bet.get("mercato", "")).strip()
                        esito_consigliato = str(bet.get("esito", "")).strip()
                        motivazione = bet.get("motivazione", "")

                        if prob_stimata < 65.0:
                            print(f"⛔ Scartato mercato avanzato {home} vs {away} ({mercato_nome} - {esito_consigliato}): prob {prob_stimata}% < 65%")
                            continue

                        if not mercato_nome or not esito_consigliato:
                            continue

                        recommended_stake = calculate_stake(prob_stimata)
                        safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", f"{mercato_nome}_{esito_consigliato}").strip("_")
                        adv_match_key = f"{match_key}_{safe_slug}"

                        if adv_match_key not in tracked_bets:
                            msg = (
                                "----------------------------------------\n"
                                "🔥 [VALUE BET AVANZATA / PLAYER PROPS] 🔥\n"
                                f"🏆 Sport: {sport.replace('_', ' ').upper()}\n"
                                f"⚔️ Match: {home} vs {away}\n"
                                f"⏰ Inizio: {formatted_time}\n"
                                f"🎯 Mercato: {mercato_nome}\n"
                                f"📌 Esito Consigliato: **{esito_consigliato}**\n"
                                f"📊 Probabilità Stimata: {prob_stimata}%\n"
                                f"💵 Stake Consigliato: **{recommended_stake}**\n"
                                f"📈 Classifica & Gol Casa/Trasf: {riepilogo_stats}\n"
                                f"🩺 Assenze: {assenze_txt}\n"
                                f"📝 Analisi: {motivazione}\n"
                                "✅ Analisi Qualitativa Superata!"
                            )
                            if send_discord_alert(msg):
                                tracked_bets[adv_match_key] = {
                                    "source": "gemini_advanced",
                                    "sport": sport,
                                    "home": home,
                                    "away": away,
                                    "mercato": mercato_nome,
                                    "esito": esito_consigliato,
                                    "prob": prob_stimata,
                                    "min_prob": 65.0,
                                    "status": "active",
                                }
                                save_state(tracked_bets)
                                time.sleep(1)

            except Exception as e:
                print(f"❌ Errore generale nel controllo per {sport}: {e}")

    # 3. SWISS NATIONAL LEAGUE (NLA) VIA GEMINI
    print("🏒 Controllo Swiss National League (NLA) tramite Gemini...")
    nla_bets = analyze_swiss_nla_with_gemini(today_local_str)
    print(f"🏒 Proposte NLA ricevute da Gemini: {len(nla_bets)}")
    allowed_nla_markets = {"1x2", "over/under gol partita", "over/under gol squadra"}

    for item in nla_bets:
        home = str(item.get("home", "")).strip()
        away = str(item.get("away", "")).strip()
        mercato = str(item.get("mercato", "")).strip()
        esito = str(item.get("esito", "")).strip()
        prob = min(round(float(item.get("probabilita", 0) or 0), 1), 95.0)
        quota_stimata = float(item.get("quota_stimata", 0) or 0)
        ora_locale = item.get("ora_locale", "Oggi")
        riepilogo_stats = item.get("riepilogo_classifica_gol", "N/D")
        nota = item.get("assenze_e_analisi", "")
        sconfitte_5 = int(item.get("sconfitte_ultime_5_scelta", 0) or 0)
        heavy_nla = bool(item.get("tre_in_4_5_giorni_scelta", False))

        if not home or not away or not mercato or not esito:
            continue

        mercato_norm = mercato.lower()
        if mercato_norm not in allowed_nla_markets:
            print(f"⛔ Mercato NLA non ammesso: {mercato} ({home} vs {away})")
            continue

        is_away_1x2 = (mercato_norm == "1x2") and (esito.lower() == away.lower())
        min_prob_nla = 70.0 if is_away_1x2 else 65.0

        if heavy_nla:
            print(f"⛔ Bloccata NLA {home} vs {away} ({esito}): carico ravvicinato (3 partite in 4-5 giorni)")
            continue

        if sconfitte_5 >= 3:
            print(f"⛔ Bloccata NLA {home} vs {away} ({esito}): {sconfitte_5} sconfitte nelle ultime 5")
            continue

        if quota_stimata > 0 and quota_stimata <= 1.29:
            print(f"⛔ Bloccata NLA {home} vs {away} ({esito}): quota stimata troppo bassa ({quota_stimata})")
            continue

        if prob < min_prob_nla:
            print(f"⛔ Bloccata NLA {home} vs {away} ({esito}): prob {prob}% < {min_prob_nla}%")
            continue

        base_key = get_existing_key(tracked_bets, home, away)
        safe_slug = re.sub(r"[^a-zA-Z0-9]+", "_", f"NLA_{mercato}_{esito}").strip("_")
        nla_key = f"{base_key}_{safe_slug}"

        if nla_key not in tracked_bets:
            recommended_stake = calculate_stake(prob)
            quota_str = f"{quota_stimata:.2f} (stimata)" if quota_stimata > 0 else "N/D"
            msg = (
                "----------------------------------------\n"
                "🏒 [VALUE BET SWISS NATIONAL LEAGUE] 🏒\n"
                f"🏆 Lega: SWISS NATIONAL LEAGUE (NLA)\n"
                f"⚔️ Match: {home} vs {away}\n"
                f"⏰ Ora Locale: {ora_locale}\n"
                f"🎯 Mercato: {mercato}\n"
                f"📌 Esito Consigliato: **{esito}**\n"
                f"💰 Quota: {quota_str}\n"
                f"📊 Probabilità: {prob}%\n"
                f"💵 Stake: **{recommended_stake}**\n"
                f"📈 Classifica & Gol Casa/Trasf: {riepilogo_stats}\n"
                f"🩺 Assenze & Analisi: {nota}\n"
                "✅ Filtri NLA Superati!"
            )
            if send_discord_alert(msg):
                tracked_bets[nla_key] = {
                    "source": "gemini_nla",
                    "sport": "icehockey_swiss_nla",
                    "home": home,
                    "away": away,
                    "mercato": mercato,
                    "esito": esito,
                    "prob": prob,
                    "min_prob": min_prob_nla,
                    "status": "active",
                }
                save_state(tracked_bets)
                time.sleep(1)


def main():
    tracked_bets = load_state()
    while True:
        try:
            run_bot_cycle(tracked_bets)
        except Exception as e:
            print(f"❌ Errore critico durante il ciclo: {e}")

        if RUN_ONCE:
            print("✅ RUN_ONCE attivo: esecuzione completata.")
            break

        print(f"⏳ In attesa di {POLL_INTERVAL_SECONDS} secondi prima del prossimo ciclo...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
