import json
import os
import re
import base64
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

import dash
import dash_bootstrap_components as dbc
import plotly.express as px
import plotly.graph_objects as go
from dash import Input, Output, State, callback, dcc, html, no_update, ctx, Patch

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

GMAIL_SENDER       = os.getenv("GMAIL_SENDER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

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


def _mock_schedule(date_str: str) -> list:
    day = datetime.fromisoformat(date_str).strftime("%A") if date_str else "Monday"
    if day in ("Saturday","Sunday"):
        return [
            {"time":"09:00","end":"10:00","title":"Gym session",        "type":"personal","location":"Campus gym","notes":"Leg day","meet_link":""},
            {"time":"11:00","end":"13:00","title":"Brunch with friends","type":"personal","location":"Cafe Latte","notes":"","meet_link":""},
            {"time":"14:00","end":"17:00","title":"Group project study","type":"deadline","location":"Library",   "notes":"Finish slides","meet_link":""},
        ]
    return [
        {"time":"08:00","end":"08:30","title":"Morning standup",           "type":"meeting","location":"Zoom",      "notes":"Weekly sync",   "meet_link":"https://zoom.us/j/123456789"},
        {"time":"09:00","end":"10:30","title":"PDAI Lecture — Prof. Jose","type":"class",  "location":"Room 2.01","notes":"Bring laptop",  "meet_link":""},
        {"time":"11:00","end":"12:00","title":"Team project sync",         "type":"meeting","location":"Library",   "notes":"Data pipeline","meet_link":"https://meet.google.com/abc-defg-hij"},
        {"time":"12:30","end":"13:30","title":"Lunch",                     "type":"personal","location":"",         "notes":"",             "meet_link":""},
        {"time":"14:00","end":"15:30","title":"AI II Lecture",             "type":"class",  "location":"Room 3.05","notes":"Quiz next week","meet_link":""},
        {"time":"16:00","end":"17:00","title":"Gym session",               "type":"personal","location":"Campus gym","notes":"Upper body",  "meet_link":""},
        {"time":"18:00","end":"19:00","title":"Cloud Platforms deadline",  "type":"deadline","location":"",         "notes":"Submit Moodle","meet_link":""},
    ]


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
            y=[calendar],
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

    # Tick ทุก 2 ชั่วโมง
    tick_vals  = list(range(0, 25, 2))
    tick_texts = [f"{h:02d}:00" for h in tick_vals]

    fig.update_layout(
        plot_bgcolor="rgba(255,255,255,0.35)", paper_bgcolor="rgba(0,0,0,0)",
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
            tickfont=dict(color="#1e293b"),
            fixedrange=True,
        ),
        height=330, clickmode="event+select", dragmode="pan",
    )
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL BRIEFING
# ─────────────────────────────────────────────────────────────────────────────

def _build_email_html(city: str, topics: list) -> str:
    w      = get_weather(city)
    events = get_calendar_events(datetime.now().strftime("%Y-%m-%d"))
    news   = get_news(topics, 5)
    today  = datetime.now().strftime("%A, %B %d, %Y")

    rows = "".join(
        f"<tr><td style='padding:4px 10px;color:#64748b'>{e['time']}</td>"
        f"<td style='padding:4px 10px;color:#1e293b;font-weight:600'>{e['title']}</td>"
        f"<td style='padding:4px 10px;color:#64748b'>{e.get('location','')}</td></tr>"
        for e in events
    )
    news_items = "".join(
        f"<li style='margin-bottom:8px'>"
        f"<a href='{a['url']}' style='color:#60a5fa;text-decoration:none'>{a['title']}</a>"
        f" <span style='color:#64748b;font-size:11px'>— {a['source']}</span></li>"
        for a in news
    ) or "<li style='color:#64748b'>No news available</li>"

    return f"""
<html><body style="background:#FAF1D6;color:#1e293b;
  font-family:'DM Sans',Arial,sans-serif;padding:36px;max-width:620px;margin:0 auto">
  <h1 style="color:#FC9F66;margin-bottom:2px">WakeFlow</h1>
  <p style="color:#64748b;margin-top:0;font-size:13px">{today} · Your Morning Briefing</p>
  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#e07b30;margin-top:0;font-size:15px">{w.get('icon','?')} Weather — {w.get('city',city)}</h2>
    <span style="font-size:2.6rem;font-weight:700;color:#FC9F66">{w.get('temp','--')}°C</span>
    <span style="color:#64748b;margin-left:12px">{w.get('condition','--')} · Feels {w.get('feels_like','--')}°C · {w.get('humidity','--')}%</span>
  </div>
  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#0b8043;margin-top:0;font-size:15px">Today's Schedule</h2>
    <table style="width:100%;border-collapse:collapse">{rows}</table>
  </div>
  <div style="background:#fff8e8;border-radius:12px;padding:20px;margin:16px 0;border:1px solid #FAC357">
    <h2 style="color:#1d6fad;margin-top:0;font-size:15px">Top Stories</h2>
    <ul style="padding-left:16px;margin:0">{news_items}</ul>
  </div>
</body></html>"""


def send_email(recipient: str, city: str, topics: list,
               gmail_user: str = "", gmail_password: str = "") -> tuple[bool, str]:
    gmail_user     = gmail_user     or GMAIL_SENDER
    gmail_password = gmail_password or GMAIL_APP_PASSWORD
    if not gmail_user or not gmail_password:
        return False, "Missing Gmail sender credentials."
    try:
        html_content = _build_email_html(city, topics)
        html_content = (html_content
            .replace("\xa0"," ").replace("\u200b","")
            .replace("\u2019","'").replace("\u2018","'")
            .replace("\u201c",'"').replace("\u201d",'"')
        )
        html_content = html_content.encode("utf-8", errors="replace").decode("utf-8")

        msg            = MIMEMultipart("alternative")
        msg["Subject"] = f"WakeFlow · {datetime.now().strftime('%A, %B %d')}"
        msg["From"]    = gmail_user
        msg["To"]      = recipient
        msg.attach(MIMEText(html_content, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(gmail_user, gmail_password)
            s.send_message(msg)
        return True, f"Briefing sent to {recipient}!"
    except smtplib.SMTPAuthenticationError:
        return False, "Gmail authentication failed. Use an App Password."
    except Exception as e:
        return False, f"{str(e).replace(chr(10),' ')}"


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
    dcc.Store(id="pending-upload",         data=None),

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
            html.A(
                dbc.Badge(
                    "✅ Calendar Connected" if google_connected else "🔌 Connect Google Calendar",
                    color="success" if google_connected else "secondary",
                    className="wf-badge",
                ),
                href="/connect-google", target="_blank",
            ),
        ]),
    ]),

    # Tabs
    dbc.Tabs(id="main-tabs", active_tab="tab-day", className="wf-tabs", children=[

        # ── Tab 1: My Day ────────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-day", label="📅 My Day", children=[
            dbc.Row(className="mt-3 g-3", children=[
                dbc.Col(width=8, children=[
                    html.Div(className="d-flex align-items-center gap-3 mb-3", children=[
                        dcc.DatePickerSingle(
                            id="date-picker", date=datetime.now().date(),
                            display_format="D MMM YYYY", className="wf-datepicker",
                        ),
                        html.Div(id="selected-date-label",
                                 style={"fontWeight":"600","fontSize":"15px","color":"#1e293b"}),
                    ]),
                    dcc.Loading(type="circle", color="#f97316",
                                children=dcc.Graph(id="gantt-chart", config={"displayModeBar":False,"scrollZoom":False,"doubleClick":False})),
                    html.P("👆 Click any event bar to get AI tips + see location on map",
                           className="wf-hint mt-1"),
                ]),
                dbc.Col(width=4, children=[
                    html.Div(id="event-panel", className="wf-card", style={"minHeight":"300px"}, children=[
                        html.Div("🤖", style={"fontSize":"2rem","marginBottom":"8px"}),
                        html.P("Click an event on the chart →", style={"color":"#475569","fontSize":"13px"}),
                    ]),
                ]),
            ]),
        ]),

        # ── Tab 2: AI Assistant ──────────────────────────────────────────────
        dbc.Tab(tab_id="tab-chat", label="💬 AI Assistant", children=[
            dbc.Row(className="mt-3 g-3", children=[
                dbc.Col(width=10, children=[
                    html.Div(id="chat-window", children=[
                        _bubble_ai("Hey! I'm WakeFlow 👋 Ask me anything — "
                                   "I'll check your calendar, weather, and news automatically."),
                    ]),
                    dbc.InputGroup(className="mt-2", children=[
                        dcc.Upload(
                            id="upload-doc",
                            children=dbc.Button(
                                "📎", color="light", size="sm",
                                title="Upload PDF or image to extract events",
                                style={
                                    "height":"38px", "width":"38px", "padding":"0",
                                    "fontSize":"16px", "border":"1px solid #dee2e6",
                                    "borderRadius":"8px 0 0 8px",
                                    "display":"flex", "alignItems":"center",
                                    "justifyContent":"center",
                                },
                            ),
                            accept=".pdf,.png,.jpg,.jpeg",
                            multiple=False,
                        ),
                        dbc.Input(id="chat-input", placeholder="Ask about your day, or upload a file to import events...",
                                  type="text", className="wf-input", debounce=False),
                        dbc.Button("Send ↑", id="send-btn", color="warning",
                                   n_clicks=0, className="wf-send-btn"),
                    ]),
                    html.Div(id="upload-preview", className="mt-1"),

                    # AI Response Voting row
                    html.Div(className="d-flex align-items-center gap-2 mt-2", children=[
                        html.Small("Rate last response:",
                                   style={"color":"#64748b","fontSize":"12px","fontWeight":"500"}),
                        dbc.Button("👍", id="vote-up-btn", size="sm",
                                   color="outline-success", n_clicks=0, className="vote-btn"),
                        dbc.Button("👎", id="vote-down-btn", size="sm",
                                   color="outline-danger", n_clicks=0, className="vote-btn"),
                        html.Div(id="vote-status",
                                 style={"fontSize":"12px","color":"#34d399","fontWeight":"500"}),
                        html.Div(id="vote-stats", className="ms-auto",
                                 style={"fontSize":"11px","color":"#94a3b8"},
                                 children=_init_stats),
                    ]),
                ]),
            ]),
        ]),

        # ── Tab 3: Weather ───────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-weather", label="🌤️ Weather", children=[
            dbc.Row(className="mt-3 g-3", children=[
                dbc.Col(width=12, className="mb-2", children=[
                    html.Div(className="d-flex align-items-center gap-2", children=[
                        html.Label("📍 City:", style={"fontWeight":"600","color":"#475569","fontSize":"14px","marginBottom":"0"}),
                        dbc.Input(id="city-input", value="Barcelona", placeholder="Enter city name...",
                                  debounce=True, size="sm", style={"maxWidth":"220px"}, className="wf-input"),
                    ]),
                ]),
                dbc.Col(width=5, children=[html.Div(id="weather-card", className="wf-card")]),
                dbc.Col(width=7, children=[dcc.Graph(id="weather-chart", config={"displayModeBar":False})]),
            ]),
        ]),

        # ── Tab 4: News ──────────────────────────────────────────────────────
        dbc.Tab(tab_id="tab-news", label="📰 News", children=[
            dbc.Row(className="mt-3", children=[
                dbc.Col([
                    dbc.Checklist(
                        id="topics-check",
                        options=[{"label":f" {e}  {t}","value":t}
                                 for t,e in [("Tech","🤖"),("Finance","📈"),("World","🌏"),
                                             ("Business","💼"),("Science","🔬"),("Sports","⚽")]],
                        value=["Tech","Finance"], inline=True, className="wf-checklist mb-3",
                    ),
                    html.Div(id="news-list"),
                ]),
            ]),
        ]),

        # ── Tab 5: Daily Email ───────────────────────────────────────────────
        dbc.Tab(tab_id="tab-email", label="📧 Daily Email", children=[
            dbc.Row(className="mt-3 g-3", children=[
                dbc.Col(width=6, children=[
                    html.H5("Morning Briefing Email", className="wf-card-title mb-1"),
                    html.P("Get a daily morning briefing with your calendar, weather, and top news "
                           "delivered straight to your inbox.",
                           style={"color":"#374151","fontSize":"13px","marginBottom":"20px"}),
                    dbc.Input(id="gmail-user-input", type="hidden", value=""),
                    dbc.Input(id="gmail-pass-input", type="hidden", value=""),
                    dbc.Label("💌 Email address for morning brief", className="wf-label"),
                    dbc.Input(id="email-input", type="email", placeholder="your@email.com",
                              className="wf-input mb-3"),
                    dbc.Label("⏰ Send daily briefing at", className="wf-label"),
                    dcc.Dropdown(
                        id="email-time-dropdown",
                        options=[{"label": f"{h:02d}:00", "value": h} for h in range(5, 12)],
                        value=7, clearable=False, className="wf-dropdown mb-3",
                        style={"maxWidth":"160px"},
                    ),
                    html.P(id="email-time-display",
                           style={"color":"#374151","fontSize":"12px","marginTop":"-8px","marginBottom":"12px"}),
                    dbc.Row(className="g-2", children=[
                        dbc.Col(width=6, children=[
                            dbc.Button("📨 Send Now", id="send-email-btn",
                                       color="warning", n_clicks=0, size="sm", className="w-100"),
                        ]),
                        dbc.Col(width=6, children=[
                            dbc.Button("⏰ Save Schedule", id="save-schedule-btn",
                                       color="success", n_clicks=0, size="sm", className="w-100"),
                        ]),
                    ]),
                    html.Div(id="email-status",   className="mt-3"),
                    html.Div(id="schedule-status", className="mt-2"),
                ]),
                dbc.Col(width=6, children=[
                    html.Div(className="wf-card", children=[
                        html.H6("📋 What's in the daily email?", className="wf-card-title"),
                        *[html.Div(className="wf-tool-row", children=[
                            html.Span(icon+" ", style={"marginRight":"8px"}),
                            html.Span(label, style={"color":"#374151","fontSize":"13px"}),
                        ]) for icon, label in [
                            ("📅","Today's schedule from Google Calendar"),
                            ("🌤️","Live weather for your city"),
                            ("📰","Top 5 news headlines"),
                        ]],
                        html.Hr(className="wf-divider"),
                        html.Div(id="schedule-info", style={"color":"#374151","fontSize":"12px"}),
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


@callback(Output("email-time-display","children"), Input("email-time-dropdown","value"))
def cb_time_display(hour):
    return f"📌 Selected: {hour:02d}:00" if hour is not None else ""


@callback(Output("selected-date-label","children"), Input("date-picker","date"))
def cb_date_label(selected_date):
    if not selected_date:
        return ""
    try:
        return datetime.fromisoformat(str(selected_date)).strftime("%A, %d %B %Y")
    except Exception:
        return selected_date


@callback(
    Output("gantt-chart",  "figure"),
    Output("events-store", "data"),
    Input("date-picker",   "date"),
)
def cb_update_gantt(selected_date):
    date_str = str(selected_date) if selected_date else datetime.now().strftime("%Y-%m-%d")
    events   = get_calendar_events(date_str)
    return build_gantt(events, date_str), events


@callback(
    Output("event-panel",    "children"),
    Output("location-store", "data"),
    Input("gantt-chart",     "clickData"),
    State("events-store",    "data"),
    State("city-store",      "data"),
    prevent_initial_call=True,
)
def cb_click_event(click_data, events, city):
    if not click_data or not events:
        return no_update, no_update
    try:
        title = click_data["points"][0]["customdata"][0]
    except (KeyError, IndexError):
        return no_update, no_update

    event = next((e for e in events if e["title"] == title), None)
    if not event:
        return no_update, no_update

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
    ]), loc_data


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
    Output("pending-upload", "data"),
    Output("upload-preview", "children"),
    Input("upload-doc",      "contents"),
    State("upload-doc",      "filename"),
    prevent_initial_call=True,
)
def cb_store_upload(contents, filename):
    if not contents or not filename:
        return None, None

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext in ("png", "jpg", "jpeg"):
        preview = html.Div(
            className="d-flex align-items-center gap-2 p-2",
            style={"background":"rgba(255,255,255,0.5)","borderRadius":"12px",
                   "border":"1px solid rgba(255,255,255,0.7)","maxWidth":"320px"},
            children=[
                html.Img(src=contents,
                         style={"height":"56px","width":"56px","objectFit":"cover",
                                "borderRadius":"8px"}),
                html.Div([
                    html.Div(filename, style={"fontSize":"12px","fontWeight":"600","color":"#1e293b"}),
                    html.Div("Image ready — type a message or just press Send",
                             style={"fontSize":"11px","color":"#64748b"}),
                ]),
                html.Span("✕", id="clear-upload", style={"marginLeft":"auto","cursor":"pointer",
                           "color":"#94a3b8","fontSize":"14px","padding":"0 4px"}),
            ],
        )
    else:
        preview = html.Div(
            className="d-flex align-items-center gap-2 p-2",
            style={"background":"rgba(255,255,255,0.5)","borderRadius":"12px",
                   "border":"1px solid rgba(255,255,255,0.7)","maxWidth":"320px"},
            children=[
                html.Span("📄", style={"fontSize":"2rem"}),
                html.Div([
                    html.Div(filename, style={"fontSize":"12px","fontWeight":"600","color":"#1e293b"}),
                    html.Div("PDF ready — type a message or just press Send",
                             style={"fontSize":"11px","color":"#64748b"}),
                ]),
                html.Span("✕", id="clear-upload", style={"marginLeft":"auto","cursor":"pointer",
                           "color":"#94a3b8","fontSize":"14px","padding":"0 4px"}),
            ],
        )

    return {"contents": contents, "filename": filename}, preview


@callback(
    Output("chat-window",            "children"),
    Output("chat-store",             "data"),
    Output("chat-input",             "value"),
    Output("pending-upload",         "data",     allow_duplicate=True),
    Output("upload-preview",         "children", allow_duplicate=True),
    Output("extracted-events-store", "data",     allow_duplicate=True),
    Input("send-btn",        "n_clicks"),
    Input("chat-input",      "n_submit"),
    State("chat-input",      "value"),
    State("chat-store",      "data"),
    State("city-store",      "data"),
    State("topics-check",    "value"),
    State("pending-upload",  "data"),
    prevent_initial_call=True,
)
def cb_chat(n_clicks, n_submit, user_text, history, city, topics, pending):
    user_text = user_text or ""
    if not user_text.strip() and not pending:
        return no_update, no_update, no_update, no_update, no_update, no_update

    city    = city    or "Barcelona"
    topics  = topics  or ["Tech", "Finance"]
    history = history or []

    bubbles = [_bubble_ai("Hey! I'm WakeFlow 👋 Ask me anything — "
                           "I'll check your calendar, weather, and news automatically.")]

    if pending:
        fname    = pending["filename"]
        contents = pending["contents"]
        ext      = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""

        if ext in ("png", "jpg", "jpeg"):
            user_bubble_content = html.Div([
                html.Img(src=contents,
                         style={"maxWidth":"220px","maxHeight":"160px","borderRadius":"10px",
                                "display":"block","marginBottom":"4px" if user_text.strip() else "0"}),
                html.Div(user_text.strip(), style={"fontSize":"13px"}) if user_text.strip() else None,
            ])
        else:
            user_bubble_content = html.Div([
                html.Div(className="d-flex align-items-center gap-2", children=[
                    html.Span("📄", style={"fontSize":"1.4rem"}),
                    html.Span(fname, style={"fontSize":"12px","fontWeight":"600"}),
                ]),
                html.Div(user_text.strip(), style={"fontSize":"13px","marginTop":"4px"})
                    if user_text.strip() else None,
            ])

        user_bubble = html.Div(
            className="wf-bubble wf-bubble-user mb-2",
            style={"alignSelf":"flex-end"},
            children=user_bubble_content,
        )

        events = []
        if ext == "pdf":
            try:
                import io
                decoded = base64.b64decode(contents.split(",", 1)[1])
                try:
                    import pdfplumber
                    with pdfplumber.open(io.BytesIO(decoded)) as pdf:
                        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                except ImportError:
                    import PyPDF2
                    reader = PyPDF2.PdfReader(io.BytesIO(decoded))
                    text = "\n".join(p.extract_text() or "" for p in reader.pages)
                if text.strip():
                    events = extract_events_from_text(text)
            except Exception:
                pass
        elif ext in ("png", "jpg", "jpeg"):
            mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
            b64  = contents.split(",", 1)[1]
            events = extract_events_from_image(b64, mime)

        if events:
            event_lines = "\n".join(
                f"- **{ev.get('title','?')}** — {ev.get('date','')} "
                f"{ev.get('start_time','') or 'All day'}"
                f"{(' @ ' + ev['location']) if ev.get('location') else ''}"
                for ev in events
            )
            ai_msg = (
                f"📎 I found **{len(events)} event{'s' if len(events)>1 else ''}** "
                f"in `{fname}`:\n\n{event_lines}\n\n"
                "Click **➕ Add to Calendar** to add them all!"
            )
            _google_ok = os.path.exists(TOKEN_FILE)
            add_section = html.Div(className="mt-2", children=[
                dbc.Button(
                    f"➕ Add {len(events)} Event{'s' if len(events)>1 else ''} to Google Calendar",
                    id="add-all-events-btn", color="success", size="sm", n_clicks=0,
                    disabled=not _google_ok,
                ),
                html.Div(id="add-events-status", className="mt-1", style={"fontSize":"12px"}),
            ])
            ai_bubble = html.Div(
                className="wf-bubble wf-bubble-ai mb-2",
                style={"alignSelf":"flex-start","maxWidth":"90%"},
                children=[
                    html.Div([
                        html.Span("🤖 ", style={"fontSize":"16px"}),
                        html.Span("WakeFlow", style={"fontSize":"11px","color":"#475569","fontWeight":"600"}),
                    ], style={"marginBottom":"4px"}),
                    dcc.Markdown(ai_msg, style={"margin":"0","fontSize":"13px","color":"#1e293b"}),
                    add_section,
                ],
            )
        else:
            ai_bubble = _bubble_ai(
                f"📎 I read **{fname}** but couldn't find events with clear dates and times.\n\n"
                "Try uploading a file with specific dates like 'Monday April 21, 14:00'."
            )

        for m in history:
            if m["role"] == "user":
                display = m["content"].split("] ")[-1] if "] " in m["content"] else m["content"]
                bubbles.append(_bubble_user(display))
            elif m["role"] == "assistant":
                bubbles.append(_bubble_ai(m["content"]))
        bubbles.append(user_bubble)
        bubbles.append(ai_bubble)

        return bubbles, history, "", None, None, events if events else []

    # ── normal chat (no file) ─────────────────────────────────────────────────
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

    return bubbles, history, "", None, None, no_update


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
    Output("gantt-chart", "figure", allow_duplicate=True),
    Input("gantt-chart",  "relayoutData"),
    prevent_initial_call=True,
)
def cb_fix_gantt_range(relayout):
    if not relayout:
        return no_update
    x0 = relayout.get("xaxis.range[0]")
    x1 = relayout.get("xaxis.range[1]")
    if x0 is None or x1 is None:
        return no_update
    x0, x1 = float(x0), float(x1)
    if abs((x1 - x0) - 12) < 0.05:
        return no_update
    # บังคับให้กว้าง 12 ชั่วโมงเสมอ
    if x0 + 12 > 24:
        x0, x1 = 12.0, 24.0
    else:
        x1 = x0 + 12
    p = Patch()
    p["layout"]["xaxis"]["range"] = [x0, x1]
    return p


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
    State("email-input",       "value"),
    State("gmail-user-input",  "value"),
    State("gmail-pass-input",  "value"),
    State("city-store",        "data"),
    State("topics-check",      "value"),
    prevent_initial_call=True,
)
def cb_send_email(n, recipient, gmail_user, gmail_pass, city, topics):
    if not recipient:
        return dbc.Alert("Please enter a recipient email address.", color="warning")
    sender   = gmail_user or GMAIL_SENDER
    password = gmail_pass or GMAIL_APP_PASSWORD
    if not sender or not password:
        return dbc.Alert("Email sender not configured. Add GMAIL_SENDER and GMAIL_APP_PASSWORD to .env", color="danger")
    ok, msg = send_email(recipient, city or "Barcelona", topics or ["Tech","Finance"],
                         gmail_user=sender, gmail_password=password)
    return dbc.Alert(msg, color="success" if ok else "danger", dismissable=True)


@callback(
    Output("schedule-status", "children"),
    Output("schedule-info",   "children"),
    Input("save-schedule-btn",   "n_clicks"),
    State("email-input",         "value"),
    State("gmail-user-input",    "value"),
    State("gmail-pass-input",    "value"),
    State("email-time-dropdown", "value"),
    State("city-store",          "data"),
    State("topics-check",        "value"),
    prevent_initial_call=True,
)
def cb_save_schedule(n, recipient, gmail_user, gmail_pass, hour, city, topics):
    sender   = gmail_user or GMAIL_SENDER
    password = gmail_pass or GMAIL_APP_PASSWORD
    if not recipient:
        return dbc.Alert("Please enter a recipient email address.", color="warning"), no_update
    if not sender or not password:
        return dbc.Alert("Email sender not configured.", color="danger"), no_update

    if _scheduled_job["job"]:
        try:
            _scheduled_job["job"].remove()
        except Exception:
            pass

    city   = city   or "Barcelona"
    topics = topics or ["Tech","Finance"]

    def _send():
        send_email(recipient, city, topics, gmail_user=sender, gmail_password=password)

    job = scheduler.add_job(_send, trigger="cron", hour=hour, minute=0,
                            id="daily_briefing", replace_existing=True)
    _scheduled_job["job"] = job

    info = html.Div([
        html.Span("⏰ Scheduled: ", style={"color":"#34d399","fontWeight":"600"}),
        html.Span(f"Daily at {hour:02d}:00", style={"color":"#e2e8f0"}), html.Br(),
        html.Span("📬 To: ", style={"color":"#475569"}),
        html.Span(recipient, style={"color":"#e2e8f0"}),
    ])
    return (dbc.Alert(f"✅ Schedule saved! Briefing will send daily at {hour:02d}:00.",
                      color="success", dismissable=True), info)


# ── Import Events Callbacks ───────────────────────────────────────────────────
@callback(
    Output("import-result",          "children"),
    Output("extracted-events-store", "data"),
    Input("upload-doc",              "contents"),
    State("upload-doc",              "filename"),
    prevent_initial_call=True,
)
def cb_process_upload(contents, filename):
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

    # Render extracted event cards
    _google_ok = os.path.exists(TOKEN_FILE)
    cards = []
    for ev in events:
        time_label = (f"{ev.get('start_time','')} – {ev.get('end_time','')}"
                      if ev.get("start_time") else "All day")
        cards.append(html.Div(className="wf-event-import-card", children=[
            dbc.Row([
                dbc.Col(width=12, children=[
                    html.Strong(ev.get("title","Untitled"),
                                style={"color":"#1e293b","fontSize":"14px"}),
                    html.Div(className="d-flex gap-2 mt-1 flex-wrap", children=[
                        dbc.Badge(ev.get("date",""), color="primary"),
                        dbc.Badge(time_label, color="secondary"),
                        dbc.Badge(ev["location"], color="info") if ev.get("location") else html.Div(),
                    ]),
                    html.P(ev.get("description",""),
                           style={"color":"#64748b","fontSize":"11px","margin":"4px 0 0"})
                    if ev.get("description") else html.Div(),
                ]),
            ]),
        ]))

    add_section = html.Div(className="mt-3", children=[
        dbc.Button(
            f"➕ Add All {len(events)} Event{'s' if len(events) > 1 else ''} to Google Calendar",
            id="add-all-events-btn", color="success", size="sm", n_clicks=0,
            disabled=not _google_ok,
        ),
        html.Div(id="add-events-status", className="mt-2"),
    ]) if _google_ok else dbc.Alert("🔌 Connect Google Calendar first to add events.", color="secondary", className="mt-2")

    return html.Div([
        dbc.Alert(f"✅ Found {len(events)} event{'s' if len(events)>1 else ''} in «{filename}»",
                  color="success", className="mb-3"),
        *cards,
        add_section,
    ]), events


@callback(
    Output("add-events-status",     "children"),
    Input("add-all-events-btn",     "n_clicks"),
    State("extracted-events-store", "data"),
    prevent_initial_call=True,
)
def cb_add_all_events(n, events):
    if not n or not events:
        return no_update
    success, failed = 0, 0
    for ev in events:
        ok, _ = create_calendar_event(ev)
        if ok:
            success += 1
        else:
            failed += 1

    if failed == 0:
        return dbc.Alert(
            f"🎉 Added {success} event{'s' if success > 1 else ''} to Google Calendar! "
            "Refresh the My Day tab to see them.",
            color="success",
        )
    elif success == 0:
        return dbc.Alert(
            "❌ Failed. If you see a permissions error, delete google_token.json and reconnect.",
            color="danger",
        )
    return dbc.Alert(f"⚠️ Added {success} event(s). {failed} failed.", color="warning")
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    app.run(debug=False, host="0.0.0.0", port=port)