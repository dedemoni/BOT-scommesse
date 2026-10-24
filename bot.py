from datetime import datetime, timezone, timedelta
import json
import os
import time
import requests
from zoneinfo import ZoneInfo

# ================= CONFIGURAZIONE DA GITHUB SECRETS =================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = "tracked_bets.json"
# ====================================================================

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


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
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


def calculate_stake(prob):
    """Calcola la puntata in frazioni di unità (max 1) basata sulla probabilità"""
    if prob >= 82.0:
        return "1/2 unità (0.50)"
    elif prob >= 76.0:
        return "1/3 unità (0.33)"
    elif prob >= 70.0:
        return "1/4 unità (0.25)"
    else:
        return "1/5 unità (0.20)"


def format_match_time(iso_time_str):
    """Converte l'ora ISO UTC in orario locale italiano (Europe/Rome)"""
    try:
        dt_utc = datetime.fromisoformat(iso_time_str.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo("Europe/Rome"))
        return dt_local.strftime("%d/%m/%Y alle %H:%M")
    except Exception:
        return iso_time_str


def get_active_football_sports_today():
    print("📅 Controllo calendario partite di calcio per oggi...")
    active_sports = set(ALWAYS_ACTIVE_SPORTS)

    if not FOOTBALL_DATA_KEY:
        print("⚠️ FOOTBALL_DATA_KEY non trovata, procedo con tutti i campionati.")
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
            print(f"✅ Campionati di calcio attivi oggi trovati: {list(found_competitions)}")
        else:
            print(f"⚠️ Errore calendario (Codice {response.status_code}), procedo con tutti.")
            return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS
    except Exception as e:
        print(f"⚠️ Errore di connessione al calendario: {e}, procedo con tutti.")
        return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

    return list(active_sports)


# ================= CONTROLLI STATISTICI AVANZATI E GRATUITI =================

def check_soccer_form(sport, team_name):
    """Controlla forma e Differenza Reti tramite Football-Data.org"""
    if not FOOTBALL_DATA_KEY:
        return True, "Forma N/A (Key mancante)"

    comp_code = None
    for code, odds_name in LEAGUE_MAPPING.items():
        if odds_name == sport:
            comp_code = code
            break

    if not comp_code:
        return True, "Forma N/A"

    url = f"https://api.football-data.org/v4/competitions/{comp_code}/standings"
    headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}

    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            for table_group in data.get("standings", []):
                if table_group.get("type") == "TOTAL":
                    for row in table_group.get("table", []):
                        current_team = row.get("team", {}).get("name", "")
                        if team_name.lower() in current_team.lower() or current_team.lower() in team_name.lower():
                            form = row.get("form", "")
                            position = row.get("position", 0)
                            goal_diff = row.get("goalDifference", 0)

                            # 1. Scarta se troppe sconfitte recenti (3 o più)
                            if form and form.count("L") >= 3:
                                return False, f"Forma rischiosa (Ultime 5: {form}, Pos: {position}°)"

                            # 2. Scarta se la differenza reti in classifica è negativa o pari a 0
                            if goal_diff <= 0:
                                return False, f"Diff. Reti insufficiente ({goal_diff} gol, Pos: {position}°)"

                            return True, f"Pos: {position}° | Diff.Reti: {goal_diff:+d} | Forma: {form if form else 'N/D'}"
    except Exception as e:
        print(f"Errore controllo calcio per {team_name}: {e}")
    return True, "Forma non verificata"


def check_nba_form(team_name):
    """Controlla record e il filtro Back-to-Back (partite consecutive) via ESPN API"""
    url_standings = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/standings"
    form_info = "NBA Form OK"
    try:
        # Controllo Record
        res = requests.get(url_standings, timeout=5)
        if res.status_code == 200:
            data = res.json()
            for group in data.get("children", []):
                for standing in group.get("standings", []):
                    for entry in standing.get("entries", []):
                        t_name = entry.get("team", {}).get("displayName", "")
                        if team_name.lower() in t_name.lower() or t_name.lower() in team_name.lower():
                            stats = entry.get("stats", [])
                            for stat in stats:
                                if stat.get("name") == "overall":
                                    record = stat.get("displayValue", "N/A")
                                    form_info = f"NBA Record: {record}"

        # Controllo Back-to-Back (Partita giocata ieri)
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        url_scoreboard = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={yesterday_str}"
        res_sb = requests.get(url_scoreboard, timeout=5)
        if res_sb.status_code == 200:
            sb_data = res_sb.json()
            for event in sb_data.get("events", []):
                for comp in event.get("competitions", []):
                    for competitor in comp.get("competitors", []):
                        played_team = competitor.get("team", {}).get("displayName", "")
                        if team_name.lower() in played_team.lower() or played_team.lower() in team_name.lower():
                            return False, f"B2B Rischio Stanchezza (Ha giocato anche ieri!) | {form_info}"

        return True, form_info
    except Exception as e:
        print(f"Errore controllo NBA per {team_name}: {e}")
    return True, form_info


def check_nhl_form(team_name):
    """Controlla record e statistiche NHL tramite API ufficiale gratuita"""
    url = "https://api-web.nhle.com/v1/standings/now"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            for team_record in data.get("standings", []):
                place = team_record.get("placeName", {}).get("default", "")
                name = team_record.get("teamName", {}).get("default", "")
                full_name = f"{place} {name}"
                if team_name.lower() in full_name.lower() or team_name.lower() in name.lower():
                    wins = team_record.get("wins", 0)
                    losses = team_record.get("losses", 0)
                    ot_losses = team_record.get("otLosses", 0)
                    streak = team_record.get("streakCode", "N/A")
                    return True, f"NHL Record: {wins}-{losses}-{ot_losses} | Streak: {streak}"
    except Exception as e:
        print(f"Errore controllo NHL per {team_name}: {e}")
    return True, "NHL Form OK"


# ==============================================================================

def run_bot_cycle(tracked_bets):
    print("🔄 Avvio ciclo intelligente multi-sport con controlli statistici avanzati...")
    sports_to_check = get_active_football_sports_today()

    now_utc = datetime.now(timezone.utc)
    start_of_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_of_day = now_utc.replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_str = now_utc.strftime("%Y-%m-%d")

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
            res = requests.get(url, params=params)
            if res.status_code == 200:
                data = res.json()

                # 1. MONITORAGGIO BET GIÀ SALVATE (REVOCA SE SCENDE SOTTO IL 65%)
                for match in data:
                    home = match["home_team"]
                    away = match["away_team"]
                    match_key = f"{home}vs{away}"

                    if match_key in tracked_bets and tracked_bets[match_key]["status"] == "active":
                        bookmakers = match.get("bookmakers", [])
                        if bookmakers:
                            outcomes = bookmakers[0]["markets"][0]["outcomes"]
                            for out in outcomes:
                                if out["name"] == tracked_bets[match_key]["esito"]:
                                    current_price = out["price"]
                                    implied_prob = (1 / current_price) * 100
                                    current_prob = round(implied_prob + 4.5, 1)
                                    if current_prob > 95:
                                        current_prob = 95.0

                                    if current_prob < 65.0:
                                        msg = (
                                            "----------------------------------------\n"
                                            "🛑 [STOP SCOMMESSA / REVOCA VALUE BET] 🛑\n"
                                            f"⚔️ Match: {home} vs {away}\n"
                                            f"🎯 Esito Precedente: {tracked_bets[match_key]['esito']}\n"
                                            f"📊 Nuova Probabilità: {current_prob}%\n"
                                            "❌ CONSIGLIO: NON SCOMMETTERE PIÙ O CHIUDI LA POSIZIONE!"
                                        )
                                        send_discord_alert(msg)
                                        tracked_bets[match_key]["status"] = "revoked"
                                        save_state(tracked_bets)
                                        time.sleep(1)

                # 2. RICERCA NUOVE VALUE BET
                for match in data:
                    commence_time = match.get("commence_time", "")
                    if commence_time and not commence_time.startswith(today_str):
                        continue

                    home = match["home_team"]
                    away = match["away_team"]
                    match_key = f"{home}vs{away}"

                    if match_key in tracked_bets:
                        continue

                    bookmakers = match.get("bookmakers", [])
                    if bookmakers:
                        book = bookmakers[0]["title"]
                        outcomes = bookmakers[0]["markets"][0]["outcomes"]

                        # Calcolo probabilità di tutte le opzioni per filtro equilibrio/derby
                        probs_dict = {}
                        for out in outcomes:
                            p = round(((1 / out["price"]) * 100) + 4.5, 1)
                            probs_dict[out["name"]] = min(p, 95.0)

                        # Filtro Anti-Derby / Equilibrato: se la differenza tra le due opzioni principali è < 15%
                        if len(probs_dict) >= 2:
                            sorted_p = sorted(probs_dict.values(), reverse=True)
                            if (sorted_p[0] - sorted_p[1]) < 15.0:
                                print(f"⚠️ Scartata {home} vs {away}: Scontro troppo equilibrato/Derby.")
                                continue

                        for out in outcomes:
                            name = out["name"]
                            price = out["price"]
                            calculated_prob = probs_dict[name]

                            # Soglia base del 65%, oppure 70% se gioca in Trasferta (Fattore Campo)
                            is_away_team = (name == away)
                            min_prob_required = 70.0 if is_away_team else 65.0

                            if price > 1.29 and calculated_prob >= min_prob_required:
                                target_team = home if name == home else (away if name == away else None)

                                form_ok = True
                                form_info = "N/A"

                                if target_team:
                                    print(f"🔍 Controllo avanzato per [{sport}]: {target_team}...")
                                    if "soccer" in sport:
                                        form_ok, form_info = check_soccer_form(sport, target_team)
                                    elif "basketball_nba" in sport:
                                        form_ok, form_info = check_nba_form(target_team)
                                    elif "icehockey_nhl" in sport:
                                        form_ok, form_info = check_nhl_form(target_team)

                                if not form_ok:
                                    print(f"⚠️ Scartata {home} vs {away} per filtro statistico: {form_info}")
                                    continue

                                formatted_time = format_match_time(commence_time)
                                recommended_stake = calculate_stake(calculated_prob)

                                msg = (
                                    "----------------------------------------\n"
                                    "💎 [VALUE BET MULTISPORT VERIFICATA] 💎\n"
                                    f"🏆 Sport: {sport.replace('_', ' ').upper()}\n"
                                    f"⚔️ Match: {home} vs {away}\n"
                                    f"⏰ Inizio: {formatted_time}\n"
                                    f"🎯 Esito Consigliato: {name} {'(Trasferta)' if is_away_team else '(Casa)'}\n"
                                    f"💰 Quota: {price} ({book})\n"
                                    f"📊 Probabilità Stimata: {calculated_prob}%\n"
                                    f"💵 Stake Consigliato: **{recommended_stake}**\n"
                                    f"📈 Dettagli: {form_info}\n"
                                    "✅ Tutti i Filtri Superati con Successo!"
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
    print(f"\n--- Esecuzione Ciclo Multi-Sport Bot: {datetime.now()} ---")
    tracked_bets = load_state()
    run_bot_cycle(tracked_bets)
    print("--- Esecuzione completata ---")
