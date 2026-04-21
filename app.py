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

scheduler = BackgroundScheduler()
scheduler.start()
_scheduled_job = {"job": None}

_APP_DIR            = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE          = os.path.join(_APP_DIR, "google_token.json")
CLIENT_SECRETS_FILE = os.path.join(_APP_DIR, "credentials.json")
FEEDBACK_FILE       = os.path.join(_APP_DIR, "feedback.json")

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
                            out.append({
                                "title":       a["title"],
                                "description": a.get("description") or "",
                                "source":      a["source"]["name"],
                                "time":        ago,
                                "url":         a.get("url",""),
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
                        meet_link   = hang_link or zoom_link or teams_link

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


def create_calendar_event(event_data: dict) -> tuple[bool, str]:
    """Create a single event in the user's primary Google Calendar."""
    if not GOOGLE_AVAILABLE or not os.path.exists(TOKEN_FILE):
        return False, "Google Calendar not connected."
    try:
        from datetime import timedelta
        creds   = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
        service = gapi_build("calendar", "v3", credentials=creds)

        date       = event_data.get("date", datetime.now().strftime("%Y-%m-%d"))
        start_time = event_data.get("start_time")
        end_time   = event_data.get("end_time")

        if start_time:
            start = {"dateTime": f"{date}T{start_time}:00", "timeZone": "Europe/Madrid"}
            if end_time:
                end = {"dateTime": f"{date}T{end_time}:00", "timeZone": "Europe/Madrid"}
            else:
                st  = datetime.strptime(f"{date}T{start_time}", "%Y-%m-%dT%H:%M")
                et  = st + timedelta(hours=1)
                end = {"dateTime": et.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": "Europe/Madrid"}
        else:
            start = {"date": date}
            end   = {"date": date}

        body = {"summary": event_data.get("title", "Imported Event"), "start": start, "end": end}
        if event_data.get("location"):
            body["location"] = event_data["location"]
        if event_data.get("description"):
            body["description"] = event_data["description"]

        result = service.events().insert(calendarId="primary", body=body).execute()
        return True, result.get("htmlLink", "")
    except Exception as e:
        err = str(e)
        if "insufficientPermissions" in err:
            return False, "Need write permission — reconnect Google Calendar."
        return False, err


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
]


def _run_tool(name: str, args: dict, city: str, topics: list) -> str:
    if name == "get_weather":
        return json.dumps(get_weather(args.get("city",city)))
    if name == "get_news":
        arts = get_news(args.get("topics",topics), args.get("n",5))
        return json.dumps({"articles":[{"title":a["title"],"source":a["source"]} for a in arts]})
    if name == "get_calendar_events":
        d = args.get("date", datetime.now().strftime("%Y-%m-%d"))
        return json.dumps({"date":d,"events":get_calendar_events(d)})
    return json.dumps({"error":f"Unknown tool: {name}"})


def chat_with_tools(history: list, city: str, topics: list) -> str:
    if not OPENAI_API_KEY:
        return "No OpenAI API key configured. Add OPENAI_API_KEY to your .env file."

    from openai import OpenAI
    client   = OpenAI(api_key=OPENAI_API_KEY)
    messages = [{"role":"system","content":SYSTEM_PROMPT}, *history]

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
                args   = json.loads(tc.function.arguments)
                result = _run_tool(tc.function.name, args, city, topics)
                messages.append({"role":"tool","tool_call_id":tc.id,"content":result})
            resp = client.chat.completions.create(
                model="gpt-4o-mini", messages=messages,
                tools=LLM_TOOLS, tool_choice="auto", max_tokens=500,
            )
            msg = resp.choices[0].message

        return msg.content or "Sorry, no response."
    except Exception as e:
        return f"AI error: {e}"


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

    # Header
    dbc.Row(className="wf-header align-items-center py-3 mb-2", children=[
        dbc.Col(width=7, children=[
            html.Div(className="d-flex align-items-center gap-3", children=[
                html.Span("🌅", style={"fontSize":"2.2rem"}),
                html.Div([
                    html.H3("WakeFlow", className="wf-logo mb-0"),
                    html.P(datetime.now().strftime("%A, %B %d, %Y"), className="wf-subtitle mb-0"),
                ]),
            ]),
        ]),
        dbc.Col(width=5, className="d-flex justify-content-end align-items-center gap-2", children=[
        ]),
    ]),

    # Tabs
    dbc.Tabs(id="main-tabs", active_tab="tab-day", className="wf-tabs", children=[

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
                                    id="date-picker", date=datetime.now().date(),
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

                # ── Upload + Calendar selector ───────────────────────────────
                dbc.Col(width=6, children=[
                    html.Div(style={
                        "background":"white","borderRadius":"12px",
                        "padding":"20px","boxShadow":"0 1px 4px rgba(0,0,0,0.08)",
                        "marginBottom":"16px",
                    }, children=[
                        html.H6("📎 Upload File to Calendar",
                                style={"fontWeight":"700","color":"#1e293b","marginBottom":"8px"}),
                        html.P("Upload a PDF or image — AI will extract your events and add them to Google Calendar.",
                               style={"fontSize":"13px","color":"#64748b","marginBottom":"12px"}),

                        # Calendar selector
                        html.Div(className="d-flex align-items-center gap-2 mb-3 flex-wrap", children=[
                            html.Span("🗓️", style={"fontSize":"14px","color":"#64748b"}),
                            dcc.Dropdown(
                                id="planner-topic-dropdown",
                                options=[{"label":"📅 Primary Calendar","value":"primary"}],
                                placeholder="Select target calendar...",
                                clearable=True,
                                style={"flex":"1","fontSize":"13px","minWidth":"180px"},
                            ),
                        ]),

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
            dbc.Row(className="mt-3 g-3", children=[
                dbc.Col(width=5, children=[html.Div(id="weather-card", className="wf-card")]),
                dbc.Col(width=7, children=[dcc.Graph(id="weather-chart", config={"displayModeBar":False})]),
            ]),
        ]),

        # ── Tab 4: News ──────────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-news", label="📰 News", children=[
            dbc.Row(className="mt-3", children=[
                dbc.Col([
                    html.Div(id="news-list"),
                ]),
            ]),
        ]),

        # ── Tab 5: Settings ──────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-settings", label="⚙️ Settings", children=[
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
    Output("chat-window",  "children"),
    Output("chat-store",   "data"),
    Output("chat-input",   "value"),
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
        return no_update, no_update, no_update

    city    = city    or "Barcelona"
    topics  = topics  or ["Tech","Finance"]
    history = history or []

    bubbles = [_bubble_ai("Hey! I'm WakeFlow 👋 Ask me anything — "
                           "I'll check your calendar, weather, and news automatically.")]

    today_tag = datetime.now().strftime("%A %Y-%m-%d")
    enriched  = f"[TODAY: {today_tag}] [CITY: {city}] [TOPICS: {', '.join(topics)}] {user_text}"
    history.append({"role":"user","content":enriched})
    reply = chat_with_tools(history, city, topics)
    history.append({"role":"assistant","content":reply})

    for m in history:
        if m["role"] == "user":
            display = m["content"].split("] ")[-1] if "] " in m["content"] else m["content"]
            bubbles.append(_bubble_user(display))
        elif m["role"] == "assistant":
            bubbles.append(_bubble_ai(m["content"]))

    return bubbles, history, ""


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
    Input("city-input",     "value"),
)
def cb_weather(city):
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
    Input("topics-check",  "value"),
)
def cb_news(topics):
    topics   = topics or ["Tech"]
    articles = get_news(topics, 10)
    if not articles:
        return dbc.Alert("No news — add NEWS_API_KEY to your .env file", color="secondary"), topics
    cards = [dbc.Card(className="wf-news-card mb-2", children=dbc.CardBody([
        html.H6(a["title"], className="wf-news-title"),
        html.P((a["description"] or "")[:110] + ("…" if len(a.get("description",""))>110 else ""), className="wf-news-desc"),
        html.Div(className="d-flex align-items-center", children=[
            dbc.Badge(a["source"], color="secondary", className="me-2"),
            html.Span(a["time"], className="wf-news-time"),
            html.A("Read →", href=a["url"], target="_blank", className="ms-auto wf-news-link"),
        ]),
    ])) for a in articles]
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
    Input("planner-upload",          "contents"),
    State("planner-upload",          "filename"),
    State("planner-topic-dropdown",  "value"),
    prevent_initial_call=True,
)
def cb_process_upload(contents, filename, selected_topic):
    if not contents or not filename:
        return no_update, no_update

    try:
        _ctype, content_string = contents.split(",", 1)
        decoded = base64.b64decode(content_string)
    except Exception:
        return dbc.Alert("Could not read file.", color="danger"), []

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
                                      color="warning"), [])
        except Exception as e:
            return dbc.Alert(f"Error reading PDF: {e}", color="danger"), []
        if not text.strip():
            return dbc.Alert("Could not extract text from PDF — try uploading an image instead.", color="warning"), []
        events = extract_events_from_text(text)

    elif ext in ("png","jpg","jpeg"):
        mime   = f"image/{'jpeg' if ext == 'jpg' else ext}"
        events = extract_events_from_image(content_string, mime)
    else:
        return dbc.Alert("Please upload a PDF or image file.", color="warning"), []

    if not events:
        return dbc.Alert("No events found. Try a document with clear dates and times.", color="warning"), []

    # Render extracted event cards — editable/customizable
    _google_ok = os.path.exists(TOKEN_FILE)
    cards = []
    for i, ev in enumerate(events):
        cards.append(html.Div(className="wf-event-import-card", style={
            "background":"#f8fafc","borderRadius":"10px",
            "padding":"12px","marginBottom":"10px",
            "border":"1px solid #e2e8f0",
        }, children=[
            # Title
            html.Strong(ev.get("title","Untitled"),
                        style={"color":"#1e293b","fontSize":"14px","display":"block","marginBottom":"8px"}),

            # Destination toggle: Calendar or Task
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
                    type="text",
                    value=ev.get("date",""),
                    placeholder="YYYY-MM-DD",
                    debounce=True,
                    style={"width":"130px","fontSize":"12px","padding":"3px 6px",
                           "border":"1px solid #cbd5e1","borderRadius":"6px"},
                ),
                html.Span("🕐", style={"fontSize":"13px"}),
                dcc.Input(
                    id={"type":"event-start","index":i},
                    type="text",
                    value=ev.get("start_time",""),
                    placeholder="Start HH:MM",
                    debounce=True,
                    style={"width":"100px","fontSize":"12px","padding":"3px 6px",
                           "border":"1px solid #cbd5e1","borderRadius":"6px"},
                ),
                html.Span("–", style={"color":"#94a3b8"}),
                dcc.Input(
                    id={"type":"event-end","index":i},
                    type="text",
                    value=ev.get("end_time",""),
                    placeholder="End HH:MM",
                    debounce=True,
                    style={"width":"100px","fontSize":"12px","padding":"3px 6px",
                           "border":"1px solid #cbd5e1","borderRadius":"6px"},
                ),
                html.Span("(leave time blank = All day)",
                          style={"fontSize":"11px","color":"#94a3b8"}),
            ]),

            # Description
            html.P(ev.get("description",""),
                   style={"color":"#64748b","fontSize":"11px","margin":"6px 0 0"})
            if ev.get("description") else html.Div(),
        ]))

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
            dbc.Badge(f"🏷️ {selected_topic}", color="primary", className="ms-1") if selected_topic else html.Span(),
        ]),
        *cards,
        add_section,
    ]), events


@callback(
    Output("add-events-status",     "children"),
    Output("task-store",            "data", allow_duplicate=True),
    Input("add-all-events-btn",     "n_clicks"),
    State("extracted-events-store", "data"),
    State({"type":"event-dest",  "index": dash.ALL}, "value"),
    State({"type":"event-date",  "index": dash.ALL}, "value"),
    State({"type":"event-start", "index": dash.ALL}, "value"),
    State({"type":"event-end",   "index": dash.ALL}, "value"),
    State("task-store",             "data"),
    prevent_initial_call=True,
)
def cb_add_all_events(n, events, dests, dates, starts, ends, tasks):
    if not n or not events:
        return no_update, no_update

    tasks = tasks or []
    next_id = max((t["id"] for t in tasks), default=-1) + 1

    cal_success, cal_failed, task_added = 0, 0, 0

    for i, ev in enumerate(events):
        # Read edited values (fall back to original if list is shorter)
        dest       = dests[i]  if i < len(dests)  else "calendar"
        edit_date  = dates[i]  if i < len(dates)  else ev.get("date","")
        edit_start = starts[i] if i < len(starts) else ev.get("start_time","")
        edit_end   = ends[i]   if i < len(ends)   else ev.get("end_time","")

        # Build merged event dict with user edits
        merged = {**ev,
                  "date":       (edit_date  or "").strip() or ev.get("date",""),
                  "start_time": (edit_start or "").strip() or None,
                  "end_time":   (edit_end   or "").strip() or None}

        if dest == "task":
            # Add to task store
            due_str = merged.get("date") or None
            tasks.append({
                "id":       next_id,
                "text":     merged.get("title","Imported Task"),
                "category": "assignment",
                "done":     False,
                "due":      due_str,
            })
            next_id += 1
            task_added += 1
        else:
            ok, _ = create_calendar_event(merged)
            if ok:
                cal_success += 1
            else:
                cal_failed += 1

    # Build summary message
    parts = []
    if cal_success:
        parts.append(f"📅 {cal_success} event{'s' if cal_success>1 else ''} added to Google Calendar")
    if task_added:
        parts.append(f"✅ {task_added} task{'s' if task_added>1 else ''} added to My Tasks")
    if cal_failed:
        parts.append(f"❌ {cal_failed} calendar event{'s' if cal_failed>1 else ''} failed")

    if not parts:
        return dbc.Alert("Nothing was added.", color="warning"), tasks

    color = "danger" if (cal_failed and not cal_success and not task_added) else \
            "warning" if cal_failed else "success"
    msg = " · ".join(parts)
    if cal_success:
        msg += " — Refresh My Day tab to see them!"
    return dbc.Alert(f"🎉 {msg}", color=color), tasks
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    app.run(debug=False, host="0.0.0.0", port=port)