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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemma-4-26b-it")
STATE_FILE = "tracked_bets.json"
RUN_ONCE = os.environ.get("RUN_ONCE", "true").lower() in ("1", "true", "yes")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "1800"))
GEMINI_DELAY_SECONDS = float(os.environ.get("GEMINI_DELAY_SECONDS", "3.5"))
MAX_GEMINI_CALLS_PER_CYCLE = int(os.environ.get("MAX_GEMINI_CALLS_PER_CYCLE", "15"))
# ====================================================================

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
gemini_calls_this_cycle = 0

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
    "icehockey_swiss_nla",
]

FREE_RSS_FEEDS = [
    "https://www.espn.com/espn/rss/soccer/news",
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nhl/news",
]


def list_available_models():
    if not client:
        print("⚠️ GEMINI_API_KEY non configurato: impossibile elencare i modelli.")
        return
    try:
        print("📋 Elenco modelli disponibili sulla chiave:")
        for m in client.models.list():
            name = getattr(m, "name", "")
            print(f"  - {name}")
    except Exception as e:
        print(f"⚠️ Errore nel recupero lista modelli: {e}")


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
        print(f"🛡️ Raggiunto tetto di {MAX_GEMINI_CALLS_PER_CYCLE} chiamate Gemini per questo ciclo. Mi fermo per proteggere i limiti.")
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
                    print(f"⏳ Limite richieste (429) su {context_label}: attendo 25s e riprovo...")
                    time.sleep(25)
                    continue
                print(f"🛑 Limite 429 raggiunto su {context_label} con modello {GEMINI_MODEL}.")
                return "QUOTA_EXCEEDED"
            print(f"⚠️ Errore Gemini su {context_label}: {e}")
            return ""
    return ""


def analyze_match_complete_with_gemini(sport, home, away, today_str, standard_candidate=None, news_context=""):
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
    Sei un analista sportivo per lo sport: {sport}.
    Analizza la partita: {home} (Casa) vs {away} (Trasferta).
    {candidate_block}

    Notizie recenti raccolte da feed gratuiti (verifica sempre eventuali assenze/infortuni prima di giocare):
    {news_context}

    REGOLE:
    - Valuta in modo prudente contesto casa/trasferta, forma e possibili infortuni/assenze.
    - Se c'è una candidatura 1X2 standard, metti `approvata: true` solo se `probabilita_finale` >= soglia_minima e il contesto non è sfavorevole.
    - In `mercati_avanzati` puoi proporre esiti (inclusi mercati avanzati e player props) SOLO se hanno probabilità stimata >= 65.0:
      - CALCIO: Over/Under gol, Goal/No Goal, Multigol casa/ospite, marcatore/assist probabile.
      - NHL / SWISS NLA: Over/Under gol incontro, Over/Under gol squadra, punti/gol giocatore.
      - BASKETBALL_NBA: esito/margine, punti/assist/rimbalzi/triple o combo giocatore.

    Rispondi ESCLUSIVAMENTE con un oggetto JSON valido:
    {{
      "stats": {{
        "gf_casa": 1,
        "gs_casa": 0,
        "gf_trasferta": 1,
        "gs_trasferta": 0,
        "sconfitte_ultime_5_casa": 0,
        "sconfitte_ultime_5_trasferta": 0,
        "tre_in_4_5_giorni_casa": false,
        "tre_in_4_5_giorni_trasferta": false,
        "riepilogo_classifica_gol": "Sintesi contesto e rendimento"
      }},
      "assenze_importanti": "Eventuali assenze/infortuni noti o da verificare prima del match",
      "standard_1x2": {{
        "approvata": true,
        "probabilita_finale": 68.0,
        "motivazione": "Motivazione sintetica"
      }},
      "mercati_avanzati": [
        {{
          "mercato": "Nome mercato o Player Prop",
          "esito": "Esito consigliato",
          "probabilita": 67.0,
          "motivazione": "Motivazione sintetica"
        }}
      ]
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


def passes_hard_stats_filter(sport, stats, is_away_team):
    if not isinstance(stats, dict) or not stats:
        return True, "Filtro quote attivo"
    try:
        if is_away_team:
            losses = int(stats.get("sconfitte_ultime_5_trasferta", 0) or 0)
            heavy_schedule = bool(stats.get("tre_in_4_5_giorni_trasferta", False
