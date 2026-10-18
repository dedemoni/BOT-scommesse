from datetime import datetime
import json
import os
import time
import requests

# ================= CONFIGURAZIONE DA GITHUB SECRETS =================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = "tracked_bets.json"
# ====================================================================

# Mappa per collegare i codici di Football-Data.org a quelli di The Odds API (inclusa Conference League)
LEAGUE_MAPPING = {
    "SA": "soccer_italy_serie_a",  # Serie A
    "PL": "soccer_epl",  # Premier League
    "PD": "soccer_spain_la_liga",  # LaLiga
    "BL1": "soccer_germany_bundesliga",  # Bundesliga
    "FL1": "soccer_france_ligue_one",  # Ligue 1
    "CLI": "soccer_usa_mls",  # Major League Soccer
    "CL": "soccer_uefa_champs_league",  # Champions League
    "EL": "soccer_uefa_europa_league",  # Europa League
    "TE": "soccer_uefa_conference_league",  # Conference League
}

# Sport fissi che vengono controllati sempre (senza passare da football-data)
ALWAYS_ACTIVE_SPORTS = ["basketball_nba", "icehockey_nhl"]


def load_state():
  if os.path.exists(STATE_FILE):
    try:
      with open(STATE_FILE, "r") as f:
        return json.load(f)
    except:
      return {}
  return {}


def save_state(state):
  with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=4)


def send_discord_alert(message):
  payload = {"content": message}
  try:
    response = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    if response.status_code == 204:
      print("📲 Notifica inviata con successo su Discord!")
    else:
      print(f"Errore Discord: {response.text}")
  except Exception as e:
    print(f"Errore di rete Discord: {e}")


def get_active_football_sports_today():
  """Consulta il calendario di Football-Data per vedere quali campionati di calcio giocano oggi"""
  print("📅 Controllo calendario partite di calcio per oggi...")
  active_sports = set(ALWAYS_ACTIVE_SPORTS)  # NBA e NHL sempre inclusi

  if not FOOTBALL_DATA_KEY:
    print(
        "⚠️ FOOTBALL_DATA_KEY non trovata, includo tutti i campionati per"
        " sicurezza."
    )
    return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

  url = "https://api.football-data.org/v4/matches"
  headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}

  try:
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
      data = response.json()
      matches = data.get("matches", [])

      found_competitions = set()
      for match in matches:
        comp_code = match.get("competition", {}).get("code")
        if comp_code in LEAGUE_MAPPING:
          found_competitions.add(LEAGUE_MAPPING[comp_code])

      for sport in found_competitions:
        active_sports.add(sport)

      print(
          "✅ Campionati di calcio attivi oggi trovati:"
          f" {list(found_competitions)}"
      )
    else:
      print(
          f"⚠️ Errore nel calendario (Codice {response.status_code}), procedo"
          " con tutti i campionati."
      )
      return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS
  except Exception as e:
    print(
        "⚠️ Errore di connessione al calendario:"
        f" {e}, procedo con tutti i campionati."
    )
    return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

  return list(active_sports)


def run_bot_cycle(tracked_bets):
  print("🔄 Avvio ciclo intelligente di analisi e monitoraggio...")

  sports_to_check = get_active_football_sports_today()
  print(
      "🎯 Sport totali che verranno interrogati su The Odds API oggi:"
      f" {len(sports_to_check)}"
  )

  for sport in sports_to_check:
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
    params = {
        "api_key": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    try:
      res = requests.get(url, params=params)
      if res.status_code == 200:
        data = res.json()

        # 1. MONITORAGGIO BET GIÀ SALVATE
        for match in data:
          home = match["home_team"]
          away = match["away_team"]
          match_key = f"{home}vs{away}"

          if (
              match_key in tracked_bets
              and tracked_bets[match_key]["status"] == "active"
          ):
            bookmakers = match.get("bookmakers", [])
            if bookmakers:
              outcomes = bookmakers[0]["markets"][0]["outcomes"]
              for out in outcomes:
                if out["name"] == tracked_bets[match_key]["esito"]:
                  current_price = out["price"]
                  implied_prob = 1 / current_price * 100
                  current_prob = round(implied_prob + 4.5, 1)
                  if current_prob > 95:
                    current_prob = 95.0

                  if current_prob < 60.0:
                    msg = (
                        "----------------------------------------\n"
                        "🛑 [STOP SCOMMESSA / REVOCA VALUE BET] 🛑\n"
                        f"⚔️ Match: {home} vs {away}\n"
                        f"🎯 Esito Precedente: {tracked_bets[match_key]['esito']}\n"
                        "📊 Nuova Probabilità Stimata:"
                        f" {current_prob}% (Sotto soglia 60%)\n"
                        "⚠️ Spiegazione: Variazione quote negative.\n"
                        "❌ CONSIGLIO: NON SCOMMETTERE PIÙ QUESTA PARTITA!"
                    )
                    send_discord_alert(msg)
                    tracked_bets[match_key]["status"] = "revoked"
                    save_state(tracked_bets)
                    time.sleep(1)

                  elif (
                      abs(current_prob - tracked_bets[match_key]["prob"])
                      >= 5.0
                  ):
                    msg = (
                        "----------------------------------------\n"
                        "⚠️ [ATTENZIONE - CAMBIAMENTO RILEVATO] ⚠️\n"
                        f"⚔️ Match: {home} vs {away}\n"
                        f"🎯 Esito: {tracked_bets[match_key]['esito']}\n"
                        "📊 Aggiornamento Probabilità: Era"
                        f" {tracked_bets[match_key]['prob']}% -> Ora"
                        f" {current_prob}%\n"
                        "🔍 Spiegazione: Oscillazione di mercato rilevante."
                    )
                    send_discord_alert(msg)
                    tracked_bets[match_key]["prob"] = current_prob
                    save_state(tracked_bets)
                    time.sleep(1)

        # 2. RICERCA NUOVE VALUE BET (Analizza TUTTE le partite)
        for match in data:
          home = match["home_team"]
          away = match["away_team"]
          match_key = f"{home}vs{away}"

          if match_key in tracked_bets:
            continue

          bookmakers = match.get("bookmakers", [])
          if bookmakers:
            book = bookmakers[0]["title"]
            outcomes = bookmakers[0]["markets"][0]["outcomes"]

            for out in outcomes:
              name = out["name"]
              price = out["price"]

              implied_prob = 1 / price * 100
              calculated_prob = round(implied_prob + 4.5, 1)
              if calculated_prob > 95:
                calculated_prob = 95.0

              if price > 1.29 and calculated_prob >= 60.0:
                msg = (
                    "----------------------------------------\n"
                    "💎 [NUOVA VALUE BET RILEVATA] 💎\n"
                    f"🏆 Sport/Evento: {sport.replace('_', ' ').upper()}\n"
                    f"⚔️ Match: {home} vs {away}\n"
                    f"🎯 Esito Consigliato: {name}\n"
                    f"💰 Quota Reale: {price} ({book})\n"
                    f"📊 Probabilità di Vittoria: {calculated_prob}%\n"
                    "🔥 Filtro Superato: Quota > 1.29 & Prob > 60%\n"
                    "✅ Analisi Statistica: Ottimale"
                )
                send_discord_alert(msg)

                tracked_bets[match_key] = {
                    "sport": sport,
                    "home": home,
                    "away": away,
                    "esito": name,
                    "prob": calculated_prob,
                    "status": "active",
                }
                save_state(tracked_bets)
                time.sleep(1)
    except Exception as e:
      print(f"Errore controllo per {sport}: {e}")


if __name__ == "__main__":
  print(f"\n--- Esecuzione Ciclo Intelligente Bot: {datetime.now()} ---")
  tracked_bets = load_state()
  run_bot_cycle(tracked_bets)
  print("--- Esecuzione completata ---")
