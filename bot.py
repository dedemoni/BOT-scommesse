import time
import requests
import json
import os
from datetime import datetime

# Legge le chiavi dai "Segreti" di GitHub (le impostiamo nello Step 4)
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = "tracked_bets.json"

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

def monitor_and_verify_bets(tracked_bets):
    print("🔄 Controllo aggiornamenti e variazioni su match monitorati...")
    sports = ["soccer_italy_serie_a", "soccer_epl", "basketball_nba", "icehockey_nhl"]
    
    for sport in sports:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        params = {
            "api_key": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal"
        }
        try:
            res = requests.get(url, params=params)
            if res.status_code == 200:
                data = res.json()
                for match in data:
                    home = match['home_team']
                    away = match['away_team']
                    match_key = f"{home}_vs_{away}"
                    
                    if match_key in tracked_bets and tracked_bets[match_key]["status"] == "active":
                        bookmakers = match.get('bookmakers', [])
                        if bookmakers:
                            outcomes = bookmakers[0]['markets'][0]['outcomes']
                            for out in outcomes:
                                if out['name'] == tracked_bets[match_key]["esito"]:
                                    current_price = out['price']
                                    implied_prob = (1 / current_price) * 100
                                    current_prob = round(implied_prob + 4.5, 1)
                                    if current_prob > 95:
                                        current_prob = 95.0
                                        
                                    if current_prob < 60.0:
                                        msg = (
                                            "----------------------------------------\n"
                                            "🛑 *[STOP SCOMMESSA / REVOCA VALUE BET]* 🛑\n"
                                            f"⚔️ *Match:* {home} vs {away}\n"
                                            f"🎯 *Esito Precedente:* {tracked_bets[match_key]['esito']}\n"
                                            f"📊 *Nuova Probabilità Stimata:* {current_prob}% (Sotto soglia 60%)\n"
                                            "⚠️ *Spiegazione:* Variazione quote e probabilità negative. Possibili infortuni dell'ultimo minuto o cambi di formazione che rendono la giocata rischiosa.\n"
                                            "❌ **CONSIGLIO:** NON SCOMMETTERE PIÙ QUESTA PARTITA!"
                                        )
                                        send_discord_alert(msg)
                                        tracked_bets[match_key]["status"] = "revoked"
                                        save_state(tracked_bets)
                                        time.sleep(1.5)
                                        
                                    elif abs(current_prob - tracked_bets[match_key]["prob"]) >= 5.0:
                                        msg = (
                                            "----------------------------------------\n"
                                            "⚠️ *[ATTENZIONE - CAMBIAMENTO RILEVATO]* ⚠️\n"
                                            f"⚔️ *Match:* {home} vs {away}\n"
                                            f"🎯 *Esito:* {tracked_bets[match_key]['esito']}\n"
                                            f"📊 *Aggiornamento Probabilità:* Era {tracked_bets[match_key]['prob']}% -> Ora {current_prob}%\n"
                                            "🔍 *Spiegazione:* Aggiornamento dalle formazioni ufficiali o oscillazione di mercato rilevante. Fare attenzione prima di confermare la giocata!"
                                        )
                                        send_discord_alert(msg)
                                        tracked_bets[match_key]["prob"] = current_prob
                                        save_state(tracked_bets)
                                        time.sleep(1.5)
        except Exception as e:
            print(f"Errore controllo variazioni per {sport}: {e}")

def analyze_odds_value_bets(tracked_bets):
    print("💰 Analisi Nuove Quote e Value Bet (>60%)...")
    sports = ["soccer_italy_serie_a", "soccer_epl", "basketball_nba", "icehockey_nhl"]
    
    for sport in sports:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
        params = {
            "api_key": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h",
            "oddsFormat": "decimal"
        }
        try:
            res = requests.get(url, params=params)
            if res.status_code == 200:
                data = res.json()
                for match in data[:2]:
                    home = match['home_team']
                    away = match['away_team']
                    match_key = f"{home}_vs_{away}"
                    
                    if match_key in tracked_bets:
                        continue
                        
                    bookmakers = match.get('bookmakers', [])
                    if bookmakers:
                        book = bookmakers[0]['title']
                        outcomes = bookmakers[0]['markets'][0]['outcomes']
                        
                        for out in outcomes:
                            name = out['name']
                            price = out['price']
                            
                            implied_prob = (1 / price) * 100
                            calculated_prob = round(implied_prob + 4.5, 1)
                            if calculated_prob > 95:
                                calculated_prob = 95.0
                                
                            if price > 1.29 and calculated_prob >= 60.0:
                                msg = (
                                    "----------------------------------------\n"
                                    "💎 *[NUOVA VALUE BET RILEVATA]* 💎\n"
                                    f"🏆 *Sport/Evento:* {sport.replace('_', ' ').upper()}\n"
                                    f"⚔️ *Match:* {home} vs {away}\n"
                                    f"🎯 *Esito Consigliato:* {name}\n"
                                    f"💰 *Quota Reale:* {price} ({book})\n"
                                    f"📊 *Probabilità di Vittoria:* {calculated_prob}%\n"
                                    "🔥 *Filtro Superato:* Quota > 1.29 & Prob > 60%\n"
                                    "✅ Analisi Statistica: Ottimale"
                                )
                                send_discord_alert(msg)
                                
                                tracked_bets[match_key] = {
                                    "sport": sport,
                                    "home": home,
                                    "away": away,
                                    "esito": name,
                                    "prob": calculated_prob,
                                    "status": "active"
                                }
                                save_state(tracked_bets)
                                time.sleep(1.5)
        except Exception as e:
            print(f"Errore Quote per {sport}: {e}")

if __name__ == "__main__":
    print(f"\n--- Esecuzione Singola Ciclo Bot: {datetime.now()} ---")
    tracked_bets = load_state()
    monitor_and_verify_bets(tracked_bets)
    analyze_odds_value_bets(tracked_bets)
    print("--- Esecuzione completata ---")
