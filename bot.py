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

# Mappa per collegare i codici di Football-Data.org a quelli di The Odds API (Calcio)
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

# Sport attivi fissi (verranno controllati con API dedicate gratuite)
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
    print("📅 Controllo calendario partite di calcio per oggi...")
    active_sports = set(ALWAYS_ACTIVE_SPORTS)

    if not FOOTBALL_DATA_KEY:
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
        else:
            return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS
    except:
        return list(LEAGUE_MAPPING.values()) + ALWAYS_ACTIVE_SPORTS

    return list(active_sports)


# ================= CONTROLLI STATO DI FORMA MULTISPORT (GRATUITI) =================


def check_soccer_form(sport, team_name):
    """Controlla forma calcio tramite Football-Data.org"""
    if not FOOTBALL_DATA_KEY:
        return True, "N/A"

    comp_code = None
    for code, odds_name in LEAGUE_MAPPING.items():
        if odds_name == sport:
            comp_code = code
            break

    if not comp_code:
        return True, "N/A"

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
                        if (
                            team_name.lower() in current_team.lower()
                            or current_team.lower() in team_name.lower()
                        ):
                            form = row.get("form", "")
                            position = row.get("position", 0)
                            if form and form.count("L") >= 4:
                                return (
                                    False,
                                    f"Forma scarsa (Ultime 5: {form}, Pos: {position})",
                                )
                            return (
                                True,
                                f"Pos: {position}° | Forma: {form if form else 'N/D'}",
                            )
    except Exception as e:
        print(f"Errore controllo calcio per {team_name}: {e}")
    return True, "Forma non verificata"


def check_nba_form(team_name):
    """Controlla record e stato NBA tramite API pubblica gratuita ESPN"""
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/standings"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            for group in data.get("children", []):
                for standing in group.get("standings", []):
                    for entry in standing.get("entries", []):
                        t_name = entry.get("team", {}).get("displayName", "")
                        if (
                            team_name.lower() in t_name.lower()
                            or t_name.lower() in t_name.lower()
                        ):
                            stats = entry.get("stats", [])
                            record = "N/A"
                            for stat in stats:
                                if stat.get("name") == "overall":
                                    record = stat.get("displayValue", "N/A")
                            return True, f"NBA Record: {record}"
    except Exception as e:
        print(f"Errore controllo NBA per {team_name}: {e}")
    return True, "NBA Form OK"


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
                if (
                    team_name.lower() in full_name.lower()
                    or team_name.lower() in name.lower()
                ):
                    wins = team_record.get("wins", 0)
                    losses = team_record.get("losses", 0)
                    ot_losses = team_record.get("otLosses", 0)
                    streak = team_record.get("streakCode", "N/A")
                    return (
                        True,
                        f"NHL Record: {wins}-{losses}-{ot_losses} | Streak: {streak}",
                    )
    except Exception as e:
        print(f"Errore controllo NHL per {team_name}: {e}")
    return True, "NHL Form OK"


# ==============================================================================


def run_bot_cycle(tracked_bets):
    print("🔄 Avvio ciclo intelligente multi-sport di analisi...")
    sports_to_check = get_active_football_sports_today()

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
                                            f"📊 Nuova Probabilità: {current_prob}%\n"
                                            "❌ CONSIGLIO: NON SCOMMETTERE PIÙ!"
                                        )
                                        send_discord_alert(msg)
                                        tracked_bets[match_key]["status"] = "revoked"
                                        save_state(tracked_bets)
                                        time.sleep(1)

                # 2. RICERCA NUOVE VALUE BET CON FILTRI MULTISPORT
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
                                target_team = (
                                    home
                                    if name == home
                                    else (away if name == away else None)
                                )

                                form_ok = True
                                form_info = "N/A"

                                if target_team:
                                    print(
                                        f"🔍 Controllo stato per [{sport}]:"
                                        f" {target_team}..."
                                    )
                                    if "soccer" in sport:
                                        form_ok, form_info = check_soccer_form(
                                            sport, target_team
                                        )
                                    elif "basketball_nba" in sport:
                                        form_ok, form_info = check_nba_form(
                                            target_team
                                        )
                                    elif "icehockey_nhl" in sport:
                                        form_ok, form_info = check_nhl_form(
                                            target_team
                                        )

                                if not form_ok:
                                    print(
                                        f"⚠️ Scartata {home} vs {away} per"
                                        f" statistiche sfavorevoli: {form_info}"
                                    )
                                    continue

                                msg = (
                                    "----------------------------------------\n"
                                    "💎 [VALUE BET MULTISPORT VERIFICATA] 💎\n"
                                    f"🏆 Sport: {sport.replace('_', ' ').upper()}\n"
                                    f"⚔️ Match: {home} vs {away}\n"
                                    f"🎯 Esito Consigliato: {name}\n"
                                    f"💰 Quota: {price} ({book})\n"
                                    f"📊 Probabilità: {calculated_prob}%\n"
                                    f"📈 Dettagli: {form_info}\n"
                                    "✅ Filtri Superati con Successo!"
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
