import json
import os
import re
import base64
from datetime import datetime

import requests
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html, no_update, ctx

load_dotenv()
if not os.getenv("REDIRECT_URI", "").startswith("https"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8050/oauth2callback")

_oauth_flows = {}

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
NEWS_API_KEY    = os.getenv("NEWS_API_KEY", "")

scheduler = BackgroundScheduler(timezone="Europe/Madrid")
scheduler.start()
_scheduled_job = {"job": None}

_APP_DIR            = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE          = os.path.join(_APP_DIR, "google_token.json")
CLIENT_SECRETS_FILE = os.path.join(_APP_DIR, "credentials.json")
FEEDBACK_FILE       = os.path.join(_APP_DIR, "feedback.json")
TOOL_USAGE_FILE     = os.path.join(_APP_DIR, "tool_usage.json")

# Write credentials.json from env var if not exists
_creds_env = os.getenv("GOOGLE_CREDENTIALS", "")
if _creds_env and not os.path.exists(CLIENT_SECRETS_FILE):
    with open(CLIENT_SECRETS_FILE, "w") as f:
        f.write(_creds_env)

# Updated scopes — users must re-authenticate (delete google_token.json)
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

BREVO_API_KEY      = os.getenv("BREVO_API_KEY", "")

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build as gapi_build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

TYPE_COLORS = {
    "Meeting":  "#60a5fa",
    "Class":    "#34d399",
    "Personal": "#f472b6",
    "Deadline": "#f87171",
}


# ─────────────────────────────────────────────────────────────────────────────
# FEEDBACK HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_feedback() -> dict:
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"thumbs_up": 0, "thumbs_down": 0, "log": []}


def save_feedback(data: dict):
    try:
        with open(FEEDBACK_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# TOOL USAGE TRACKING
# ─────────────────────────────────────────────────────────────────────────────

def load_tool_usage() -> dict:
    if os.path.exists(TOOL_USAGE_FILE):
        try:
            with open(TOOL_USAGE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"get_weather": 0, "get_news": 0, "get_calendar_events": 0,
            "create_calendar_event": 0, "add_task": 0, "delete_calendar_event": 0}


def track_tool_call(tool_name: str):
    usage = load_tool_usage()
    usage[tool_name] = usage.get(tool_name, 0) + 1
    try:
        with open(TOOL_USAGE_FILE, "w") as f:
            json.dump(usage, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_weather(city: str) -> dict:
    try:
        if WEATHER_API_KEY:
            url  = (f"https://api.openweathermap.org/data/2.5/weather"
                    f"?q={city}&appid={WEATHER_API_KEY}&units=metric")
            data = requests.get(url, timeout=5).json()
            if data.get("cod") != 200:
                return {"error": True, "city": city, "message": data.get("message","API error")}
            icons = {"Clear":"☀️","Clouds":"☁️","Rain":"🌧️","Drizzle":"🌦️",
                     "Thunderstorm":"⛈️","Snow":"❄️","Mist":"🌫️"}
            cond = data["weather"][0]["main"]
            return {
                "city": city, "temp": round(data["main"]["temp"]),
                "feels_like": round(data["main"]["feels_like"]),
                "humidity": data["main"]["humidity"],
                "condition": cond, "icon": icons.get(cond, "🌤️"),
            }
    except Exception:
        pass
    return {"error": True, "city": city, "message": "Add WEATHER_API_KEY to your .env file to see live weather."}


def get_news(topics: list, n: int = 8) -> list:
    try:
        if NEWS_API_KEY and topics:
            query = " OR ".join(topics)
            for url in [
                (f"https://newsapi.org/v2/everything?q={query}"
                 f"&language=en&sortBy=publishedAt&pageSize={n}&apiKey={NEWS_API_KEY}"),
                (f"https://newsapi.org/v2/top-headlines?q={query}"
                 f"&language=en&pageSize={n}&apiKey={NEWS_API_KEY}"),
                (f"https://newsapi.org/v2/top-headlines?category=technology"
                 f"&language=en&pageSize={n}&apiKey={NEWS_API_KEY}"),
            ]:
                try:
                    data = requests.get(url, timeout=8).json()
                    if data.get("status") == "ok" and data.get("articles"):
                        out = []
                        for a in data["articles"][:n]:
                            if not a.get("title") or a["title"] == "[Removed]":
                                continue
                            try:
                                pub  = datetime.strptime(a["publishedAt"][:19],"%Y-%m-%dT%H:%M:%S")
                                diff = datetime.now() - pub
                                h    = int(diff.total_seconds()/3600)
                                ago  = (f"{int(diff.total_seconds()/60)}m ago" if h < 1
                                        else f"{h}h ago" if h < 24 else f"{h//24}d ago")
                            except Exception:
                                ago = ""
                            # Tag article with matching topic
                            title_lower = a["title"].lower()
                            matched_topic = next(
                                (t for t in topics if t.lower() in title_lower), topics[0]
                            )
                            out.append({
                                "title":       a["title"],
                                "description": a.get("description") or "",
                                "source":      a["source"]["name"],
                                "time":        ago,
                                "url":         a.get("url",""),
                                "topic":       matched_topic,
                            })
                        if out:
                            return out
                except Exception:
                    continue
    except Exception:
        pass
    return []


def get_calendar_events(date_str: str) -> list:
    if GOOGLE_AVAILABLE and os.path.exists(TOKEN_FILE):
        try:
            import pytz
            creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
            service = gapi_build("calendar","v3",credentials=creds)

            local_tz = pytz.timezone("Europe/Madrid")
            day   = datetime.fromisoformat(date_str)
            tmin  = local_tz.localize(day.replace(hour=0,  minute=0,  second=0)).isoformat()
            tmax  = local_tz.localize(day.replace(hour=23, minute=59, second=59)).isoformat()

            cal_list = service.calendarList().list().execute()
            all_events = []
            seen_ids = set()

            GCAL_COLORS = {
                "1":  "#7986cb", "2":  "#33b679", "3":  "#8e24aa",
                "4":  "#e67c73", "5":  "#f6c026", "6":  "#f5511d",
                "7":  "#039be5", "8":  "#616161", "9":  "#3f51b5",
                "10": "#0b8043", "11": "#d60000",
            }

            for cal in cal_list.get("items", []):
                try:
                    cal_color_id = cal.get("colorId", "")
                    cal_bg_color = cal.get("backgroundColor", "")
                    cal_color = cal_bg_color or GCAL_COLORS.get(cal_color_id, "#60a5fa")

                    res = service.events().list(
                        calendarId=cal["id"],
                        timeMin=tmin, timeMax=tmax,
                        singleEvents=True, orderBy="startTime",
                        maxResults=50,
                    ).execute()
                    for item in res.get("items", []):
                        if item["id"] in seen_ids:
                            continue
                        seen_ids.add(item["id"])
                        start = item["start"].get("dateTime", item["start"].get("date", ""))
                        end   = item["end"].get("dateTime",   item["end"].get("date", ""))
                        def _hhmm(s):
                            if not s or "T" not in s:
                                return None
                            return s[11:16]
                        t_start = _hhmm(start) or "00:00"
                        t_end   = _hhmm(end)   or "23:59"

                        ev_color_id = item.get("colorId", "")
                        ev_color = GCAL_COLORS.get(ev_color_id, cal_color)

                        # Extract meeting join link (Google Meet, Zoom, Teams)
                        hang_link   = item.get("hangoutLink", "")
                        desc_text   = item.get("description", "") or ""
                        zoom_match  = re.search(
                            r'https://[^\s<>"]*zoom\.us/j/[^\s<>"&]*', desc_text)
                        zoom_link   = zoom_match.group(0).rstrip(".,;") if zoom_match else ""
                        teams_match = re.search(
                            r'https://teams\.microsoft\.com/l/meetup-join/[^\s<>"&]*', desc_text)
                        teams_link  = teams_match.group(0).rstrip(".,;") if teams_match else ""
                        # Also extract meet.google.com links from description
                        gmeet_match = re.search(
                            r'https://meet\.google\.com/[^\s<>"&]+', desc_text)
                        gmeet_desc  = gmeet_match.group(0).rstrip(".,;") if gmeet_match else ""
                        meet_link   = hang_link or gmeet_desc or zoom_link or teams_link

                        all_events.append({
                            "time":      t_start,
                            "end":       t_end,
                            "title":     item.get("summary", "Untitled"),
                            "type":      "meeting",
                            "color":     ev_color,
                            "calendar":  cal.get("summary", ""),
                            "location":  item.get("location", ""),
                            "notes":     desc_text,
                            "meet_link": meet_link,
                        })
                except Exception:
                    continue

            all_events.sort(key=lambda e: e["time"])
            return all_events
        except ImportError:
            try:
                creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
                service = gapi_build("calendar","v3",credentials=creds)
                day  = datetime.fromisoformat(date_str)
                tmin = day.replace(hour=0, minute=0, second=0).isoformat() + "+01:00"
                tmax = day.replace(hour=23,minute=59,second=59).isoformat() + "+01:00"
                res  = service.events().list(
                    calendarId="primary", timeMin=tmin, timeMax=tmax,
                    singleEvents=True, orderBy="startTime",
                ).execute()
                events = []
                for item in res.get("items", []):
                    start = item["start"].get("dateTime", item["start"].get("date", ""))
                    end   = item["end"].get("dateTime",   item["end"].get("date", ""))
                    events.append({
                        "time":      start[11:16] if "T" in start else "00:00",
                        "end":       end[11:16]   if "T" in end   else "23:59",
                        "title":     item.get("summary", "Untitled"),
                        "type":      "meeting",
                        "location":  item.get("location", ""),
                        "notes":     item.get("description", ""),
                        "meet_link": item.get("hangoutLink", ""),
                    })
                return events
            except Exception:
                pass
        except Exception:
            pass
    return []


def geocode_location(place: str, city_hint: str = "") -> dict | None:
    import time
    parts = [p.strip() for p in place.split(",")]
    candidates = [place]
    if len(parts) >= 3:
        candidates.append(", ".join(parts[1:]))
    if len(parts) >= 4:
        candidates.append(", ".join(parts[1:4]))
    short = parts[0]
    if city_hint:
        candidates.append(f"{short}, {city_hint}")
    candidates.append(short)
    postcode = next((p.strip() for p in parts if re.match(r"\d{5}", p.strip())), None)
    if postcode:
        for p in parts:
            if re.match(r"(Carrer|Avinguda|Calle|Passeig|Rambla|Plaza|Plaça|Av\.|C\.)", p.strip(), re.I):
                candidates.append(f"{p.strip()}, {postcode}")
                break
    seen = set()
    for query in candidates:
        q = query.strip()
        if not q or q in seen:
            continue
        seen.add(q)
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": "WakeFlowApp/2.0"},
                timeout=5,
            )
            data = resp.json()
            if data:
                return {"lat": float(data[0]["lat"]), "lon": float(data[0]["lon"]),
                        "display_name": data[0]["display_name"]}
            time.sleep(0.25)
        except Exception:
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# EVENT IMPORT — AI EXTRACTION + CALENDAR CREATION
# ─────────────────────────────────────────────────────────────────────────────

def extract_events_from_text(text: str) -> list:
    """Use GPT-4o-mini to extract structured calendar events from plain text."""
    if not OPENAI_API_KEY:
        return []
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        today  = datetime.now().strftime("%Y-%m-%d")
        resp   = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=1500,
            messages=[
                {"role": "system", "content": (
                    f"Today is {today}. Extract ALL calendar events from the text. "
                    "Return ONLY a valid JSON array (no markdown, no explanation). "
                    "Each object must have: title (string), date (YYYY-MM-DD), "
                    "start_time (HH:MM or null), end_time (HH:MM or null), "
                    "location (string or null), description (string or null). "
                    "If a date is relative (e.g. next Monday), calculate from today. "
                    "Return [] if no events found."
                )},
                {"role": "user", "content": text[:8000]},
            ],
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[WakeFlow] Event text extraction error: {e}")
        return []


def extract_events_from_image(b64_data: str, mime_type: str) -> list:
    """Use GPT-4o-mini vision to extract structured calendar events from an image."""
    if not OPENAI_API_KEY:
        return []
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        today  = datetime.now().strftime("%Y-%m-%d")
        resp   = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=1500,
            messages=[
                {"role": "system", "content": (
                    f"Today is {today}. Extract ALL calendar events from this image "
                    "(schedule, timetable, meeting invitation, etc.). "
                    "Return ONLY a valid JSON array (no markdown, no explanation). "
                    "Each object: title (string), date (YYYY-MM-DD), "
                    "start_time (HH:MM or null), end_time (HH:MM or null), "
                    "location (string or null), description (string or null). "
                    "Calculate relative dates from today. Return [] if none found."
                )},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}},
                ]},
            ],
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[WakeFlow] Image event extraction error: {e}")
        return []


def create_calendar_event(event_data: dict, calendar_id: str = "primary") -> tuple[bool, str]:
    """Create a single event in the specified Google Calendar (default: primary)."""
    if not GOOGLE_AVAILABLE or not os.path.exists(TOKEN_FILE):
        return False, "Google Calendar not connected."
    try:
        from datetime import timedelta
        creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service = gapi_build("calendar", "v3", credentials=creds)

        date       = (event_data.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
        start_time = (event_data.get("start_time") or "").strip() or None
        end_time   = (event_data.get("end_time")   or "").strip() or None

        # Normalize time format (handle 16.35, 1635, 16:35)
        def _norm(t):
            if not t: return None
            t = t.replace(".", ":").replace(",", ":")
            if len(t) == 4 and t.isdigit():
                t = t[:2] + ":" + t[2:]
            parts = t.split(":")
            if len(parts) == 2:
                try:
                    h, m = int(parts[0]), int(parts[1])
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        return f"{h:02d}:{m:02d}"
                except ValueError:
                    pass
            return None

        start_time = _norm(start_time)
        end_time   = _norm(end_time)

        if start_time:
            start = {"dateTime": f"{date}T{start_time}:00", "timeZone": "Europe/Madrid"}
            if end_time:
                end = {"dateTime": f"{date}T{end_time}:00", "timeZone": "Europe/Madrid"}
            else:
                st  = datetime.strptime(f"{date}T{start_time}", "%Y-%m-%dT%H:%M")
                et  = st + timedelta(hours=1)
                end = {"dateTime": et.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": "Europe/Madrid"}
        else:
            # All-day: end must be day AFTER start (Google Calendar API requirement)
            next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            start = {"date": date}
            end   = {"date": next_day}

        body = {"summary": event_data.get("title", "Imported Event"), "start": start, "end": end}
        if event_data.get("location"):
            body["location"] = event_data["location"]
        if event_data.get("description"):
            body["description"] = event_data["description"]
        if event_data.get("colorId"):
            body["colorId"] = str(event_data["colorId"])

        cal_id = calendar_id if calendar_id else "primary"
        result = service.events().insert(calendarId=cal_id, body=body).execute()
        return True, result.get("htmlLink", "")
    except Exception as e:
        err = str(e)
        if "insufficientPermissions" in err:
            return False, "Need write permission — reconnect Google Calendar."
        return False, err


def move_event_to_calendar(title: str, date: str, target_calendar_name: str) -> tuple[bool, str]:
    """Move an event from its current calendar to a different calendar by name."""
    if not GOOGLE_AVAILABLE or not os.path.exists(TOKEN_FILE):
        return False, "Google Calendar not connected."
    try:
        import pytz
        creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service = gapi_build("calendar", "v3", credentials=creds)

        # Find destination calendar ID
        dest_cal_id = find_calendar_id_by_name(target_calendar_name)
        if dest_cal_id == "primary" and target_calendar_name.lower() not in ("primary", ""):
            return False, f"Calendar '{target_calendar_name}' not found."

        local_tz = pytz.timezone("Europe/Madrid")
        day  = datetime.fromisoformat(date)
        tmin = local_tz.localize(day.replace(hour=0,  minute=0,  second=0)).isoformat()
        tmax = local_tz.localize(day.replace(hour=23, minute=59, second=59)).isoformat()

        query_tokens = set(title.lower().split()) - {"the","a","an","with","for","at","on","in","of"}
        cal_list = service.calendarList().list().execute()
        moved = []

        for cal in cal_list.get("items", []):
            src_cal_id = cal["id"]
            if src_cal_id == dest_cal_id:
                continue
            events = service.events().list(
                calendarId=src_cal_id, timeMin=tmin, timeMax=tmax,
                singleEvents=True, orderBy="startTime"
            ).execute()
            for ev in events.get("items", []):
                ev_tokens = set(ev.get("summary","").lower().split()) - {"the","a","an","with","for","at","on","in","of"}
                if query_tokens & ev_tokens:
                    service.events().move(
                        calendarId=src_cal_id,
                        eventId=ev["id"],
                        destination=dest_cal_id
                    ).execute()
                    moved.append(ev.get("summary", title))

        if moved:
            return True, f"Moved '{', '.join(moved)}' to '{target_calendar_name}' calendar."
        return False, f"No event matching '{title}' found on {date}."
    except Exception as e:
        return False, str(e)


def update_calendar_event_by_title(title: str, date: str, meeting_link: str = "",
                                    location: str = "", description: str = "",
                                    new_start_time: str = "", new_end_time: str = "",
                                    new_date: str = "") -> tuple[bool, str]:
    """Find an event and update its time, meeting link, location, or description."""
    if not GOOGLE_AVAILABLE or not os.path.exists(TOKEN_FILE):
        return False, "Google Calendar not connected."
    try:
        from datetime import timedelta
        import pytz
        creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service = gapi_build("calendar", "v3", credentials=creds)

        local_tz = pytz.timezone("Europe/Madrid")
        day  = datetime.fromisoformat(date)
        tmin = local_tz.localize(day.replace(hour=0,  minute=0,  second=0)).isoformat()
        tmax = local_tz.localize(day.replace(hour=23, minute=59, second=59)).isoformat()

        query_tokens = set(title.lower().split()) - {"the","a","an","with","for","at","on","in","of"}
        cal_list = service.calendarList().list().execute()
        updated  = []

        for cal in cal_list.get("items", []):
            cal_id = cal["id"]
            events = service.events().list(
                calendarId=cal_id, timeMin=tmin, timeMax=tmax,
                singleEvents=True, orderBy="startTime"
            ).execute()
            for ev in events.get("items", []):
                ev_tokens = set(ev.get("summary","").lower().split()) - {"the","a","an","with","for","at","on","in","of"}
                if query_tokens & ev_tokens:
                    patch = {}
                    # Reschedule time
                    target_date = (new_date or date).strip()
                    if new_start_time:
                        start_str = new_start_time.strip().replace(".", ":").replace(",", ":")
                        patch["start"] = {"dateTime": f"{target_date}T{start_str}:00", "timeZone": "Europe/Madrid"}
                        if new_end_time:
                            end_str = new_end_time.strip().replace(".", ":").replace(",", ":")
                            patch["end"] = {"dateTime": f"{target_date}T{end_str}:00", "timeZone": "Europe/Madrid"}
                        else:
                            # Default 1 hour later
                            st = datetime.strptime(f"{target_date}T{start_str}", "%Y-%m-%dT%H:%M")
                            et = st + timedelta(hours=1)
                            patch["end"] = {"dateTime": et.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": "Europe/Madrid"}
                    elif new_date:
                        next_day = (datetime.strptime(new_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                        patch["start"] = {"date": new_date}
                        patch["end"]   = {"date": next_day}
                    if location:
                        patch["location"] = location
                    if description or meeting_link:
                        existing_desc = ev.get("description", "") or ""
                        new_desc = description or ""
                        if meeting_link:
                            new_desc = (new_desc + "\n\n" if new_desc else "") + f"🔗 Join Meeting: {meeting_link}"
                        if existing_desc and new_desc not in existing_desc:
                            new_desc = existing_desc + "\n\n" + new_desc
                        patch["description"] = new_desc.strip()
                    if patch:
                        service.events().patch(calendarId=cal_id, eventId=ev["id"], body=patch).execute()
                        updated.append(ev.get("summary", title))

        if updated:
            return True, f"Updated: {', '.join(updated)}"
        return False, f"No event matching '{title}' found on {date}."
    except Exception as e:
        return False, str(e)


def find_calendar_id_by_name(name: str) -> str:
    """Look up a calendar's ID by partial name match. Returns 'primary' if not found."""
    if not name or not GOOGLE_AVAILABLE or not os.path.exists(TOKEN_FILE):
        return "primary"
    try:
        creds    = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service  = gapi_build("calendar", "v3", credentials=creds)
        cal_list = service.calendarList().list().execute()
        name_lower = name.lower()
        for c in cal_list.get("items", []):
            if name_lower in c.get("summary", "").lower():
                return c["id"]
    except Exception:
        pass
    return "primary"


def delete_calendar_event_by_title(title: str, date: str) -> tuple[bool, str]:
    """Find and delete a calendar event by fuzzy title match and date."""
    if not GOOGLE_AVAILABLE or not os.path.exists(TOKEN_FILE):
        return False, "Google Calendar not connected."
    try:
        import pytz
        creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service = gapi_build("calendar", "v3", credentials=creds)

        local_tz = pytz.timezone("Europe/Madrid")
        day  = datetime.fromisoformat(date)
        tmin = local_tz.localize(day.replace(hour=0,  minute=0,  second=0)).isoformat()
        tmax = local_tz.localize(day.replace(hour=23, minute=59, second=59)).isoformat()

        # Build keyword tokens from user title for fuzzy matching
        # e.g. "Meeting with deltalad" → ["meeting", "deltalad"]
        query_tokens = set(title.lower().split())
        # Remove common stop words
        stop_words = {"the","a","an","with","for","at","on","in","of","and","to","my","this"}
        query_tokens -= stop_words

        cal_list = service.calendarList().list().execute()
        deleted  = []

        for cal in cal_list.get("items", []):
            cal_id = cal["id"]
            events = service.events().list(
                calendarId=cal_id, timeMin=tmin, timeMax=tmax,
                singleEvents=True, orderBy="startTime"
            ).execute()
            for ev in events.get("items", []):
                ev_title_lower = ev.get("summary", "").lower()
                ev_tokens = set(ev_title_lower.split()) - stop_words
                # Match if any meaningful keyword overlaps
                if query_tokens & ev_tokens:
                    service.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
                    deleted.append(ev.get("summary", title))

        if deleted:
            return True, f"Deleted: {', '.join(deleted)}"
        return False, (
            f"No event matching '{title}' found on {date}. "
            "Try using the exact event title from the calendar."
        )
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# LLM — SYSTEM PROMPT + TOOL CALLING
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are WakeFlow, a smart and friendly AI morning assistant.
You speak concisely, use emojis naturally, and give actionable advice.

Your job is to help the user plan their day by answering questions about their
schedule, weather, and news. Always call the relevant tools before answering.

Rules:
- Call get_calendar_events before answering any schedule question
- Call get_weather before giving outfit or travel advice
- Call get_news before summarising headlines
- Keep replies under 120 words unless the user asks for more
- Use bullet points for 3+ items
- BEFORE any event operation (delete, update, move, reschedule, change time): ALWAYS call get_calendar_events FIRST for the relevant date to get the EXACT event title as it appears in the calendar. Then use that exact title. NEVER claim an event doesn't exist without calling get_calendar_events first.
- When the user mentions an event by a rough name (e.g. "Louvre Museum", "meeting", "picnic"), call get_calendar_events for that date and find the closest matching event — do NOT say "event not found" without fetching first.
- If user asks to reschedule or change time of an event: call get_calendar_events to confirm the event exists, then use update_calendar_event to patch it, or delete_calendar_event + create_calendar_event if needed.

IMPORTANT — DATE CALCULATION:
Each message starts with [TODAY: WEEKDAY YYYY-MM-DD]. Use this as your anchor.
When user says "this Sunday", "next Monday", "Saturday" etc., calculate the
correct YYYY-MM-DD from TODAY and pass it to get_calendar_events.
Example: if TODAY is Friday 2026-03-13 and user asks "this Sunday",
call get_calendar_events with date="2026-03-15".
NEVER guess the date — always derive it from the [TODAY: ...] tag.

Context tags in each message:
[TODAY: ...]  → today weekday + date — anchor for all date calculations
[CITY: ...]   → default city for get_weather
[TOPICS: ...] → default topics for get_news

Always end with one tip that connects the user's calendar + weather or news.\
"""

LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type":"string"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get the latest news headlines for given topics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topics": {"type":"array","items":{"type":"string"}},
                    "n":      {"type":"integer"},
                },
                "required": ["topics"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": "Get the user's schedule for a specific date.",
            "parameters": {
                "type": "object",
                "properties": {"date": {"type":"string"}},
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": (
                "Create a new event in the user's Google Calendar. "
                "Use this when the user asks to add, schedule, or create a meeting/event/appointment. "
                "Resolve relative dates like 'tomorrow', 'next Monday', 'this Friday' to YYYY-MM-DD based on today's date. "
                "If the user specifies a calendar name (e.g. 'Deltalab calendar', 'N'eng life'), pass it as calendar_name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":         {"type":"string", "description":"Event title"},
                    "date":          {"type":"string", "description":"Date in YYYY-MM-DD format"},
                    "start_time":    {"type":"string", "description":"Start time HH:MM (24h), omit for all-day"},
                    "end_time":      {"type":"string", "description":"End time HH:MM (24h), omit for all-day"},
                    "description":   {"type":"string", "description":"Optional description or meeting link"},
                    "location":      {"type":"string", "description":"Optional location"},
                    "calendar_name": {"type":"string", "description":"Name of the calendar to add to (e.g. 'Deltalab'). Leave empty for primary."},
                },
                "required": ["title", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": (
                "Delete a calendar event by its title and date. "
                "Use this when the user asks to remove, cancel, or delete a meeting/event. "
                "Always call get_calendar_events first to confirm the event exists before deleting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type":"string", "description":"Title or partial title of the event to delete"},
                    "date":  {"type":"string", "description":"Date of the event in YYYY-MM-DD format"},
                },
                "required": ["title", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_calendar_event",
            "description": (
                "Update an existing calendar event — reschedule time, add a meeting link, change location, or update description. "
                "Use this when the user wants to CHANGE the time of an event, ADD a Zoom/Meet/Teams link, "
                "or update the location/description of an existing event. "
                "Always call get_calendar_events first to get the exact event title."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":          {"type":"string", "description":"Exact event title to find"},
                    "date":           {"type":"string", "description":"Current date of the event YYYY-MM-DD"},
                    "new_start_time": {"type":"string", "description":"New start time HH:MM to reschedule to"},
                    "new_end_time":   {"type":"string", "description":"New end time HH:MM"},
                    "new_date":       {"type":"string", "description":"New date YYYY-MM-DD if moving to different day"},
                    "meeting_link":   {"type":"string", "description":"Meeting URL to add (Zoom/Meet/Teams)"},
                    "location":       {"type":"string", "description":"New location to set"},
                    "description":    {"type":"string", "description":"New or additional description text"},
                },
                "required": ["title", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_calendar_event",
            "description": (
                "Move an existing event to a different calendar. "
                "Use this when the user says 'move this event to X calendar', "
                "'fix this to be in X calendar', or 'put this in X calendar'. "
                "Always call get_calendar_events first to get the exact event title."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":               {"type":"string", "description":"Exact or partial event title"},
                    "date":                {"type":"string", "description":"Date YYYY-MM-DD"},
                    "target_calendar_name":{"type":"string", "description":"Name of the destination calendar (e.g. Deltalab, N'eng life)"},
                },
                "required": ["title", "date", "target_calendar_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Add a task or to-do item to the user's My Tasks list. "
                "Use this when the user says 'remind me', 'add a task', 'I need to do', or similar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text":     {"type":"string", "description":"Task description"},
                    "due_date": {"type":"string", "description":"Due date YYYY-MM-DD (optional)"},
                    "category": {
                        "type":"string",
                        "enum":["todo","assignment","work","personal"],
                        "description":"Task category"
                    },
                },
                "required": ["text"],
            },
        },
    },
]


def _run_tool(name: str, args: dict, city: str, topics: list) -> tuple[str, dict | None]:
    """Returns (result_json, pending_task_or_event) where pending is used for UI side-effects."""
    track_tool_call(name)

    if name == "get_weather":
        return json.dumps(get_weather(args.get("city", city))), None

    if name == "get_news":
        arts = get_news(args.get("topics", topics), args.get("n", 5))
        return json.dumps({"articles": [{"title": a["title"], "source": a["source"]} for a in arts]}), None

    if name == "get_calendar_events":
        d = args.get("date", datetime.now().strftime("%Y-%m-%d"))
        return json.dumps({"date": d, "events": get_calendar_events(d)}), None

    if name == "create_calendar_event":
        # Don't execute yet — return pending event for UI calendar picker
        event_data = {
            "title":        args.get("title", "New Event"),
            "date":         args.get("date",  datetime.now().strftime("%Y-%m-%d")),
            "start_time":   args.get("start_time") or None,
            "end_time":     args.get("end_time")   or None,
            "description":  args.get("description") or None,
            "location":     args.get("location")    or None,
            "calendar_name":args.get("calendar_name", ""),
        }
        pending = {"type": "event", "data": event_data}
        return json.dumps({"success": True, "message": f"Event '{event_data['title']}' is ready to be added. I'll ask the user to choose the target calendar."}), pending

    if name == "delete_calendar_event":
        ok, result = delete_calendar_event_by_title(
            args.get("title", ""), args.get("date", datetime.now().strftime("%Y-%m-%d"))
        )
        return json.dumps({"success": ok, "message": result}), None

    if name == "update_calendar_event":
        ok, result = update_calendar_event_by_title(
            title=args.get("title", ""),
            date=args.get("date", datetime.now().strftime("%Y-%m-%d")),
            meeting_link=args.get("meeting_link", ""),
            location=args.get("location", ""),
            description=args.get("description", ""),
            new_start_time=args.get("new_start_time", ""),
            new_end_time=args.get("new_end_time", ""),
            new_date=args.get("new_date", ""),
        )
        return json.dumps({"success": ok, "message": result}), None

    if name == "move_calendar_event":
        ok, result = move_event_to_calendar(
            title=args.get("title", ""),
            date=args.get("date", datetime.now().strftime("%Y-%m-%d")),
            target_calendar_name=args.get("target_calendar_name", "primary"),
        )
        return json.dumps({"success": ok, "message": result}), None

    if name == "add_task":
        task = {
            "text":     args.get("text", "New Task"),
            "due_date": args.get("due_date") or None,
            "category": args.get("category", "todo"),
        }
        return json.dumps({"success": True, "message": f"Task '{task['text']}' is ready. I'll ask the user to confirm."}), {"type": "task", "data": task}

    return json.dumps({"error": f"Unknown tool: {name}"}), None


def chat_with_tools(history: list, city: str, topics: list) -> tuple[str, list]:
    """Returns (ai_reply, new_tasks_to_add)."""
    if not OPENAI_API_KEY:
        return "No OpenAI API key configured. Add OPENAI_API_KEY to your .env file.", []

    from openai import OpenAI
    client   = OpenAI(api_key=OPENAI_API_KEY)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    pending_event = None
    pending_task  = None

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages,
            tools=LLM_TOOLS, tool_choice="auto", max_tokens=500,
        )
        msg = resp.choices[0].message

        rounds = 0
        while msg.tool_calls and rounds < 4:
            rounds += 1
            messages.append(msg)
            for tc in msg.tool_calls:
                args          = json.loads(tc.function.arguments)
                result, extra = _run_tool(tc.function.name, args, city, topics)
                if extra and isinstance(extra, dict):
                    if extra.get("type") == "task":
                        pending_task = extra["data"]
                    elif extra.get("type") == "event":
                        pending_event = extra["data"]
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            resp = client.chat.completions.create(
                model="gpt-4o-mini", messages=messages,
                tools=LLM_TOOLS, tool_choice="auto", max_tokens=500,
            )
            msg = resp.choices[0].message

        return msg.content or "Sorry, no response.", pending_event, pending_task
    except Exception as e:
        return f"AI error: {e}", None, None


# ─────────────────────────────────────────────────────────────────────────────
# GANTT CHART
# ─────────────────────────────────────────────────────────────────────────────

def build_gantt(events: list, date_str: str) -> go.Figure:
    if not events:
        fig = go.Figure()
        fig.update_layout(
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            font_color="#475569",
            xaxis=dict(visible=False), yaxis=dict(visible=False),
            margin=dict(l=8,r=8,t=8,b=8), height=330,
        )
        google_connected = os.path.exists(TOKEN_FILE)
        msg = ("No events found for this day." if google_connected
               else "Connect Google Calendar (top right) to see your events here.")
        fig.add_annotation(text=msg, x=0.5, y=0.5, showarrow=False,
                           font=dict(color="#475569", size=14),
                           xref="paper", yref="paper")
        return fig

    def t2h(t):
        """Convert HH:MM to float hours."""
        try:
            h, m = map(int, t.split(":"))
            return h + m / 60
        except Exception:
            return 0.0

    fig = go.Figure()
    seen_legends = set()
    for e in events:
        color    = e.get("color") or TYPE_COLORS.get(e["type"].capitalize(), "#60a5fa")
        calendar = e.get("calendar", e["type"].capitalize())
        s        = t2h(e["time"])
        f        = t2h(e["end"])
        if f <= s:
            f = s + 0.5   # fallback: 30-min block
        show_legend = calendar not in seen_legends
        seen_legends.add(calendar)
        fig.add_trace(go.Bar(
            x=[f - s],
            y=[e["title"]],
            base=[s],
            orientation="h",
            marker_color=color,
            marker_line_width=0,
            opacity=0.88,
            name=calendar,
            showlegend=show_legend,
            customdata=[[e["title"], f"{e['time']} – {e['end']}", calendar, e.get("location","—")]],
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "%{customdata[1]}<br>"
                "📅 %{customdata[2]}<br>"
                "📍 %{customdata[3]}<extra></extra>"
            ),
        ))

    # Ticks every 2 hours
    tick_vals  = list(range(0, 25, 2))
    tick_texts = [f"{h:02d}:00" for h in tick_vals]

    fig.update_layout(
        plot_bgcolor="white", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#1e293b", size=12, family="DM Sans, sans-serif"),
        legend=dict(orientation="h", y=1.1, bgcolor="rgba(255,255,255,0.0)",
                    font=dict(color="#1e293b")),
        margin=dict(l=8,r=8,t=32,b=8),
        barmode="overlay",
        xaxis=dict(
            range=[0, 12],
            tickvals=tick_vals,
            ticktext=tick_texts,
            showgrid=True, gridcolor="rgba(0,0,0,0.06)",
            title="", color="#4b5563",
            fixedrange=False,
            minallowed=0,
            maxallowed=24,
        ),
        yaxis=dict(
            showgrid=False, title="", autorange="reversed",
            tickfont=dict(color="#1e293b", size=12),
            fixedrange=True,
            automargin=True,
        ),
        height=330, clickmode="event+select", dragmode="pan",
    )
    return fig


def get_calendar_events_range(start_date_str: str, end_date_str: str) -> dict:
    """Fetch events for a date range in ONE API call. Returns {date_str: [events]}"""
    from datetime import timedelta
    start = datetime.fromisoformat(start_date_str)
    end   = datetime.fromisoformat(end_date_str)
    result = {}
    cur = start
    while cur <= end:
        result[cur.strftime("%Y-%m-%d")] = []
        cur += timedelta(days=1)

    if not (GOOGLE_AVAILABLE and os.path.exists(TOKEN_FILE)):
        return result
    try:
        import pytz
        local_tz = pytz.timezone("Europe/Madrid")
        tmin = local_tz.localize(start.replace(hour=0,  minute=0,  second=0)).isoformat()
        tmax = local_tz.localize(end.replace(  hour=23, minute=59, second=59)).isoformat()
        creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service = gapi_build("calendar","v3",credentials=creds)
        cal_list = service.calendarList().list().execute()
        seen_ids = set()
        GCAL_COLORS = {
            "1":"#7986cb","2":"#33b679","3":"#8e24aa","4":"#e67c73",
            "5":"#f6c026","6":"#f5511d","7":"#039be5","8":"#616161",
            "9":"#3f51b5","10":"#0b8043","11":"#d60000",
        }
        for cal in cal_list.get("items", []):
            try:
                cal_color = cal.get("backgroundColor","") or GCAL_COLORS.get(cal.get("colorId",""),"#60a5fa")
                res = service.events().list(
                    calendarId=cal["id"], timeMin=tmin, timeMax=tmax,
                    singleEvents=True, orderBy="startTime", maxResults=200,
                ).execute()
                for item in res.get("items", []):
                    if item["id"] in seen_ids: continue
                    seen_ids.add(item["id"])
                    s     = item["start"].get("dateTime", item["start"].get("date",""))
                    e_end = item["end"].get("dateTime",   item["end"].get("date",""))
                    if "T" in s:
                        event_date = s[:10]; t_start = s[11:16]
                        t_end = e_end[11:16] if "T" in e_end else "23:59"
                    else:
                        event_date = s; t_start = "00:00"; t_end = "23:59"
                    if event_date not in result: continue
                    ev_color  = GCAL_COLORS.get(item.get("colorId",""), cal_color)
                    desc_text = item.get("description","") or ""
                    hang_link = item.get("hangoutLink","")
                    zoom_m    = re.search(r'https://[^\s<>"]*zoom\.us/j/[^\s<>"&]*', desc_text)
                    meet_link = hang_link or (zoom_m.group(0).rstrip(".,;") if zoom_m else "")
                    result[event_date].append({
                        "time":t_start,"end":t_end,
                        "title":item.get("summary","Untitled"),
                        "type":"meeting","color":ev_color,
                        "calendar":cal.get("summary",""),
                        "location":item.get("location",""),
                        "notes":desc_text,"meet_link":meet_link,
                    })
            except Exception: continue
        for k in result: result[k].sort(key=lambda e: e["time"])
    except Exception: pass
    return result


DAY_COLORS = {
    0: {"bg":"#163980","light":"#e8edf8","text":"white"},
    1: {"bg":"#2a96c4","light":"#e0f4fb","text":"white"},
    2: {"bg":"#866088","light":"#f0e8f0","text":"white"},
    3: {"bg":"#E47F93","light":"#fdeef1","text":"white"},
    4: {"bg":"#F25D54","light":"#feeeed","text":"white"},
    5: {"bg":"#F78C63","light":"#fef3ee","text":"white"},
    6: {"bg":"#ebbcc2","light":"#fdf4f5","text":"#5a3a3a"},
}

def build_week_html(events_by_date: dict, start_date_str: str):
    from datetime import timedelta
    import math
    monday = datetime.fromisoformat(start_date_str)
    days   = [monday + timedelta(days=i) for i in range(7)]
    today  = datetime.now().date()

    def parse_h(t):
        try:
            h, m = map(int, t.split(":"))
            return h, h + m/60
        except Exception:
            return 0, 0.0

    day_events = {}
    for i, d in enumerate(days):
        evs = events_by_date.get(d.strftime("%Y-%m-%d"), [])
        processed = []
        for ev in evs:
            sh, sf = parse_h(ev["time"])
            eh, ef = parse_h(ev.get("end",""))
            if ef <= sf: ef = sf + 0.5
            processed.append({**ev, "sh": sh, "sf": sf, "ef": ef,
                               "span": max(1, math.ceil(ef - sf))})
        day_events[i] = sorted(processed, key=lambda e: e["sf"])

    occupied = set()
    starts   = {}
    for i, evs in day_events.items():
        for ev in evs:
            key = (i, ev["sh"])
            if key not in starts:
                starts[key] = ev
                for h in range(ev["sh"]+1, ev["sh"]+ev["span"]):
                    occupied.add((i, h))

    day_labels = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    header_cells = [html.Th("", style={
        "width":"55px","background":"#f8fafc",
        "border":"1px solid #e2e8f0","padding":"8px 4px",
    })]
    for i, d in enumerate(days):
        c        = DAY_COLORS[i]
        is_today = d.date() == today
        header_cells.append(html.Th(
            html.Div([
                html.Div(day_labels[i], style={"fontWeight":"700","fontSize":"12px"}),
                html.Div(d.strftime("%-d %b"), style={"fontWeight":"400","fontSize":"11px","opacity":"0.85"}),
            ]),
            style={
                "background": "#f97316" if is_today else c["bg"],
                "color":"white","textAlign":"center",
                "padding":"10px 4px","border":"1px solid #e2e8f0",
                "minWidth":"110px",
            }
        ))

    rows = [html.Tr(header_cells)]
    for hour in range(24):
        cells = [html.Td(f"{hour:02d}:00", style={
            "fontSize":"10px","color":"#94a3b8","textAlign":"right",
            "padding":"2px 6px","verticalAlign":"top","whiteSpace":"nowrap",
            "border":"1px solid #f1f5f9","background":"#f8fafc",
            "fontWeight":"500","width":"55px",
        })]

        for i in range(7):
            if (i, hour) in occupied:
                continue

            c  = DAY_COLORS[i]
            ev = starts.get((i, hour))

            if ev:
                span  = ev["span"]
                t_lbl = f"{ev['time']}–{ev.get('end','')}"
                title = ev["title"]
                pill_h = span * 28 - 4

                cell_content = html.Div([
                    html.Div(t_lbl, style={"fontSize":"9px","opacity":"0.9","lineHeight":"1"}),
                    html.Div(
                        (title[:22]+"…") if len(title)>22 else title,
                        style={"fontSize":"11px","fontWeight":"600",
                               "lineHeight":"1.3","marginTop":"2px"},
                    ),
                ], style={
                    "background": ev.get("color") or c["bg"],
                    "color":"white","borderRadius":"4px",
                    "padding":"4px 6px",
                    "minHeight":f"{pill_h}px",
                    "boxSizing":"border-box",
                })

                cells.append(html.Td(
                    cell_content,
                    rowSpan=span,
                    style={
                        "verticalAlign":"top","padding":"2px",
                        "border":"1px solid #e2e8f0",
                        "background": c["light"],
                        "minWidth":"110px",
                    }
                ))
            else:
                cells.append(html.Td("", style={
                    "verticalAlign":"top","padding":"2px",
                    "border":"1px solid #f1f5f9",
                    "background":"white","minWidth":"110px","height":"28px",
                }))

        rows.append(html.Tr(cells, style={"height":"28px"}))

    week_label = f"{days[0].strftime('%-d %b')} – {days[6].strftime('%-d %b %Y')}"
    return html.Div([
        html.H6(f"Week of {week_label}", style={
            "fontWeight":"600","color":"#1e293b",
            "marginBottom":"12px","fontSize":"15px",
        }),
        html.Div(
            html.Table(rows, style={"width":"100%","borderCollapse":"collapse","background":"white"}),
            style={"overflowX":"auto","borderRadius":"10px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)"},
        ),
    ], style={"background":"white","borderRadius":"10px","padding":"16px"})


def build_month_calendar_html(events_by_date: dict, year: int, month: int):
    import calendar as cal_mod
    weeks      = cal_mod.monthcalendar(year, month)
    month_name = datetime(year, month, 1).strftime("%B %Y")
    today      = datetime.now().date()

    header = html.Tr([
        html.Th(d, style={
            "textAlign":"center","padding":"8px 4px","fontSize":"11px",
            "fontWeight":"600","color":"#64748b",
            "borderBottom":"2px solid #e2e8f0","background":"#f8fafc",
        }) for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    ])

    rows = [header]
    for week in weeks:
        cells = []
        for day_num in week:
            if day_num == 0:
                cells.append(html.Td(style={
                    "background":"#f8fafc","border":"1px solid #e2e8f0",
                    "minHeight":"80px","width":"14.28%",
                }))
            else:
                d          = datetime(year, month, day_num)
                date_str   = d.strftime("%Y-%m-%d")
                evs        = events_by_date.get(date_str, [])
                is_today   = d.date() == today
                is_weekend = d.weekday() >= 5

                pills = []
                for ev in evs[:3]:
                    title = ev["title"]
                    pills.append(html.Div(
                        (title[:18] + "…") if len(title) > 18 else title,
                        style={
                            "background":ev.get("color","#60a5fa"),
                            "color":"white","fontSize":"10px",
                            "padding":"1px 5px","borderRadius":"3px",
                            "marginTop":"2px","overflow":"hidden",
                            "whiteSpace":"nowrap","textOverflow":"ellipsis",
                        }
                    ))
                if len(evs) > 3:
                    pills.append(html.Div(
                        f"+{len(evs)-3} more",
                        style={"fontSize":"10px","color":"#94a3b8","marginTop":"2px"}
                    ))

                cell_bg = "#fff7ed" if is_today else ("#f8fafc" if is_weekend else "white")
                day_col = "#f97316" if is_today else ("#94a3b8" if is_weekend else "#1e293b")
                border  = "2px solid #f97316" if is_today else "1px solid #e2e8f0"

                cells.append(html.Td(
                    [html.Div(str(day_num), style={
                        "fontWeight":"700","fontSize":"13px",
                        "color":day_col,"marginBottom":"2px",
                    }), *pills],
                    style={
                        "verticalAlign":"top","padding":"6px 8px",
                        "border":border,"background":cell_bg,
                        "minHeight":"80px","width":"14.28%",
                    }
                ))
        rows.append(html.Tr(cells))

    return html.Div([
        html.H6(month_name, style={
            "fontWeight":"600","color":"#1e293b",
            "marginBottom":"12px","fontSize":"15px",
        }),
        html.Table(rows, style={
            "width":"100%","borderCollapse":"collapse",
            "background":"white","borderRadius":"8px","overflow":"hidden",
        }),
    ], style={
        "background":"white","borderRadius":"10px",
        "padding":"16px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
    })


def build_month_chart(events_by_date: dict, year: int, month: int) -> go.Figure:
    import calendar as cal_mod
    days_in_month = cal_mod.monthrange(year, month)[1]
    day_colors = {0:"#60a5fa",1:"#34d399",2:"#f97316",3:"#a78bfa",4:"#f43f5e",5:"#fbbf24",6:"#94a3b8"}
    dates, counts, hover_texts, colors_list = [], [], [], []
    for day_num in range(1, days_in_month+1):
        d        = datetime(year, month, day_num)
        date_str = d.strftime("%Y-%m-%d")
        evs      = events_by_date.get(date_str, [])
        titles   = ", ".join(e["title"] for e in evs[:3])
        if len(evs) > 3: titles += f" +{len(evs)-3} more"
        dates.append(f"{d.strftime('%d')}<br>{d.strftime('%a')}")
        counts.append(len(evs))
        hover_texts.append(titles or "No events")
        colors_list.append(day_colors[d.weekday()])

    if all(c == 0 for c in counts):
        fig = go.Figure()
        fig.add_annotation(text="No events this month.", x=0.5, y=0.5, showarrow=False,
                           font=dict(color="#475569",size=14), xref="paper", yref="paper")
        fig.update_layout(plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                          xaxis=dict(visible=False), yaxis=dict(visible=False),
                          margin=dict(l=8,r=8,t=8,b=8), height=300)
        return fig

    fig = go.Figure(go.Bar(
        x=dates, y=counts, marker_color=colors_list,
        text=[str(c) if c > 0 else "" for c in counts], textposition="outside",
        customdata=hover_texts,
        hovertemplate="<b>%{x}</b><br>%{y} event(s)<br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        plot_bgcolor="rgba(255,255,255,0.35)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#1e293b", size=11, family="DM Sans, sans-serif"),
        margin=dict(l=8,r=8,t=32,b=8),
        xaxis=dict(showgrid=False, title="", color="#4b5563", tickfont=dict(size=10)),
        yaxis=dict(showgrid=True, gridcolor="rgba(0,0,0,0.06)",
                   title="Events", dtick=1, color="#4b5563"),
        height=340, bargap=0.2,
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL BRIEFING
# ─────────────────────────────────────────────────────────────────────────────

def _build_email_html(city: str, topics: list, user_name: str = "") -> str:
    w        = get_weather(city)
    events   = get_calendar_events(datetime.now().strftime("%Y-%m-%d"))
    news     = get_news(topics, 8)
    today    = datetime.now().strftime("%A, %B %d, %Y")
    greeting = f"Hello {user_name}," if user_name.strip() else "Hello,"

    # Schedule rows — time range + title + location
    if events:
        rows = "".join(
            f"<tr style='border-bottom:1px solid #fde68a'>"
            f"<td style='padding:8px 10px;color:#92400e;font-size:12px;white-space:nowrap'>"
            f"{e['time']}–{e.get('end','')}</td>"
            f"<td style='padding:8px 10px;color:#1e293b;font-weight:600'>{e['title']}</td>"
            f"<td style='padding:8px 10px;color:#64748b;font-size:12px'>{e.get('location','')}</td>"
            f"</tr>"
            for e in events
        )
        schedule_section = f"""
  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#0b8043;margin-top:0;font-size:15px">📅 Today's Schedule — {len(events)} event{'s' if len(events)>1 else ''}</h2>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
  </div>"""
    else:
        schedule_section = """
  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#0b8043;margin-top:0;font-size:15px">📅 Today's Schedule</h2>
    <p style="color:#64748b;margin:0">No events scheduled for today — enjoy your free day! 🎉</p>
  </div>"""

    # News items — with description
    news_items = "".join(
        f"<li style='margin-bottom:12px'>"
        f"<a href='{a['url']}' style='color:#1d6fad;text-decoration:none;font-weight:600'>{a['title']}</a><br>"
        f"<span style='color:#64748b;font-size:12px'>{(a.get('description') or '')[:100]}{'…' if len(a.get('description') or '')>100 else ''}</span><br>"
        f"<span style='color:#94a3b8;font-size:11px'>— {a['source']} · {a.get('time','')}</span>"
        f"</li>"
        for a in news
    ) or "<li style='color:#64748b'>No news available</li>"

    return f"""
<html><body style="background:#FAF1D6;color:#1e293b;
  font-family:'DM Sans',Arial,sans-serif;padding:36px;max-width:640px;margin:0 auto">
  <h1 style="color:#FC9F66;margin-bottom:2px">🌅 WakeFlow</h1>
  <p style="color:#64748b;margin-top:0;font-size:13px">{today} · Your Morning Briefing</p>
  <p style="font-size:15px;color:#1e293b;margin:12px 0 20px">{greeting} Here's your briefing for today.</p>

  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#e07b30;margin-top:0;font-size:15px">{w.get('icon','🌤️')} Weather — {w.get('city',city)}</h2>
    <span style="font-size:2.6rem;font-weight:700;color:#FC9F66">{w.get('temp','--')}°C</span>
    <span style="color:#64748b;margin-left:12px">{w.get('condition','--')} · Feels {w.get('feels_like','--')}°C · Humidity {w.get('humidity','--')}%</span>
  </div>

  {schedule_section}

  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#1d6fad;margin-top:0;font-size:15px">📰 Top Stories</h2>
    <ul style="padding-left:16px;margin:0">{news_items}</ul>
  </div>

  <p style="color:#b45309;font-size:11px;text-align:center;margin-top:24px">
    Sent by WakeFlow · ESADE PDAI
  </p>
</body></html>"""


def send_email(recipient: str, city: str, topics: list,
               user_name: str = "") -> tuple[bool, str]:
    """Send email via Brevo API."""
    api_key = BREVO_API_KEY
    if not api_key:
        return False, "❌ BREVO_API_KEY not set in Railway Variables"
    try:
        html_content = _build_email_html(city, topics, user_name=user_name)
        html_content = (html_content
            .replace("\xa0"," ").replace("\u200b","")
            .replace("\u2019","'").replace("\u2018","'")
            .replace("\u201c",'"').replace("\u201d",'"')
        )
        subject = f"WakeFlow · {datetime.now().strftime('%A, %B %d')}"
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "sender": {"name": "WakeFlow", "email": "wakeflow1303@gmail.com"},
                "to": [{"email": recipient}],
                "subject": subject,
                "htmlContent": html_content,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return True, f"✅ Briefing sent to {recipient}!"
        else:
            return False, f"❌ Brevo error {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"❌ {str(e)[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# DASH APP + GOOGLE OAUTH
# ─────────────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.BOOTSTRAP,
        "https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap",
    ],
    suppress_callback_exceptions=True,
    title="WakeFlow",
    update_title=None,
)

app.index_string = """<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<style>
  html, body { min-height: 100%; margin: 0; padding: 0; }
  body {
    background: linear-gradient(160deg,#FC9F66 0%,#FAC357 20%,#FAE39C 38%,#B8E0E3 58%,#97C5D8 78%,#84A9CD 100%);
    background-attachment: fixed; background-size: cover;
  }
  .wf-root { background: transparent !important; }
  .wf-card {
    background: rgba(255,255,255,0.55) !important;
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.7) !important;
    border-radius: 16px !important; padding: 20px;
    box-shadow: 0 4px 24px rgba(0,0,0,0.08); color: #1e293b !important;
  }
  .wf-card p,.wf-card li,.wf-card span,.wf-card div { color: #1e293b !important; }
  .wf-card strong,.wf-card b { color: #0f172a !important; }
  .wf-header {
    background: rgba(255,255,255,0.45) !important;
    backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border-radius: 16px; border: 1px solid rgba(255,255,255,0.6);
  }
  .wf-tabs .nav-link { color: #374151 !important; font-weight: 500; }
  .wf-tabs .nav-link.active {
    background: rgba(255,255,255,0.7) !important;
    border-radius: 10px 10px 0 0; color: #1e293b !important;
  }
  .wf-bubble {
    max-width: 80%; padding: 10px 14px; border-radius: 18px;
    font-size: 14px; line-height: 1.5; margin-bottom: 8px !important; word-wrap: break-word;
  }
  .wf-bubble-user {
    background: #FC9F66; color: #1e293b !important; margin-left: auto;
    border-bottom-right-radius: 4px; font-weight: 500;
    box-shadow: 0 2px 8px rgba(252,159,102,0.3);
  }
  .wf-bubble-ai {
    background: rgba(255,255,255,0.75); color: #1e293b !important;
    margin-right: auto; border-bottom-left-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); backdrop-filter: blur(8px);
  }
  .wf-bubble-ai p,.wf-bubble-ai li,.wf-bubble-ai strong { color: #1e293b !important; }
  #chat-window {
    display: flex; flex-direction: column; gap: 4px;
    max-height: 500px; overflow-y: auto; padding: 12px;
    background: rgba(255,255,255,0.25); border-radius: 16px; backdrop-filter: blur(8px);
  }
  .vote-btn { border-radius: 20px !important; padding: 2px 12px !important; font-size: 13px !important; transition: transform 0.1s; }
  .vote-btn:active { transform: scale(1.15); }
  .wf-upload-zone {
    border: 2px dashed rgba(255,255,255,0.6) !important;
    border-radius: 14px !important; background: rgba(255,255,255,0.2) !important;
    cursor: pointer; transition: background 0.2s;
  }
  .wf-upload-zone:hover { background: rgba(255,255,255,0.35) !important; }
  .wf-event-import-card {
    background: rgba(255,255,255,0.6); border: 1px solid rgba(255,255,255,0.75);
    border-radius: 12px; padding: 12px 16px; margin-bottom: 10px;
  }
</style>
</head>
<body>
{%app_entry%}
<footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>"""
server = app.server


@server.route("/connect-google")
def connect_google():
    if not GOOGLE_AVAILABLE or not os.path.exists(CLIENT_SECRETS_FILE):
        return "<p>Place credentials.json in the project folder first.</p>", 400
    from flask import request as freq
    email_hint = freq.args.get("email", "")
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=GOOGLE_SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    extra = {"prompt": "consent"}
    if email_hint:
        extra["login_hint"] = email_hint
    auth_url, state = flow.authorization_url(**extra)
    _oauth_flows[state] = flow
    return f"<script>window.location.href='{auth_url}'</script>"


@server.route("/oauth2callback")
def oauth2callback():
    from flask import request as freq
    state = freq.args.get("state", "")
    flow  = _oauth_flows.pop(state, None)
    if flow is None:
        flow = Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE, scopes=GOOGLE_SCOPES,
            redirect_uri=REDIRECT_URI,
        )
    callback_url = freq.url.replace("http://", "https://") if REDIRECT_URI.startswith("https") else freq.url
    flow.fetch_token(authorization_response=callback_url)
    with open(TOKEN_FILE, "w") as f:
        f.write(flow.credentials.to_json())
    return "<script>window.location.href='/'</script>"


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _bubble_ai(text):
    return html.Div(className="wf-bubble wf-bubble-ai mb-2", style={"alignSelf":"flex-start"}, children=[
        html.Div([
            html.Span("🤖 ", style={"fontSize":"16px"}),
            html.Span("WakeFlow", style={"fontSize":"11px","color":"#475569","fontWeight":"600"}),
        ], style={"marginBottom":"4px"}),
        dcc.Markdown(text, style={"margin":"0","fontSize":"13px","color":"#1e293b"}),
    ])

def _bubble_user(text):
    return html.Div(className="wf-bubble wf-bubble-user mb-2", children=text, style={"alignSelf":"flex-end"})


google_connected = os.path.exists(TOKEN_FILE)
_init_fb    = load_feedback()
_init_stats = (f"👍 {_init_fb['thumbs_up']} · 👎 {_init_fb['thumbs_down']}"
               if (_init_fb["thumbs_up"] + _init_fb["thumbs_down"]) > 0 else "")

app.layout = dbc.Container(fluid=True, className="wf-root", children=[

    # Stores
    dcc.Store(id="events-store",           data=[]),
    dcc.Store(id="chat-store",             data=[]),
    dcc.Store(id="topics-store",           data=["Tech","Finance"]),
    dcc.Store(id="location-store",         data=None),
    dcc.Store(id="city-store",             data="Barcelona"),
    dcc.Store(id="email-settings",         data={}),
    dcc.Store(id="extracted-events-store", data=[]),
    dcc.Store(id="deleted-event-indices",  data=[]),
    dcc.Store(id="pending-event-store",    data=None),  # AI-created event waiting for calendar pick

    # ── Calendar Picker Modal (for AI create event) ───────────────────────
    dbc.Modal(id="calendar-picker-modal", is_open=False, centered=True, size="lg", children=[
        dbc.ModalHeader(dbc.ModalTitle("📅 Add Event to Google Calendar")),
        dbc.ModalBody([
            html.P("Review and complete your event details before adding:",
                   style={"color":"#64748b","fontSize":"13px","marginBottom":"14px"}),
            dbc.Label("Event Title *", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dbc.Input(id="modal-event-title", type="text", size="sm",
                      className="mb-2", style={"fontSize":"13px"}),
            dbc.Label("Description (optional)", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dbc.Textarea(id="modal-event-desc", size="sm", rows=2,
                         className="mb-2", style={"fontSize":"13px","resize":"none"}),
            dbc.Label("📍 Location (optional)", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dbc.Input(id="modal-event-location", type="text", size="sm",
                      placeholder="e.g. BCN Airport, ESADE Barcelona",
                      className="mb-2", style={"fontSize":"13px"}),
            dbc.Label("🔗 Meeting link (optional)", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dbc.Input(id="modal-event-meeting-link", type="url", size="sm",
                      placeholder="Zoom / Google Meet / Teams URL",
                      className="mb-2", style={"fontSize":"13px"}),
            dbc.Row(className="g-2 mb-1", children=[
                dbc.Col(width=5, children=[
                    dbc.Label("📆 Date *", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
                    dbc.Input(id="modal-event-date", type="text",
                              placeholder="YYYY-MM-DD", size="sm", style={"fontSize":"12px"}),
                ]),
                dbc.Col(width=3, children=[
                    dbc.Label("🕐 Start", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
                    dbc.Input(id="modal-event-start", type="text",
                              placeholder="HH:MM", size="sm", style={"fontSize":"12px"}),
                ]),
                dbc.Col(width=3, children=[
                    dbc.Label("🕑 End", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
                    dbc.Input(id="modal-event-end", type="text",
                              placeholder="HH:MM", size="sm", style={"fontSize":"12px"}),
                ]),
            ]),
            html.Small("Leave time blank = All day event",
                       style={"color":"#94a3b8","fontSize":"11px","display":"block","marginBottom":"10px"}),
            dbc.Row(className="g-2 mb-1", children=[
                dbc.Col(width=7, children=[
                    dbc.Label("Target Calendar", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
                    dcc.Dropdown(id="calendar-picker-dropdown",
                                 options=[{"label":"📅 Primary Calendar","value":"primary"}],
                                 value="primary", clearable=False, style={"fontSize":"13px"}),
                ]),
                dbc.Col(width=5, children=[
                    dbc.Label("🎨 Color", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
                    dcc.Dropdown(id="modal-event-color",
                                 options=[
                                     {"label":"⬜ Default","value":""},
                                     {"label":"🍅 Tomato","value":"11"},
                                     {"label":"🌸 Flamingo","value":"4"},
                                     {"label":"🍊 Tangerine","value":"6"},
                                     {"label":"🍌 Banana","value":"5"},
                                     {"label":"🌿 Sage","value":"2"},
                                     {"label":"🌲 Basil","value":"10"},
                                     {"label":"🫐 Peacock","value":"7"},
                                     {"label":"🫐 Blueberry","value":"9"},
                                     {"label":"💜 Lavender","value":"1"},
                                     {"label":"🍇 Grape","value":"3"},
                                     {"label":"🩶 Graphite","value":"8"},
                                 ],
                                 value="", clearable=False, style={"fontSize":"12px"}),
                ]),
            ]),
            html.Div(id="calendar-picker-event-preview"),
        ]),
        dbc.ModalFooter([
            dbc.Button("✕ Cancel",    id="calendar-picker-cancel",  color="secondary", size="sm", n_clicks=0),
            dbc.Button("➕ Add Event", id="calendar-picker-confirm", color="success",   size="sm", n_clicks=0),
        ]),
    ]),

    # ── Task Picker Modal ─────────────────────────────────────────────────
    dcc.Store(id="pending-task-store", data=None),
    dbc.Modal(id="task-picker-modal", is_open=False, centered=True, children=[
        dbc.ModalHeader(dbc.ModalTitle("✅ Add to My Tasks")),
        dbc.ModalBody([
            html.P("Review and complete your task details:",
                   style={"color":"#64748b","fontSize":"13px","marginBottom":"14px"}),
            dbc.Label("Task *", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dbc.Input(id="modal-task-text", type="text", size="sm",
                      className="mb-2", style={"fontSize":"13px"}),
            dbc.Label("Category", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dcc.Dropdown(id="modal-task-category",
                         options=[
                             {"label":"📋 To-Do",     "value":"todo"},
                             {"label":"📚 Assignment", "value":"assignment"},
                             {"label":"💼 Work",       "value":"work"},
                             {"label":"🏠 Personal",   "value":"personal"},
                         ],
                         value="todo", clearable=False,
                         style={"fontSize":"13px","marginBottom":"10px"}),
            dbc.Label("📅 Due Date (optional)", style={"fontWeight":"600","fontSize":"12px","color":"#374151"}),
            dcc.DatePickerSingle(id="modal-task-due", placeholder="No due date",
                                 display_format="D MMM YYYY", clearable=True,
                                 style={"fontSize":"12px"}),
        ]),
        dbc.ModalFooter([
            dbc.Button("✕ Cancel",   id="task-picker-cancel",  color="secondary", size="sm", n_clicks=0),
            dbc.Button("✅ Add Task", id="task-picker-confirm", color="warning",   size="sm", n_clicks=0),
        ]),
    ]),

    # Header
    dbc.Row(className="wf-header align-items-center py-3 mb-2", children=[
        dbc.Col(width=7, children=[
            html.Div(className="d-flex align-items-center gap-3", children=[
                html.Span("🌅", style={"fontSize":"2.2rem"}),
                html.Div([
                    html.H3("WakeFlow", className="wf-logo mb-0"),
                    html.P(id="header-date-display", className="wf-subtitle mb-0"),
                ]),
            ]),
        ]),
        dbc.Col(width=5, className="d-flex justify-content-end align-items-center gap-2", children=[
        ]),
    ]),

    # Tabs
    dbc.Tabs(id="main-tabs", active_tab="tab-settings", className="wf-tabs", children=[

        # ── Tab 1: My Day ────────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-day", label="📅 My Day", children=[
            dbc.Row(className="mt-3 g-3", children=[

                # ── Left: Calendar chart ──────────────────────────────────────
                dbc.Col(width=8, children=[
                    dbc.Tabs(id="view-tabs", active_tab="view-day",
                             className="mb-3", children=[

                        dbc.Tab(tab_id="view-day", label="Day", children=[
                            html.Div(className="d-flex align-items-center gap-3 mt-2 mb-2", children=[
                                dcc.DatePickerSingle(
                                    id="date-picker", date=(__import__('pytz').timezone('Europe/Madrid') and datetime.now(__import__('pytz').timezone('Europe/Madrid')).date()),
                                    display_format="D MMM YYYY", className="wf-datepicker",
                                ),
                                html.Div(id="selected-date-label",
                                         style={"fontWeight":"600","fontSize":"15px","color":"#1e293b"}),
                            ]),
                        ]),

                        dbc.Tab(tab_id="view-week", label="Week", children=[
                            html.Div(className="d-flex align-items-center gap-3 mt-2 mb-2", children=[
                                dcc.DatePickerSingle(
                                    id="week-picker", date=datetime.now().date(),
                                    display_format="D MMM YYYY", className="wf-datepicker",
                                ),
                                html.Div(id="selected-week-label",
                                         style={"fontWeight":"600","fontSize":"15px","color":"#1e293b"}),
                            ]),
                        ]),

                        dbc.Tab(tab_id="view-month", label="Month", children=[
                            html.Div(className="d-flex align-items-center gap-3 mt-2 mb-2", children=[
                                dcc.Dropdown(
                                    id="month-select",
                                    options=[{"label": m, "value": i} for i, m in enumerate(
                                        ["January","February","March","April","May","June",
                                         "July","August","September","October","November","December"], 1
                                    )],
                                    value=datetime.now().month,
                                    clearable=False,
                                    style={"width":"140px","fontSize":"13px"},
                                ),
                                dcc.Dropdown(
                                    id="year-select",
                                    options=[{"label": str(y), "value": y}
                                             for y in range(datetime.now().year - 2, datetime.now().year + 3)],
                                    value=datetime.now().year,
                                    clearable=False,
                                    style={"width":"90px","fontSize":"13px"},
                                ),
                                html.Div(id="selected-month-label",
                                         style={"fontWeight":"600","fontSize":"15px","color":"#1e293b"}),
                            ]),
                        ]),
                    ]),

                    # Chart area
                    dcc.Loading(type="circle", color="#f97316", children=[
                        html.Div(
                            dcc.Graph(id="gantt-chart",
                                      config={"displayModeBar":False,"scrollZoom":False,"doubleClick":False}),
                            style={"background":"white","borderRadius":"10px",
                                   "padding":"16px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)"}
                        ),
                        html.Div(id="week-calendar",  style={"display":"none"}),
                        html.Div(id="month-calendar", style={"display":"none"}),
                    ]),
                    html.P("👆 Click any event to see AI tips below",
                           className="wf-hint mt-1"),

                    # Event panel — shown BELOW the chart when event is clicked
                    html.Div(id="event-panel", style={"display":"none"},
                             className="wf-card mt-3"),
                ]),

                # ── Right: AI Chat ────────────────────────────────────────────
                dbc.Col(width=4, children=[
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"16px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                        "height":"100%",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-2", children=[
                            html.Span("🤖", style={"fontSize":"1.1rem"}),
                            html.Span("AI Assistant", style={
                                "fontWeight":"700","fontSize":"14px","color":"#1e293b"}),
                        ]),
                        html.Div(id="chat-window",
                                 style={"height":"400px","overflowY":"auto","marginBottom":"8px"},
                                 children=[
                            _bubble_ai("Hey! 👋 Ask me about your schedule, weather, or anything else!"),
                        ]),
                        dbc.InputGroup(children=[
                            dbc.Input(id="chat-input",
                                      placeholder="Ask about your day...",
                                      type="text", className="wf-input",
                                      debounce=False, n_submit=0,
                                      style={"fontSize":"13px"}),
                            dbc.Button("↑", id="send-btn", color="warning",
                                       n_clicks=0, style={"fontWeight":"700"}),
                        ]),
                        html.Div(className="d-flex align-items-center gap-2 mt-2", children=[
                            html.Small("Rate:", style={"color":"#64748b","fontSize":"11px"}),
                            dbc.Button("👍", id="vote-up-btn", size="sm",
                                       color="outline-success", n_clicks=0, className="vote-btn"),
                            dbc.Button("👎", id="vote-down-btn", size="sm",
                                       color="outline-danger", n_clicks=0, className="vote-btn"),
                            html.Div(id="vote-status",
                                     style={"fontSize":"11px","color":"#34d399"}),
                            html.Div(id="vote-stats", className="ms-auto",
                                     style={"display":"none"}),
                        ]),
                    ]),
                ]),
            ]),
        ]),

        # ── Tab 2: My Planner ────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-planner", label="📋 My Planner", children=[
            dbc.Row(className="mt-3 g-3", children=[

                # ── Left column: Manual Add + Upload ────────────────────────
                dbc.Col(width=6, children=[
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                        "marginBottom":"16px",
                    }, children=[

                        # Calendar selector (shared by both tabs)
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("🗓️", style={"fontSize":"14px","color":"#64748b"}),
                            dcc.Dropdown(
                                id="planner-topic-dropdown",
                                options=[{"label":"📅 Primary Calendar","value":"primary"}],
                                placeholder="Select target calendar...",
                                clearable=True,
                                style={"flex":"1","fontSize":"13px","minWidth":"180px"},
                            ),
                        ]),

                        dbc.Tabs(id="planner-mode-tabs", active_tab="tab-manual", children=[

                            # ── Tab A: Add Manually ───────────────────────────
                            dbc.Tab(tab_id="tab-manual", label="✏️ Add Event", children=[
                                html.Div(className="mt-3", children=[
                                    dbc.Input(
                                        id="manual-event-title",
                                        placeholder="Event title *",
                                        type="text", size="sm", className="mb-2",
                                        style={"fontSize":"13px"},
                                    ),
                                    dbc.Textarea(
                                        id="manual-event-desc",
                                        placeholder="Description (optional)",
                                        size="sm", rows=2, className="mb-2",
                                        style={"fontSize":"13px","resize":"none"},
                                    ),
                                    dbc.Input(
                                        id="manual-event-location",
                                        placeholder="📍 Location (optional) — e.g. BCN Airport",
                                        type="text", size="sm", className="mb-2",
                                        style={"fontSize":"13px"},
                                    ),
                                    dbc.Input(
                                        id="manual-event-meeting-link",
                                        placeholder="🔗 Meeting link (optional) — Zoom, Google Meet, Teams",
                                        type="url", size="sm", className="mb-2",
                                        style={"fontSize":"13px"},
                                    ),
                                    dbc.Row(className="g-2 mb-2", children=[
                                        dbc.Col(width=5, children=[
                                            html.Small("📆 Date", style={"color":"#64748b","fontSize":"11px"}),
                                            dcc.DatePickerSingle(
                                                id="manual-event-date",
                                                placeholder="Date",
                                                display_format="D MMM YYYY",
                                                clearable=True,
                                                style={"fontSize":"12px","width":"100%"},
                                            ),
                                        ]),
                                        dbc.Col(width=3, children=[
                                            html.Small("🕐 Start", style={"color":"#64748b","fontSize":"11px"}),
                                            dbc.Input(
                                                id="manual-event-start",
                                                placeholder="HH:MM",
                                                type="text", size="sm",
                                                style={"fontSize":"12px"},
                                            ),
                                        ]),
                                        dbc.Col(width=3, children=[
                                            html.Small("🕑 End", style={"color":"#64748b","fontSize":"11px"}),
                                            dbc.Input(
                                                id="manual-event-end",
                                                placeholder="HH:MM",
                                                type="text", size="sm",
                                                style={"fontSize":"12px"},
                                            ),
                                        ]),
                                    ]),
                                    html.Small("Leave time blank = All day event",
                                               style={"color":"#94a3b8","fontSize":"11px","display":"block","marginBottom":"10px"}),
                                    dbc.Button("➕ Add to Google Calendar",
                                               id="manual-add-btn", color="success", size="sm",
                                               n_clicks=0, className="w-100"),
                                    html.Div(id="manual-add-status", className="mt-2"),
                                ]),
                            ]),

                            # ── Tab B: Upload File ────────────────────────────
                            dbc.Tab(tab_id="tab-upload", label="📎 Upload File", children=[
                                html.Div(className="mt-3", children=[
                                    html.P("AI will extract events from your file and let you review before adding.",
                                           style={"fontSize":"12px","color":"#64748b","marginBottom":"10px"}),
                                    dcc.Upload(
                                        id="planner-upload",
                                        children=html.Div([
                                            html.Div("📂", style={"fontSize":"2rem","marginBottom":"6px"}),
                                            html.Div("Drag & drop or click to upload",
                                                     style={"fontWeight":"600","fontSize":"13px","color":"#1e293b"}),
                                            html.Div("PDF, PNG, JPG supported",
                                                     style={"fontSize":"11px","color":"#94a3b8","marginTop":"2px"}),
                                        ], style={"textAlign":"center","padding":"24px 16px"}),
                                        accept=".pdf,.png,.jpg,.jpeg",
                                        multiple=False,
                                        style={
                                            "border":"2px dashed #cbd5e1","borderRadius":"12px",
                                            "background":"rgba(249,250,251,0.8)","cursor":"pointer",
                                        },
                                    ),
                                    dcc.Loading(type="circle", color="#f97316",
                                                children=html.Div(id="planner-result", className="mt-3")),
                                ]),
                            ]),
                        ]),
                    ]),
                ]),

                # ── Task Manager ─────────────────────────────────────────────
                dbc.Col(width=6, children=[
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("✅", style={"fontSize":"1.3rem"}),
                            html.H6("My Tasks", style={"fontWeight":"700","color":"#1e293b","margin":"0"}),
                        ]),
                        html.Div(className="d-flex gap-2 mb-2 flex-wrap", children=[
                            dcc.Dropdown(
                                id="task-category-select",
                                options=[
                                    {"label":"📋 To-Do",      "value":"todo"},
                                    {"label":"📚 Assignment",  "value":"assignment"},
                                    {"label":"💼 Work",        "value":"work"},
                                    {"label":"🏠 Personal",    "value":"personal"},
                                ],
                                value="todo", clearable=False,
                                style={"width":"145px","fontSize":"12px"},
                            ),
                            dbc.Input(
                                id="task-input",
                                placeholder="Add a new task...",
                                type="text", size="sm", n_submit=0,
                                className="wf-input", style={"fontSize":"13px","flex":"1"},
                            ),
                            dbc.Button("➕", id="task-add-btn",
                                       color="warning", size="sm", n_clicks=0),
                        ]),
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Small("Due:", style={"color":"#64748b","fontSize":"12px","whiteSpace":"nowrap"}),
                            dcc.DatePickerSingle(
                                id="task-due-date",
                                placeholder="No due date",
                                display_format="D MMM YYYY",
                                clearable=True,
                                style={"fontSize":"12px"},
                            ),
                        ]),
                        html.Div(id="task-list-display"),
                        dcc.Store(id="task-store", storage_type="local", data=[]),
                    ]),
                ]),
            ]),
        ]),

        # ── Tab 3: Weather ───────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-weather", label="🌤️ Weather", children=[
            html.Div(className="mt-3", children=[
                # City search bar
                html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                    html.Span("📍", style={"fontSize":"16px"}),
                    dbc.Input(
                        id="weather-city-input",
                        placeholder="Search city — e.g. Tokyo, London, Bangkok...",
                        type="text",
                        debounce=True,
                        size="sm",
                        style={"maxWidth":"320px","fontSize":"13px"},
                    ),
                ]),
                dbc.Row(className="g-3", children=[
                    dbc.Col(width=5, children=[html.Div(id="weather-card", className="wf-card")]),
                    dbc.Col(width=7, children=[dcc.Graph(id="weather-chart", config={"displayModeBar":False})]),
                ]),
            ]),
        ]),

        # ── Tab 4: News ──────────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-news", label="📰 News", children=[
            html.Div(className="mt-3", children=[
                # Topic filter chips
                html.Div(className="d-flex align-items-center gap-2 flex-wrap mb-3", children=[
                    html.Span("Filter:", style={"fontSize":"13px","color":"#64748b","fontWeight":"600"}),
                    dbc.Checklist(
                        id="news-topic-filter",
                        options=[{"label": f"{e} {t}", "value": t}
                                 for t, e in [("Tech","🤖"),("Finance","📈"),("World","🌏"),
                                              ("Business","💼"),("Science","🔬"),("Sports","⚽")]],
                        value=["Tech","Finance"],
                        inline=True,
                        inputClassName="btn-check",
                        labelClassName="btn btn-sm btn-outline-secondary me-1 mb-1",
                        labelCheckedClassName="btn btn-sm btn-warning me-1 mb-1",
                        style={"fontSize":"12px"},
                    ),
                ]),
                html.Div(id="news-list"),
            ]),
        ]),

        # ── Tab 5: User Info ─────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-settings", label="👤 User Info", children=[
            dbc.Row(className="mt-3 g-3", children=[

                # ── Google Calendar ──────────────────────────────────────────
                dbc.Col(width=6, children=[
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                        "marginBottom":"16px",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("🗓️", style={"fontSize":"1.4rem"}),
                            html.H6("Google Calendar", style={"fontWeight":"700","color":"#1e293b","margin":"0"}),
                        ]),
                        html.P("Connect your Google account to sync your calendar events with WakeFlow.",
                               style={"fontSize":"13px","color":"#64748b","marginBottom":"16px"}),

                        # Dynamic status — updated by callback
                        html.Div(id="gcal-status-section"),
                        dcc.Interval(id="gcal-status-interval", interval=3000, n_intervals=0),
                    ]),

                    # ── City Setting ─────────────────────────────────────────
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                        "marginBottom":"16px",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("📍", style={"fontSize":"1.4rem"}),
                            html.H6("Your City", style={"fontWeight":"700","color":"#1e293b","margin":"0"}),
                        ]),
                        html.P("Set your default city for weather and morning briefing.",
                               style={"fontSize":"13px","color":"#64748b","marginBottom":"12px"}),
                        dbc.Input(
                            id="city-input", value="Barcelona",
                            placeholder="e.g. Barcelona, Bangkok, London...",
                            debounce=True, className="wf-input",
                        ),
                        html.P("Used in Weather tab, AI Assistant, and daily email.",
                               style={"fontSize":"11px","color":"#94a3b8","marginTop":"8px","marginBottom":"0"}),
                    ]),

                    # ── News Preferences ──────────────────────────────────────
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("📰", style={"fontSize":"1.4rem"}),
                            html.H6("News Preferences", style={"fontWeight":"700","color":"#1e293b","margin":"0"}),
                        ]),
                        html.P("Choose the topics you're interested in — used in News tab and daily email.",
                               style={"fontSize":"13px","color":"#64748b","marginBottom":"12px"}),
                        dbc.Checklist(
                            id="topics-check",
                            options=[{"label": f" {e}  {t}", "value": t}
                                     for t, e in [("Tech","🤖"),("Finance","📈"),("World","🌏"),
                                                  ("Business","💼"),("Science","🔬"),("Sports","⚽")]],
                            value=["Tech","Finance"],
                            inline=True,
                            className="wf-checklist",
                        ),
                    ]),
                ]),

                # ── Daily Email ───────────────────────────────────────────────
                dbc.Col(width=6, children=[

                    # Daily Email
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("📧", style={"fontSize":"1.4rem"}),
                            html.H6("Daily Morning Briefing Email", style={"fontWeight":"700","color":"#1e293b","margin":"0"}),
                        ]),
                        html.P("Get your calendar, weather, and news delivered to your inbox every morning.",
                               style={"fontSize":"13px","color":"#64748b","marginBottom":"16px"}),

                        dbc.Label("👤 Your name (for email greeting)",
                                  style={"fontWeight":"600","fontSize":"13px","color":"#374151"}),
                        dbc.Input(id="user-name-input", type="text",
                                  placeholder="e.g. Eng",
                                  className="wf-input mb-3"),

                        dbc.Label("💌 Send briefing to",
                                  style={"fontWeight":"600","fontSize":"13px","color":"#374151"}),
                        dbc.Input(id="email-input", type="email",
                                  placeholder="your@email.com",
                                  className="wf-input mb-3"),

                        dbc.Label("⏰ Send daily at",
                                  style={"fontWeight":"600","fontSize":"13px","color":"#374151"}),
                        dcc.Dropdown(
                            id="email-time-dropdown",
                            options=[{"label": f"{h:02d}:00", "value": h} for h in range(5, 12)],
                            value=7, clearable=False, className="wf-dropdown mb-1",
                            style={"maxWidth":"160px"},
                        ),
                        html.P(id="email-time-display",
                               style={"color":"#94a3b8","fontSize":"11px","marginBottom":"16px"}),

                        dbc.Row(className="g-2 mb-2", children=[
                            dbc.Col(width=6, children=[
                                dbc.Button("📨 Send Now", id="send-email-btn",
                                           color="warning", n_clicks=0, size="sm", className="w-100"),
                            ]),
                            dbc.Col(width=6, children=[
                                dbc.Button("⏰ Save Schedule", id="save-schedule-btn",
                                           color="success", n_clicks=0, size="sm", className="w-100"),
                            ]),
                        ]),

                        html.Div(id="email-status",    className="mt-2"),
                        html.Div(id="schedule-status", className="mt-1"),
                        html.Div(id="schedule-info",   className="mt-1",
                                 style={"fontSize":"12px"}),
                    ]),

                    # ── Tool Usage Analytics ──────────────────────────────────
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                        "marginTop":"16px",
                    }, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-3", children=[
                            html.Span("📊", style={"fontSize":"1.4rem"}),
                            html.H6("AI Tool Usage Analytics", style={"fontWeight":"700","color":"#1e293b","margin":"0"}),
                        ]),
                        html.P("Tracks which AI tools the chatbot calls most often.",
                               style={"fontSize":"12px","color":"#64748b","marginBottom":"12px"}),
                        html.Div(id="tool-usage-display"),
                        dcc.Interval(id="tool-usage-interval", interval=10000, n_intervals=0),
                    ]),
                ]),

            ]),
        ]),

    ]),

    html.Div(className="pb-4"),
])


# ─────────────────────────────────────────────────────────────────────────────
# CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

@callback(Output("city-store","data"), Input("city-input","value"))
def cb_store_city(city):
    return city or "Barcelona"


@callback(Output("header-date-display", "children"), Input("gcal-status-interval", "n_intervals"))
def cb_header_date(n):
    try:
        import pytz
        now = datetime.now(pytz.timezone("Europe/Madrid"))
    except Exception:
        now = datetime.now()
    return now.strftime("%A, %B %d, %Y")


# Sync: User Info checklist → News filter (one-way only to avoid circular)
@callback(
    Output("news-topic-filter", "value"),
    Input("topics-check", "value"),
)
def cb_sync_to_news_filter(topics):
    return topics or ["Tech", "Finance"]


@callback(
    Output("tool-usage-display", "children"),
    Input("tool-usage-interval", "n_intervals"),
)
def cb_tool_usage(n):
    usage = load_tool_usage()
    total = sum(usage.values())
    if total == 0:
        return html.P("No tool calls yet — start chatting with the AI! 🤖",
                      style={"color":"#94a3b8","fontSize":"12px"})
    labels = {
        "get_calendar_events":   "📅 Check Calendar",
        "get_weather":           "🌤️ Get Weather",
        "get_news":              "📰 Fetch News",
        "create_calendar_event": "➕ Create Event",
        "add_task":              "✅ Add Task",
        "delete_calendar_event": "🗑️ Delete Event",
    }
    rows = []
    for key, label in labels.items():
        count = usage.get(key, 0)
        pct   = int(count / total * 100) if total else 0
        rows.append(html.Div(className="mb-2", children=[
            html.Div(className="d-flex justify-content-between mb-1", children=[
                html.Span(label, style={"fontSize":"12px","color":"#374151"}),
                html.Span(f"{count} calls ({pct}%)", style={"fontSize":"11px","color":"#94a3b8"}),
            ]),
            html.Div(style={"background":"#f1f5f9","borderRadius":"4px","height":"6px"}, children=[
                html.Div(style={
                    "background":"#f97316","borderRadius":"4px",
                    "height":"6px","width":f"{pct}%","transition":"width 0.3s",
                }),
            ]),
        ]))
    rows.append(html.Small(f"Total: {total} tool calls",
                           style={"color":"#94a3b8","fontSize":"11px","marginTop":"6px","display":"block"}))
    return html.Div(rows)


@callback(
    Output("gcal-status-section", "children"),
    Input("gcal-status-interval", "n_intervals"),
)
def cb_gcal_status(n):
    if os.path.exists(TOKEN_FILE):
        return html.Div([
            dbc.Alert([html.Span("✅  "), "Google Calendar is connected!"],
                      color="success", className="py-2 mb-2"),
            html.A(
                html.Small("Reconnect with a different account →",
                           style={"color":"#94a3b8","fontSize":"11px"}),
                href="/connect-google", target="_blank",
            ),
        ])
    return html.Div([
        dbc.Label("Google account email to connect",
                  style={"fontWeight":"600","fontSize":"13px","color":"#374151"}),
        dbc.Input(
            id="gcal-email-input", type="email",
            placeholder="yourname@gmail.com",
            className="wf-input mb-3",
        ),
        html.A(
            dbc.Button("🔌 Connect Google Calendar", color="primary", size="sm"),
            href="/connect-google", target="_blank",
        ),
    ])


@callback(Output("email-time-display","children"), Input("email-time-dropdown","value"))
def cb_time_display(hour):
    return f"📌 Selected: {hour:02d}:00" if hour is not None else ""


@callback(
    Output("selected-date-label", "children"),
    Input("date-picker", "date"),
)
def cb_date_label(selected_date):
    d = datetime.fromisoformat(str(selected_date)) if selected_date else datetime.now()
    return d.strftime("%A, %-d %B %Y")


@callback(
    Output("selected-week-label", "children"),
    Input("week-picker", "date"),
)
def cb_week_label(selected_date):
    from datetime import timedelta
    d      = datetime.fromisoformat(str(selected_date)) if selected_date else datetime.now()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime('%-d %b')} – {sunday.strftime('%-d %b %Y')}"


@callback(
    Output("selected-month-label", "children"),
    Input("month-select", "value"),
    Input("year-select",  "value"),
)
def cb_month_label(sel_month, sel_year):
    year  = sel_year  or datetime.now().year
    month = sel_month or datetime.now().month
    return datetime(year, month, 1).strftime("%B %Y")


@callback(
    Output("gantt-chart",    "figure"),
    Output("gantt-chart",    "style"),
    Output("week-calendar",  "children"),
    Output("week-calendar",  "style"),
    Output("month-calendar", "children"),
    Output("month-calendar", "style"),
    Output("events-store",   "data"),
    Input("view-tabs",    "active_tab"),
    Input("date-picker",  "date"),
    Input("week-picker",  "date"),
    Input("month-select", "value"),
    Input("year-select",  "value"),
)
def cb_update_gantt(active_tab, day_date, week_date, sel_month, sel_year):
    from datetime import timedelta
    import calendar as cal_mod
    hide = {"display":"none"}
    show = {"display":"block"}
    active_tab = active_tab or "view-day"

    if active_tab == "view-day":
        date_str = str(day_date) if day_date else datetime.now().strftime("%Y-%m-%d")
        events   = get_calendar_events(date_str)
        return (build_gantt(events, date_str), show,
                no_update, hide, no_update, hide, events)

    elif active_tab == "view-week":
        date_str = str(week_date) if week_date else datetime.now().strftime("%Y-%m-%d")
        d        = datetime.fromisoformat(date_str)
        monday   = d - timedelta(days=d.weekday())
        sunday   = monday + timedelta(days=6)
        ebd      = get_calendar_events_range(
            monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))
        all_events = [e for evs in ebd.values() for e in evs]
        return (go.Figure(), hide,
                build_week_html(ebd, monday.strftime("%Y-%m-%d")), show,
                no_update, hide, all_events)

    else:  # view-month
        year  = sel_year  or datetime.now().year
        month = sel_month or datetime.now().month
        days_in_month = cal_mod.monthrange(year, month)[1]
        start_str = f"{year}-{month:02d}-01"
        end_str   = f"{year}-{month:02d}-{days_in_month:02d}"
        ebd       = get_calendar_events_range(start_str, end_str)
        all_events = [e for evs in ebd.values() for e in evs]
        return (go.Figure(), hide,
                no_update, hide,
                build_month_calendar_html(ebd, year, month), show,
                all_events)


@callback(
    Output("event-panel",    "children"),
    Output("event-panel",    "style"),
    Output("location-store", "data"),
    Input("gantt-chart",     "clickData"),
    State("events-store",    "data"),
    State("city-store",      "data"),
    prevent_initial_call=True,
)
def cb_click_event(click_data, events, city):
    if not click_data or not events:
        return no_update, {"display":"none"}, no_update
    try:
        title = click_data["points"][0]["customdata"][0]
    except (KeyError, IndexError):
        return no_update, {"display":"none"}, no_update

    event = next((e for e in events if e["title"] == title), None)
    if not event:
        return no_update, {"display":"none"}, no_update

    w       = get_weather(city or "Barcelona")
    ai_text = ""
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp   = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=160,
                messages=[
                    {"role":"system","content":"You are WakeFlow. Give 3 short bullet tips for this event. Max 80 words. Use emojis."},
                    {"role":"user","content":
                        f"Event: {event['title']} ({event['type']}) {event['time']}-{event['end']}\n"
                        f"Location: {event.get('location') or 'not specified'}\n"
                        f"Weather: {w.get('temp','--')}C {w.get('condition','--')}"},
                ],
            )
            ai_text = resp.choices[0].message.content
        except Exception:
            pass

    if not ai_text:
        defaults = {
            "meeting":  "📋 Prep 3 talking points. ⏰ Join 2 min early. ✍️ Take notes live.",
            "class":    "📚 Skim last session's notes. 💡 Sit near the front. ✨ Note 1 key takeaway.",
            "deadline": "⏰ Start now, buffer 30 min. 🔇 Silence notifications. ✅ Done > perfect.",
            "personal": "🧘 Protect this block. 📵 Step away from screens. ✨ Rest = better focus.",
        }
        ai_text = defaults.get(event["type"], "✨ Make the most of this block!")

    icon         = {"meeting":"🤝","class":"📚","personal":"🧘","deadline":"⏰"}.get(event["type"],"📌")
    location_str = event.get("location","").strip()

    # Join Meeting button
    meet_link = event.get("meet_link","")
    join_btn  = html.Div()
    if meet_link:
        platform = ("Google Meet" if "meet.google" in meet_link else
                    "Zoom"        if "zoom.us"     in meet_link else
                    "Teams"       if "teams.microsoft" in meet_link else "Meeting")
        join_btn = html.A(
            dbc.Button(f"🎥 Join {platform}", color="primary", size="sm",
                       className="w-100 mb-2", style={"fontWeight":"600"}),
            href=meet_link, target="_blank",
        )

    map_section = html.Div()
    loc_data    = None
    if location_str:
        loc_data = {"raw":location_str,"city_hint":city or "","title":title}
        from urllib.parse import quote
        encoded    = quote(location_str)
        short_name = location_str.split(",")[0].strip()
        gmaps_embed = f"https://maps.google.com/maps?q={encoded}&output=embed&z=16"
        gmaps_dir   = f"https://www.google.com/maps/dir/?api=1&destination={encoded}"
        map_section = html.Div([
            html.Hr(className="wf-divider"),
            html.Div(className="d-flex align-items-center gap-2 mb-2", children=[
                html.Span("📍"),
                html.Span(short_name, style={"color":"#475569","fontSize":"12px"}),
            ]),
            html.Iframe(src=gmaps_embed,
                        style={"width":"100%","height":"220px","border":"0","borderRadius":"10px","marginBottom":"8px"}),
            html.A(
                dbc.Button("🧭 Get Route → Google Maps", color="primary", size="sm",
                           className="w-100", style={"fontSize":"12px"}),
                href=gmaps_dir, target="_blank",
            ),
            html.Div(id="map-area"),
        ])

    return html.Div([
        join_btn,
        html.H6(f"{icon} {event['title']}", className="wf-card-title"),
        html.P(f"{event['time']} – {event['end']}  ·  {event['type'].capitalize()}",
               style={"color":"#374151","fontSize":"12px","marginBottom":"10px"}),
        dcc.Markdown(ai_text, style={"color":"#e2e8f0","fontSize":"13px","lineHeight":"1.7"}),
        map_section,
    ]), {"display":"block"}, loc_data


@callback(
    Output("map-area","children"),
    Input("map-btn","n_clicks"),
    State("location-store","data"),
    prevent_initial_call=True,
)
def cb_show_map(n, loc_data):
    if not n or not loc_data:
        return no_update
    geo = geocode_location(loc_data["raw"], loc_data.get("city_hint",""))
    if not geo:
        return dbc.Alert(f"Could not find '{loc_data['raw']}' on map.",
                         color="warning", style={"fontSize":"12px"})
    lat, lon = geo["lat"], geo["lon"]
    fig = go.Figure(go.Scattermapbox(
        lat=[lat], lon=[lon], mode="markers+text",
        marker=go.scattermapbox.Marker(size=16, color="#f97316"),
        text=[loc_data["raw"]], textposition="top right",
        hovertemplate=f"<b>{loc_data['raw']}</b><extra></extra>",
    ))
    fig.update_layout(
        mapbox=dict(style="open-street-map", center=dict(lat=lat,lon=lon), zoom=15),
        margin=dict(l=0,r=0,t=0,b=0), height=200, paper_bgcolor="rgba(0,0,0,0)",
    )
    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
    return html.Div([
        dcc.Graph(figure=fig, config={"displayModeBar":False,"scrollZoom":True},
                  style={"borderRadius":"10px","overflow":"hidden"}),
        html.A(
            dbc.Button("🧭 Get Route → Google Maps", color="primary", size="sm",
                       className="w-100 mt-2", style={"fontSize":"12px"}),
            href=gmaps, target="_blank",
        ),
    ])


@callback(
    Output("chat-window",                   "children"),
    Output("chat-store",                    "data"),
    Output("chat-input",                    "value"),
    Output("pending-event-store",           "data"),
    Output("calendar-picker-modal",         "is_open"),
    Output("calendar-picker-event-preview", "children"),
    Output("modal-event-title",             "value"),
    Output("modal-event-date",              "value"),
    Output("modal-event-start",             "value"),
    Output("modal-event-end",               "value"),
    Output("pending-task-store",            "data"),
    Output("task-picker-modal",             "is_open"),
    Output("modal-task-text",               "value"),
    Output("modal-task-category",           "value"),
    Input("send-btn",      "n_clicks"),
    Input("chat-input",    "n_submit"),
    State("chat-input",    "value"),
    State("chat-store",    "data"),
    State("city-store",    "data"),
    State("topics-check",  "value"),
    prevent_initial_call=True,
)
def cb_chat(n_clicks, n_submit, user_text, history, city, topics):
    user_text = user_text or ""
    if not user_text.strip():
        return (no_update,) * 14

    city    = city    or "Barcelona"
    topics  = topics  or ["Tech","Finance"]
    history = history or []

    bubbles = [_bubble_ai("Hey! I'm WakeFlow 👋 Ask me anything — "
                           "I'll check your calendar, weather, and news automatically.")]

    try:
        import pytz
        local_tz  = pytz.timezone("Europe/Madrid")
        now_local = datetime.now(local_tz)
    except Exception:
        now_local = datetime.now()
    today_tag = now_local.strftime("%A %Y-%m-%d")
    enriched  = f"[TODAY: {today_tag}] [CITY: {city}] [TOPICS: {', '.join(topics)}] {user_text}"
    history.append({"role": "user", "content": enriched})

    reply, pending_event, pending_task = chat_with_tools(history, city, topics)
    history.append({"role": "assistant", "content": reply})

    for m in history:
        if m["role"] == "user":
            display = m["content"].split("] ")[-1] if "] " in m["content"] else m["content"]
            bubbles.append(_bubble_user(display))
        elif m["role"] == "assistant":
            bubbles.append(_bubble_ai(m["content"]))

    # Open event modal
    if pending_event:
        ev = pending_event
        return (
            bubbles, history, "",
            pending_event, True, html.Div(),
            ev.get("title", ""), ev.get("date", ""),
            ev.get("start_time", "") or "", ev.get("end_time", "") or "",
            no_update, False, no_update, no_update,
        )

    # Open task modal
    if pending_task:
        pt = pending_task
        return (
            bubbles, history, "",
            no_update, False, no_update,
            no_update, no_update, no_update, no_update,
            pending_task, True,
            pt.get("text", ""), pt.get("category", "todo"),
        )

    return (bubbles, history, "",
            None, False, no_update,
            no_update, no_update, no_update, no_update,
            None, False, no_update, no_update)


# ── Calendar Picker Modal Callbacks ───────────────────────────────────────────

@callback(
    Output("calendar-picker-dropdown", "options"),
    Input("calendar-picker-modal", "is_open"),
)
def cb_load_picker_calendars(is_open):
    default = [{"label":"📅 Primary Calendar","value":"primary"}]
    if not is_open or not (GOOGLE_AVAILABLE and os.path.exists(TOKEN_FILE)):
        return default
    try:
        creds    = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service  = gapi_build("calendar","v3",credentials=creds)
        cal_list = service.calendarList().list().execute()
        opts = [{"label": f"🗓️ {c['summary']}", "value": c["id"]}
                for c in cal_list.get("items",[]) if c.get("summary")]
        return opts if opts else default
    except Exception:
        return default


@callback(
    Output("calendar-picker-modal",  "is_open", allow_duplicate=True),
    Output("chat-window",            "children", allow_duplicate=True),
    Input("calendar-picker-confirm", "n_clicks"),
    Input("calendar-picker-cancel",  "n_clicks"),
    State("pending-event-store",      "data"),
    State("calendar-picker-dropdown", "value"),
    State("modal-event-title",        "value"),
    State("modal-event-desc",         "value"),
    State("modal-event-location",     "value"),
    State("modal-event-meeting-link", "value"),
    State("modal-event-date",         "value"),
    State("modal-event-start",        "value"),
    State("modal-event-end",          "value"),
    State("modal-event-color",        "value"),
    State("chat-store",               "data"),
    prevent_initial_call=True,
)
def cb_calendar_picker_action(confirm_clicks, cancel_clicks, pending_event,
                               cal_id, edit_title, edit_desc, edit_location,
                               edit_meeting_link, edit_date, edit_start, edit_end,
                               edit_color, history):
    triggered = ctx.triggered_id
    if triggered == "calendar-picker-cancel" or not pending_event:
        return False, no_update

    cal_id = cal_id or "primary"

    # Build description with meeting link appended if provided
    desc = (edit_desc or "").strip()
    link = (edit_meeting_link or "").strip()
    if link:
        desc = (desc + "\n\n" if desc else "") + f"🔗 Join Meeting: {link}"

    merged = {
        **pending_event,
        "title":       (edit_title    or "").strip() or pending_event.get("title", "New Event"),
        "date":        (edit_date     or "").strip() or pending_event.get("date", ""),
        "start_time":  (edit_start    or "").strip() or None,
        "end_time":    (edit_end      or "").strip() or None,
        "description": desc or pending_event.get("description") or None,
        "location":    (edit_location or "").strip() or pending_event.get("location") or None,
        "colorId":     edit_color or None,
    }

    ok, result = create_calendar_event(merged, calendar_id=cal_id)

    bubbles = [_bubble_ai("Hey! I'm WakeFlow 👋 Ask me anything — "
                           "I'll check your calendar, weather, and news automatically.")]
    for m in (history or []):
        if m["role"] == "user":
            display = m["content"].split("] ")[-1] if "] " in m["content"] else m["content"]
            bubbles.append(_bubble_user(display))
        elif m["role"] == "assistant":
            bubbles.append(_bubble_ai(m["content"]))

    if ok:
        time_str = (f"{merged['start_time']} – {merged['end_time']}"
                    if merged.get("start_time") else "All day")
        bubbles.append(_bubble_ai(
            f"✅ **{merged['title']}** added on {merged['date']} ({time_str}). "
            "Refresh My Day to see it! 📅"
        ))
    else:
        bubbles.append(_bubble_ai(f"❌ Could not add event: {result}"))

    return False, bubbles


@callback(
    Output("task-picker-modal",  "is_open", allow_duplicate=True),
    Output("task-store",         "data", allow_duplicate=True),
    Output("chat-window",        "children", allow_duplicate=True),
    Input("task-picker-confirm", "n_clicks"),
    Input("task-picker-cancel",  "n_clicks"),
    State("pending-task-store",  "data"),
    State("modal-task-text",     "value"),
    State("modal-task-category", "value"),
    State("modal-task-due",      "date"),
    State("task-store",          "data"),
    State("chat-store",          "data"),
    prevent_initial_call=True,
)
def cb_task_picker_action(confirm_clicks, cancel_clicks, pending_task,
                           text, category, due_date, tasks, history):
    triggered = ctx.triggered_id
    if triggered == "task-picker-cancel" or not pending_task:
        return False, no_update, no_update

    tasks   = tasks or []
    next_id = max((t["id"] for t in tasks), default=-1) + 1
    task_text = (text or "").strip() or pending_task.get("text", "New Task")
    tasks.append({
        "id":       next_id,
        "text":     task_text,
        "category": category or pending_task.get("category", "todo"),
        "done":     False,
        "due":      due_date or pending_task.get("due_date") or None,
    })

    bubbles = [_bubble_ai("Hey! I'm WakeFlow 👋 Ask me anything — "
                           "I'll check your calendar, weather, and news automatically.")]
    for m in (history or []):
        if m["role"] == "user":
            display = m["content"].split("] ")[-1] if "] " in m["content"] else m["content"]
            bubbles.append(_bubble_user(display))
        elif m["role"] == "assistant":
            bubbles.append(_bubble_ai(m["content"]))
    bubbles.append(_bubble_ai(f"✅ Task **{task_text}** added to My Tasks!"))

    return False, tasks, bubbles
def _normalize_time(t: str) -> str | None:
    """Convert various time formats (16.35, 1635, 16:35) → '16:35', or None if blank."""
    if not t:
        return None
    t = t.strip().replace(".", ":").replace(",", ":")
    # Handle 4-digit no-separator: 1635 → 16:35
    if len(t) == 4 and t.isdigit():
        t = t[:2] + ":" + t[2:]
    # Validate HH:MM
    parts = t.split(":")
    if len(parts) == 2:
        try:
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
        except ValueError:
            pass
    return None


@callback(
    Output("manual-add-status",         "children"),
    Output("manual-event-title",        "value"),
    Output("manual-event-desc",         "value"),
    Output("manual-event-location",     "value"),
    Output("manual-event-meeting-link", "value"),
    Output("manual-event-date",         "date"),
    Output("manual-event-start",        "value"),
    Output("manual-event-end",          "value"),
    Input("manual-add-btn",             "n_clicks"),
    State("manual-event-title",         "value"),
    State("manual-event-desc",          "value"),
    State("manual-event-location",      "value"),
    State("manual-event-meeting-link",  "value"),
    State("manual-event-date",          "date"),
    State("manual-event-start",         "value"),
    State("manual-event-end",           "value"),
    State("planner-topic-dropdown",     "value"),
    prevent_initial_call=True,
)
def cb_manual_add_event(n, title, desc, location, meeting_link, date, start_raw, end_raw, cal_id):
    if not n:
        return (no_update,) * 8
    if not title or not title.strip():
        return (dbc.Alert("⚠️ Please enter an event title.", color="warning"),
                *([no_update] * 7))
    if not date:
        return (dbc.Alert("⚠️ Please select a date.", color="warning"),
                *([no_update] * 7))

    start_time = _normalize_time(start_raw)
    end_time   = _normalize_time(end_raw)

    if start_raw and start_raw.strip() and start_time is None:
        return (dbc.Alert(f"⚠️ Invalid start time '{start_raw}'. Use HH:MM format (e.g. 09:30).", color="warning"),
                *([no_update] * 7))

    # Append meeting link into description
    full_desc = (desc or "").strip()
    link = (meeting_link or "").strip()
    if link:
        full_desc = (full_desc + "\n\n" if full_desc else "") + f"🔗 Join Meeting: {link}"

    event_data = {
        "title":       title.strip(),
        "date":        date,
        "start_time":  start_time,
        "end_time":    end_time,
        "description": full_desc or None,
        "location":    (location or "").strip() or None,
    }
    ok, result = create_calendar_event(event_data, calendar_id=cal_id or "primary")
    if ok:
        return (
            dbc.Alert("🎉 Event added to Google Calendar! Refresh My Day to see it.", color="success"),
            "", "", "", "", None, "", "",
        )
    else:
        tip = ""
        if "insufficientPermissions" in result or "forbidden" in result.lower():
            tip = " — Try selecting Primary Calendar instead."
        return (
            dbc.Alert(f"❌ Failed: {result}{tip}", color="danger"),
            *([no_update] * 7),
        )


# ── Planner calendar dropdown (load from Google Calendar) ─────────────────────
@callback(
    Output("planner-topic-dropdown", "options"),
    Input("gcal-status-interval",    "n_intervals"),
)
def cb_load_planner_calendars(n):
    default = [{"label":"📅 Primary Calendar","value":"primary"}]
    if not (GOOGLE_AVAILABLE and os.path.exists(TOKEN_FILE)):
        return default
    try:
        creds    = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service  = gapi_build("calendar","v3",credentials=creds)
        cal_list = service.calendarList().list().execute()
        opts = [{"label":f"🗓️ {c['summary']}","value":c["id"]}
                for c in cal_list.get("items",[]) if c.get("summary")]
        return opts if opts else default
    except Exception:
        return default


@callback(
    Output("vote-status", "children"),
    Output("vote-stats",  "children"),
    Input("vote-up-btn",   "n_clicks"),
    Input("vote-down-btn", "n_clicks"),
    State("chat-store",    "data"),
    prevent_initial_call=True,
)
def cb_vote(up_clicks, down_clicks, history):
    if not ctx.triggered_id:
        return no_update, no_update

    triggered = ctx.triggered_id
    if triggered not in ("vote-up-btn", "vote-down-btn"):
        return no_update, no_update

    if not history or not any(m["role"] == "assistant" for m in history):
        return "💬 Ask me something first!", no_update

    fb   = load_feedback()
    vote = "up" if triggered == "vote-up-btn" else "down"
    last_ai = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")

    if vote == "up":
        fb["thumbs_up"] += 1
        status = "👍 Thanks!"
    else:
        fb["thumbs_down"] += 1
        status = "👎 Got it, noted!"

    fb["log"].append({
        "vote":            vote,
        "timestamp":       datetime.now().isoformat(),
        "message_preview": last_ai[:120],
    })
    save_feedback(fb)

    total = fb["thumbs_up"] + fb["thumbs_down"]
    stats = f"👍 {fb['thumbs_up']} · 👎 {fb['thumbs_down']}" if total > 0 else ""
    return status, stats


@callback(
    Output("weather-card",  "children"),
    Output("weather-chart", "figure"),
    Input("city-store",          "data"),
    Input("weather-city-input",  "value"),
)
def cb_weather(city_store, city_weather):
    city = (city_weather or "").strip() if ctx.triggered_id == "weather-city-input" \
           else (city_store or "Barcelona")
    city = city or "Barcelona"
    w    = get_weather(city)

    if w.get("error"):
        empty_fig = go.Figure()
        empty_fig.update_layout(
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(visible=False), yaxis=dict(visible=False),
            margin=dict(l=10,r=10,t=20,b=10), height=300,
        )
        empty_fig.add_annotation(text=w.get("message","Weather unavailable"),
            x=0.5, y=0.5, showarrow=False,
            font=dict(color="#9ca3af", size=13), xref="paper", yref="paper")
        return html.Div([
            html.H2(f"📍 {city}", className="wf-card-title"),
            html.P("⚠️ " + w.get("message","Weather data unavailable."),
                   style={"color":"#9ca3af","fontSize":"13px"}),
        ]), empty_fig

    t = w["temp"]
    if   t >= 35: advice, color = "🥵 Very hot! Stay hydrated.", "#dc2626"
    elif t >= 30: advice, color = "☀️ Hot. Wear light clothing.", "#ea580c"
    elif t >= 20: advice, color = "🌤️ Pleasant — great day outside!", "#059669"
    elif t >= 10: advice, color = "🧥 Cool today. Bring a jacket.", "#2563eb"
    else:         advice, color = "🧤 Cold! Layer up.", "#7c3aed"

    card = html.Div([
        html.H2(f"{w['icon']} {w['city']}", className="wf-card-title"),
        html.Div(f"{t}°C", style={"fontSize":"3.5rem","fontWeight":"700","color":"#ea580c","lineHeight":"1.1"}),
        html.P(w["condition"], style={"color":"#6b7280","fontSize":"16px","margin":"4px 0 12px"}),
        html.Hr(className="wf-divider"),
        dbc.Row([
            dbc.Col([html.P("Feels like",className="wf-stat-label"), html.H5(f"{w['feels_like']}°C",className="wf-stat-val")]),
            dbc.Col([html.P("Humidity",  className="wf-stat-label"), html.H5(f"{w['humidity']}%", className="wf-stat-val")]),
        ]),
        html.Hr(className="wf-divider"),
        html.P(advice, style={"color":color,"fontWeight":"600","margin":"0"}),
    ])

    fig = go.Figure()
    fig.add_trace(go.Bar(x=["Temp","Feels Like"], y=[t,w["feels_like"]],
        marker_color=["#ea580c","#f59e0b"], text=[f"{t}°C",f"{w['feels_like']}°C"],
        textposition="outside", marker_line_width=0, yaxis="y1"))
    fig.add_trace(go.Bar(x=["Humidity"], y=[w["humidity"]],
        marker_color=["#60a5fa"], text=[f"{w['humidity']}%"],
        textposition="outside", marker_line_width=0, yaxis="y2"))
    fig.update_layout(
        plot_bgcolor="rgba(255,255,255,0.0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#1e293b", family="DM Sans, sans-serif"),
        showlegend=False, height=300, margin=dict(l=10,r=10,t=30,b=10),
        yaxis=dict(showgrid=False, showticklabels=False, zeroline=False, overlaying="y", range=[0,130]),
        yaxis2=dict(showgrid=False, showticklabels=False, zeroline=False, overlaying="y", range=[0,130]),
        bargap=0.4, barmode="group",
    )
    return card, fig


@callback(
    Output("news-list",    "children"),
    Output("topics-store", "data"),
    Input("news-topic-filter", "value"),
)
def cb_news(topics):
    topics   = topics or ["Tech"]
    articles = get_news(topics, 12)
    if not articles:
        return dbc.Alert("No news — add NEWS_API_KEY to your .env file", color="secondary"), topics

    # Color palette cycling per topic
    TOPIC_COLORS = {
        "Tech":     ("#6366f1", "#eef2ff"),  # indigo
        "Finance":  ("#059669", "#ecfdf5"),  # emerald
        "World":    ("#0ea5e9", "#f0f9ff"),  # sky
        "Business": ("#f59e0b", "#fffbeb"),  # amber
        "Science":  ("#8b5cf6", "#f5f3ff"),  # violet
        "Sports":   ("#ef4444", "#fef2f2"),  # red
    }
    DEFAULT_COLORS = ("#f97316", "#fff7ed")

    cards = []
    for a in articles:
        topic    = a.get("topic", topics[0] if topics else "Tech")
        accent, bg = TOPIC_COLORS.get(topic, DEFAULT_COLORS)
        cards.append(
            html.Div(style={
                "background": bg,
                "borderRadius": "12px",
                "padding": "16px 18px",
                "marginBottom": "10px",
                "borderLeft": f"4px solid {accent}",
                "boxShadow": "0 1px 3px rgba(0,0,0,0.06)",
            }, children=[
                html.Div(className="d-flex justify-content-between align-items-start gap-2", children=[
                    html.Div(style={"flex":"1"}, children=[
                        html.Div(className="d-flex align-items-center gap-2 mb-1", children=[
                            dbc.Badge(topic, style={
                                "background": accent, "fontSize":"10px",
                                "padding":"2px 8px","borderRadius":"20px",
                            }),
                            dbc.Badge(a["source"], color="light",
                                      text_color="secondary", style={"fontSize":"10px"}),
                            html.Span(a["time"], style={"fontSize":"11px","color":"#94a3b8"}),
                        ]),
                        html.Strong(a["title"], style={
                            "fontSize":"14px","color":"#1e293b",
                            "lineHeight":"1.4","display":"block","marginBottom":"4px",
                        }),
                        html.P((a["description"] or "")[:120] + ("…" if len(a.get("description","")) > 120 else ""),
                               style={"fontSize":"12px","color":"#64748b","margin":"0"}),
                    ]),
                    html.A("Read →", href=a["url"], target="_blank", style={
                        "fontSize":"12px","color": accent,"fontWeight":"600",
                        "whiteSpace":"nowrap","textDecoration":"none","paddingTop":"2px",
                    }),
                ]),
            ])
        )
    return html.Div(cards), topics


@callback(
    Output("email-status",  "children"),
    Input("send-email-btn", "n_clicks"),
    State("email-input",     "value"),
    State("user-name-input", "value"),
    State("city-store",      "data"),
    State("topics-check",    "value"),
    prevent_initial_call=True,
)
def cb_send_email(n, recipient, user_name, city, topics):
    if not recipient:
        return dbc.Alert("Please enter a recipient email address.", color="warning")
    if not BREVO_API_KEY:
        return dbc.Alert("❌ BREVO_API_KEY not set in Railway Variables", color="danger")
    ok, msg = send_email(recipient, city or "Barcelona", topics or ["Tech","Finance"],
                         user_name=user_name or "")
    return dbc.Alert(msg, color="success" if ok else "danger", dismissable=True)


@callback(
    Output("schedule-status", "children"),
    Output("schedule-info",   "children"),
    Input("save-schedule-btn",   "n_clicks"),
    State("email-input",         "value"),
    State("user-name-input",     "value"),
    State("email-time-dropdown", "value"),
    State("city-store",          "data"),
    State("topics-check",        "value"),
    prevent_initial_call=True,
)
def cb_save_schedule(n, recipient, user_name, hour, city, topics):
    if not recipient:
        return dbc.Alert("Please enter a recipient email address.", color="warning"), no_update
    if not BREVO_API_KEY:
        return dbc.Alert("❌ BREVO_API_KEY not set in Railway Variables", color="danger"), no_update

    if _scheduled_job["job"]:
        try:
            _scheduled_job["job"].remove()
        except Exception:
            pass

    city      = city      or "Barcelona"
    topics    = topics    or ["Tech","Finance"]
    user_name = user_name or ""

    def _send():
        send_email(recipient, city, topics, user_name=user_name)

    job = scheduler.add_job(_send, trigger="cron", hour=hour, minute=0,
                            id="daily_briefing", replace_existing=True)
    _scheduled_job["job"] = job

    info = html.Div([
        html.Span("⏰ Scheduled: ", style={"fontWeight":"600","color":"#0b8043"}),
        html.Span(f"Daily at {hour:02d}:00", style={"color":"#374151"}), html.Br(),
        html.Span("📬 To: ", style={"color":"#475569"}),
        html.Span(recipient, style={"color":"#374151"}),
    ])
    return (dbc.Alert(f"✅ Schedule saved! Daily briefing at {hour:02d}:00.",
                      color="success", dismissable=True), info)


# ── Task Manager Callbacks ────────────────────────────────────────────────────
CAT_COLORS = {
    "todo":       ("#f97316","📋"),
    "assignment": ("#8b5cf6","📚"),
    "work":       ("#0891b2","💼"),
    "personal":   ("#059669","🏠"),
}

@callback(
    Output("task-store",        "data"),
    Output("task-input",        "value"),
    Output("task-due-date",     "date"),
    Input("task-add-btn",       "n_clicks"),
    Input("task-input",         "n_submit"),
    State("task-input",         "value"),
    State("task-category-select","value"),
    State("task-due-date",      "date"),
    State("task-store",         "data"),
    prevent_initial_call=True,
)
def cb_add_task(n_btn, n_sub, text, category, due_date, tasks):
    if not text or not text.strip():
        return no_update, no_update, no_update
    tasks = tasks or []
    new_id = max((t["id"] for t in tasks), default=-1) + 1
    tasks.append({
        "id":       new_id,
        "text":     text.strip(),
        "category": category or "todo",
        "done":     False,
        "due":      str(due_date) if due_date else None,
    })
    return tasks, "", None


@callback(
    Output("task-store", "data", allow_duplicate=True),
    Input({"type":"task-check","index": dash.ALL}, "value"),
    State("task-store", "data"),
    prevent_initial_call=True,
)
def cb_toggle_task(checked_values, tasks):
    if not tasks or not ctx.triggered_id:
        return no_update
    triggered_idx = ctx.triggered_id["index"]
    # Find position in inputs_list matching the triggered index
    all_inputs = ctx.inputs_list[0]
    pos = next((i for i, inp in enumerate(all_inputs)
                if inp["id"]["index"] == triggered_idx), None)
    if pos is None:
        return no_update
    is_done = bool(checked_values[pos])
    for t in tasks:
        if t["id"] == triggered_idx:
            t["done"] = is_done
            break
    return tasks


@callback(
    Output("task-store", "data", allow_duplicate=True),
    Input({"type":"task-delete","index": dash.ALL}, "n_clicks"),
    State("task-store", "data"),
    prevent_initial_call=True,
)
def cb_delete_task(n_clicks_list, tasks):
    if not any(n_clicks_list) or not ctx.triggered_id:
        return no_update
    idx = ctx.triggered_id["index"]
    return [t for t in (tasks or []) if t["id"] != idx]


@callback(
    Output("task-list-display", "children"),
    Input("task-store", "data"),
)
def cb_render_tasks(tasks):
    if not tasks:
        return html.P("No tasks yet — add one above! 🎯",
                      style={"color":"#94a3b8","fontSize":"13px","textAlign":"center","marginTop":"20px"})

    today = datetime.now().date()
    cats = {}
    for t in tasks:
        cats.setdefault(t["category"], []).append(t)

    sections = []
    for cat, items in cats.items():
        color, icon = CAT_COLORS.get(cat, ("#64748b","📌"))
        done_count  = sum(1 for t in items if t["done"])
        header = html.Div(className="d-flex align-items-center gap-2 mb-2", children=[
            html.Span(icon, style={"fontSize":"14px"}),
            html.Span(cat.capitalize(), style={"fontWeight":"600","fontSize":"13px","color":color}),
            html.Span(f"{done_count}/{len(items)}", style={"fontSize":"11px","color":"#94a3b8","marginLeft":"4px"}),
        ])
        rows = []
        for t in items:
            done = t["done"]

            # Due date badge
            due_badge = html.Span()
            if t.get("due") and not done:
                try:
                    due_d = datetime.fromisoformat(t["due"]).date()
                    days_left = (due_d - today).days
                    if days_left < 0:
                        due_label = f"⚠️ Overdue {abs(days_left)}d"
                        due_color = "danger"
                    elif days_left == 0:
                        due_label = "⏰ Due today"
                        due_color = "warning"
                    elif days_left <= 3:
                        due_label = f"📅 {days_left}d left"
                        due_color = "warning"
                    else:
                        due_label = f"📅 {due_d.strftime('%-d %b')}"
                        due_color = "secondary"
                    due_badge = dbc.Badge(due_label, color=due_color,
                                          style={"fontSize":"10px","marginLeft":"4px"})
                except Exception:
                    pass

            rows.append(html.Div(className="d-flex align-items-center gap-2 py-1", style={
                "borderBottom":"1px solid #f1f5f9",
            }, children=[
                dbc.Checklist(
                    options=[{"label":"","value":1}],
                    value=[1] if done else [],
                    id={"type":"task-check","index":t["id"]},
                    inline=True, style={"margin":"0"},
                ),
                html.Div([
                    html.Span(t["text"], style={
                        "fontSize":"13px","color":"#94a3b8" if done else "#1e293b",
                        "textDecoration":"line-through" if done else "none",
                    }),
                    due_badge,
                ], style={"flex":"1"}),
                html.Span("✕", id={"type":"task-delete","index":t["id"]},
                          style={"cursor":"pointer","color":"#cbd5e1","fontSize":"12px","padding":"0 4px"},
                          n_clicks=0),
            ]))
        sections.append(html.Div([header, *rows], className="mb-3"))

    return html.Div(sections)


# ─────────────────────────────────────────────────────────────────────────────
@callback(
    Output("planner-result",         "children"),
    Output("extracted-events-store", "data"),
    Output("deleted-event-indices",  "data"),
    Input("planner-upload",          "contents"),
    State("planner-upload",          "filename"),
    State("planner-topic-dropdown",  "value"),
    State("planner-topic-dropdown",  "options"),
    prevent_initial_call=True,
)
def cb_process_upload(contents, filename, selected_topic, cal_options):
    if not contents or not filename:
        return no_update, no_update, no_update

    try:
        _ctype, content_string = contents.split(",", 1)
        decoded = base64.b64decode(content_string)
    except Exception:
        return dbc.Alert("Could not read file.", color="danger"), [], []

    ext    = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    events = []

    if ext == "pdf":
        text = ""
        try:
            import io
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(decoded)) as pdf:
                    text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            except ImportError:
                try:
                    import PyPDF2
                    reader = PyPDF2.PdfReader(io.BytesIO(decoded))
                    text = "\n".join(p.extract_text() or "" for p in reader.pages)
                except ImportError:
                    return (dbc.Alert("PDF processing requires pdfplumber. Run: pip install pdfplumber",
                                      color="warning"), [], [])
        except Exception as e:
            return dbc.Alert(f"Error reading PDF: {e}", color="danger"), [], []
        if not text.strip():
            return dbc.Alert("Could not extract text from PDF — try uploading an image instead.", color="warning"), [], []
        events = extract_events_from_text(text)

    elif ext in ("png","jpg","jpeg"):
        mime   = f"image/{'jpeg' if ext == 'jpg' else ext}"
        events = extract_events_from_image(content_string, mime)
    else:
        return dbc.Alert("Please upload a PDF or image file.", color="warning"), [], []

    if not events:
        return dbc.Alert("No events found. Try a document with clear dates and times.", color="warning"), [], []

    # Look up calendar name from options
    cal_name = "Primary Calendar"
    if selected_topic and cal_options:
        match = next((o["label"] for o in cal_options if o["value"] == selected_topic), None)
        if match:
            cal_name = match.replace("🗓️ ","").replace("📅 ","").strip()

    # Render extracted event cards — editable/customizable, with delete button
    _google_ok = os.path.exists(TOKEN_FILE)
    cards = []
    for i, ev in enumerate(events):
        cards.append(html.Div(
            id={"type":"event-card","index":i},
            style={"background":"#f8fafc","borderRadius":"10px",
                   "padding":"12px","marginBottom":"10px","border":"1px solid #e2e8f0"},
            children=[
                # Title row + delete button
                html.Div(className="d-flex justify-content-between align-items-start mb-2", children=[
                    html.Strong(ev.get("title","Untitled"),
                                style={"color":"#1e293b","fontSize":"14px","flex":"1"}),
                    html.Span("✕",
                              id={"type":"event-delete-btn","index":i},
                              n_clicks=0,
                              style={"cursor":"pointer","color":"#cbd5e1","fontSize":"14px",
                                     "fontWeight":"bold","padding":"0 4px","lineHeight":"1",
                                     "marginLeft":"8px"},
                    ),
                ]),
                # Destination toggle
                html.Div(className="d-flex align-items-center gap-2 mb-2", children=[
                    html.Span("Add as:", style={"fontSize":"12px","color":"#64748b","whiteSpace":"nowrap"}),
                    dbc.RadioItems(
                        id={"type":"event-dest","index":i},
                        options=[
                            {"label":"📅 Calendar Event", "value":"calendar"},
                            {"label":"✅ Task",            "value":"task"},
                        ],
                        value="calendar",
                        inline=True,
                        inputStyle={"marginRight":"4px"},
                        labelStyle={"fontSize":"12px","marginRight":"12px","cursor":"pointer"},
                    ),
                ]),
                # Date + time row
                html.Div(className="d-flex gap-2 flex-wrap align-items-center", children=[
                    html.Span("📆", style={"fontSize":"13px"}),
                    dcc.Input(
                        id={"type":"event-date","index":i},
                        type="text", value=ev.get("date",""), placeholder="YYYY-MM-DD",
                        debounce=True,
                        style={"width":"130px","fontSize":"12px","padding":"3px 6px",
                               "border":"1px solid #cbd5e1","borderRadius":"6px"},
                    ),
                    html.Span("🕐", style={"fontSize":"13px"}),
                    dcc.Input(
                        id={"type":"event-start","index":i},
                        type="text", value=ev.get("start_time",""), placeholder="Start HH:MM",
                        debounce=True,
                        style={"width":"100px","fontSize":"12px","padding":"3px 6px",
                               "border":"1px solid #cbd5e1","borderRadius":"6px"},
                    ),
                    html.Span("–", style={"color":"#94a3b8"}),
                    dcc.Input(
                        id={"type":"event-end","index":i},
                        type="text", value=ev.get("end_time",""), placeholder="End HH:MM",
                        debounce=True,
                        style={"width":"100px","fontSize":"12px","padding":"3px 6px",
                               "border":"1px solid #cbd5e1","borderRadius":"6px"},
                    ),
                    html.Span("(blank = All day)", style={"fontSize":"11px","color":"#94a3b8"}),
                ]),
                # Color picker
                html.Div(className="d-flex align-items-center gap-2 mt-2", children=[
                    html.Span("🎨", style={"fontSize":"13px"}),
                    html.Span("Color:", style={"fontSize":"12px","color":"#64748b"}),
                    dcc.Dropdown(
                        id={"type":"event-color","index":i},
                        options=[
                            {"label":"⬜ Calendar default", "value":""},
                            {"label":"🍅 Tomato",          "value":"11"},
                            {"label":"🌸 Flamingo",        "value":"4"},
                            {"label":"🍊 Tangerine",       "value":"6"},
                            {"label":"🍌 Banana",          "value":"5"},
                            {"label":"🌿 Sage",            "value":"2"},
                            {"label":"🌲 Basil",           "value":"10"},
                            {"label":"🫐 Peacock",         "value":"7"},
                            {"label":"🫐 Blueberry",       "value":"9"},
                            {"label":"💜 Lavender",        "value":"1"},
                            {"label":"🍇 Grape",           "value":"3"},
                            {"label":"🩶 Graphite",        "value":"8"},
                        ],
                        value="",
                        clearable=False,
                        style={"fontSize":"12px","width":"180px"},
                    ),
                ]),
                html.P(ev.get("description",""),
                       style={"color":"#64748b","fontSize":"11px","margin":"6px 0 0"})
                if ev.get("description") else html.Div(),
            ]
        ))

    # Calendar target label (shown cleanly, not the raw ID)
    cal_badge = dbc.Badge(f"📅 → {cal_name}", color="success", className="ms-1") \
                if selected_topic else html.Span()

    add_section = html.Div(className="mt-3", children=[
        dbc.Button(
            f"➕ Add All {len(events)} Item{'s' if len(events) > 1 else ''}",
            id="add-all-events-btn", color="success", size="sm", n_clicks=0,
            disabled=not _google_ok,
        ),
        html.Div(id="add-events-status", className="mt-2"),
    ]) if _google_ok else dbc.Alert("🔌 Connect Google Calendar first to add events.", color="secondary", className="mt-2")

    return html.Div([
        html.Div(className="d-flex align-items-center gap-2 mb-3 flex-wrap", children=[
            dbc.Alert(f"✅ Found {len(events)} event{'s' if len(events)>1 else ''} in «{filename}»",
                      color="success", className="mb-0 py-2"),
            cal_badge,
        ]),
        *cards,
        add_section,
    ]), events, []


# ── Hide individual event card when ✕ is clicked ─────────────────────────────
@callback(
    Output({"type":"event-card","index":dash.MATCH}, "style"),
    Output("deleted-event-indices", "data", allow_duplicate=True),
    Input({"type":"event-delete-btn","index":dash.MATCH}, "n_clicks"),
    State("deleted-event-indices", "data"),
    prevent_initial_call=True,
)
def cb_hide_event_card(n, deleted):
    if not n:
        return no_update, no_update
    idx = ctx.triggered_id["index"]
    deleted = deleted or []
    if idx not in deleted:
        deleted = deleted + [idx]
    return {"display":"none"}, deleted


@callback(
    Output("add-all-events-btn", "children"),
    Input("deleted-event-indices",  "data"),
    State("extracted-events-store", "data"),
)
def cb_update_add_btn_label(deleted, events):
    total = len(events or [])
    remaining = total - len(deleted or [])
    if remaining <= 0:
        return "➕ Add All Items"
    return f"➕ Add All {remaining} Item{'s' if remaining > 1 else ''}"


@callback(
    Output("add-events-status",     "children"),
    Output("task-store",            "data", allow_duplicate=True),
    Input("add-all-events-btn",     "n_clicks"),
    State("extracted-events-store", "data"),
    State({"type":"event-dest",  "index": dash.ALL}, "value"),
    State({"type":"event-date",  "index": dash.ALL}, "value"),
    State({"type":"event-start", "index": dash.ALL}, "value"),
    State({"type":"event-end",   "index": dash.ALL}, "value"),
    State({"type":"event-color", "index": dash.ALL}, "value"),
    State("deleted-event-indices",  "data"),
    State("task-store",             "data"),
    State("planner-topic-dropdown", "value"),
    prevent_initial_call=True,
)
def cb_add_all_events(n, events, dests, dates, starts, ends, colors_list, deleted_indices, tasks, selected_cal_id):
    if not n or not events:
        return no_update, no_update

    tasks = tasks or []
    next_id = max((t["id"] for t in tasks), default=-1) + 1
    cal_id = selected_cal_id or "primary"
    deleted_set = set(deleted_indices or [])

    cal_success, task_added = 0, 0
    error_msgs = []

    for i, ev in enumerate(events):
        if i in deleted_set:
            continue

        dest       = dests[i]       if i < len(dests)        else "calendar"
        edit_date  = dates[i]       if i < len(dates)        else ev.get("date","")
        edit_start = starts[i]      if i < len(starts)       else ev.get("start_time","")
        edit_end   = ends[i]        if i < len(ends)         else ev.get("end_time","")
        color_id   = colors_list[i] if i < len(colors_list)  else ""

        merged = {**ev,
                  "date":       (edit_date  or "").strip() or ev.get("date",""),
                  "start_time": (edit_start or "").strip() or None,
                  "end_time":   (edit_end   or "").strip() or None,
                  "colorId":    color_id or None}

        if dest == "task":
            tasks.append({
                "id":       next_id,
                "text":     merged.get("title","Imported Task"),
                "category": "assignment",
                "done":     False,
                "due":      merged.get("date") or None,
            })
            next_id += 1
            task_added += 1
        else:
            ok, err = create_calendar_event(merged, calendar_id=cal_id)
            if ok:
                cal_success += 1
            else:
                error_msgs.append(f"«{merged.get('title','?')}»: {err}")

    # Build summary
    parts = []
    if cal_success:
        parts.append(f"📅 {cal_success} event{'s' if cal_success>1 else ''} added to Google Calendar")
    if task_added:
        parts.append(f"✅ {task_added} task{'s' if task_added>1 else ''} added to My Tasks")

    result_children = []
    if parts:
        color = "warning" if error_msgs else "success"
        msg = " · ".join(parts)
        if cal_success:
            msg += " — Refresh My Day tab!"
        result_children.append(dbc.Alert(f"🎉 {msg}", color=color, className="mb-2"))

    if error_msgs:
        result_children.append(dbc.Alert([
            html.Strong("❌ Some calendar events failed:"),
            html.Ul([html.Li(e, style={"fontSize":"12px"}) for e in error_msgs], className="mb-0 mt-1"),
            html.Small("💡 Tip: Group calendars (e.g. school calendars) may not allow write access. Try selecting Primary Calendar instead.",
                       style={"color":"#92400e"}),
        ], color="danger"))

    if not result_children:
        result_children = [dbc.Alert("Nothing was added.", color="warning")]

    return html.Div(result_children), tasks
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    app.run(debug=False, host="0.0.0.0", port=port)