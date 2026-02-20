"""
Wakeflow - Your AI Morning Briefing
ESADE PDAI Prototype | Built with Streamlit
"""

import streamlit as st
import requests
from datetime import datetime, timedelta

# -- Page setup --
st.set_page_config(page_title="Wakeflow", page_icon="🌅", layout="centered")

# -- Styling --
st.markdown("""
<style>
    .stApp {
        background: linear-gradient(180deg, #87CEEB 0%, #FDB813 40%, #F97316 70%, #1a1a2e 100%);
    }
    [data-testid="stSidebar"] {
        background: rgba(30, 30, 50, 0.92);
    }
    [data-testid="stMetricValue"] {
        color: #f97316 !important;
    }
    .stProgress > div > div > div > div {
        background: linear-gradient(90deg, #f97316, #fbbf24, #87CEEB);
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


# ============================================================
# Helper Functions
# ============================================================

def call_llm(prompt):
    """Call LLM API. Returns None if no API key is available."""
    try:
        key = st.secrets.get("OPENAI_API_KEY", "")
        if key and key != "your-key-here":
            from openai import OpenAI
            client = OpenAI(api_key=key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a helpful morning briefing assistant. Be concise and friendly."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=500
            )
            return resp.choices[0].message.content
    except Exception:
        pass

    try:
        key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if key and key != "your-key-here":
            from anthropic import Anthropic
            client = Anthropic(api_key=key)
            msg = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            return msg.content[0].text
    except Exception:
        pass

    return None


@st.cache_data(ttl=600)
def get_weather(city):
    """Fetch weather from OpenWeatherMap. Returns mock data if API fails."""
    try:
        key = st.secrets.get("WEATHER_API_KEY", "")
        if key and key != "your-key-here":
            url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={key}&units=metric"
            data = requests.get(url, timeout=5).json()
            icons = {"Clear": "☀️", "Clouds": "☁️", "Rain": "🌧️", "Drizzle": "🌦️",
                     "Thunderstorm": "⛈️", "Snow": "❄️", "Mist": "🌫️"}
            condition = data["weather"][0]["main"]
            return {
                "city": city,
                "temp": round(data["main"]["temp"]),
                "feels_like": round(data["main"]["feels_like"]),
                "humidity": data["main"]["humidity"],
                "condition": condition,
                "icon": icons.get(condition, "🌤️"),
            }
    except Exception:
        pass

    # Mock data based on city (used when no API key is set)
    mock_cities = {
        "bangkok":    {"temp": 33, "feels_like": 37, "humidity": 75, "condition": "Sunny", "icon": "☀️"},
        "barcelona":  {"temp": 18, "feels_like": 16, "humidity": 60, "condition": "Clouds", "icon": "☁️"},
        "london":     {"temp": 10, "feels_like": 7,  "humidity": 80, "condition": "Rain", "icon": "🌧️"},
        "tokyo":      {"temp": 14, "feels_like": 12, "humidity": 55, "condition": "Clear", "icon": "☀️"},
        "new york":   {"temp": 5,  "feels_like": 1,  "humidity": 65, "condition": "Clouds", "icon": "☁️"},
        "singapore":  {"temp": 31, "feels_like": 35, "humidity": 80, "condition": "Thunderstorm", "icon": "⛈️"},
        "paris":      {"temp": 12, "feels_like": 9,  "humidity": 70, "condition": "Drizzle", "icon": "🌦️"},
        "sydney":     {"temp": 25, "feels_like": 26, "humidity": 50, "condition": "Clear", "icon": "☀️"},
    }
    data = mock_cities.get(city.lower(), {"temp": 22, "feels_like": 21, "humidity": 60, "condition": "Clear", "icon": "🌤️"})
    data["city"] = city
    return data


def get_mock_calendar(selected_date):
    """Return mock calendar events for a given date (simulates Google Calendar)."""
    day = selected_date.strftime("%A")

    # Different schedules for different days
    if day in ["Saturday", "Sunday"]:
        return [
            {"time": "09:00", "end": "10:00", "title": "Gym session", "type": "personal",
             "location": "Campus gym", "notes": "Leg day + cardio"},
            {"time": "11:00", "end": "13:00", "title": "Brunch with friends", "type": "personal",
             "location": "Cafe Latte", "notes": ""},
            {"time": "14:00", "end": "17:00", "title": "Study session — group project", "type": "deadline",
             "location": "Library", "notes": "Finish slides for presentation"},
            {"time": "19:00", "end": "20:30", "title": "Movie night", "type": "personal",
             "location": "Home", "notes": ""},
        ]
    else:
        return [
            {"time": "08:00", "end": "08:30", "title": "Morning standup", "type": "meeting",
             "location": "Zoom", "notes": "Weekly sync with team"},
            {"time": "09:00", "end": "10:30", "title": "PDAI Lecture — Prof. Jose", "type": "class",
             "location": "Room 2.01", "notes": "Bring laptop, Streamlit exercise due"},
            {"time": "11:00", "end": "12:00", "title": "Team project sync", "type": "meeting",
             "location": "Library", "notes": "Discuss data pipeline approach"},
            {"time": "12:30", "end": "13:30", "title": "Lunch break", "type": "personal",
             "location": "", "notes": ""},
            {"time": "14:00", "end": "15:30", "title": "AI II Lecture — Prof. De-Arteaga", "type": "class",
             "location": "Room 3.05", "notes": "Random Forest quiz next week"},
            {"time": "16:00", "end": "17:00", "title": "Gym session", "type": "personal",
             "location": "Campus gym", "notes": "Upper body workout"},
            {"time": "18:00", "end": "19:00", "title": "Cloud Platforms homework deadline", "type": "deadline",
             "location": "", "notes": "Submit via Moodle before 19:00"},
        ]


# ============================================================
# News Functions
# ============================================================

TOPIC_TO_QUERY = {
    "Tech": "technology",
    "Finance": "finance OR stock market",
    "World": "world news",
    "Business": "business",
    "Science": "science",
    "Sports": "sports",
}

TOPIC_EMOJIS = {
    "Tech": "🤖", "Finance": "📈", "World": "🌏",
    "Business": "💼", "Science": "🔬", "Sports": "⚽",
}

@st.cache_data(ttl=1800)  # Cache for 30 minutes
def get_news(selected_topics):
    """Fetch real news from NewsAPI. Falls back to mock data if API fails."""
    try:
        key = st.secrets.get("NEWS_API_KEY", "")
        if key and key != "your-key-here":
            query = " OR ".join([TOPIC_TO_QUERY.get(t, t) for t in selected_topics])
            url = (
                f"https://newsapi.org/v2/everything?"
                f"q={query}&language=en&sortBy=publishedAt&pageSize=10&apiKey={key}"
            )
            resp = requests.get(url, timeout=5)
            data = resp.json()

            if data.get("status") == "ok" and data.get("articles"):
                articles = []
                for a in data["articles"][:10]:
                    # Calculate time ago
                    published = datetime.strptime(a["publishedAt"][:19], "%Y-%m-%dT%H:%M:%S")
                    diff = datetime.now() - published
                    hours = int(diff.total_seconds() / 3600)
                    if hours < 1:
                        time_ago = f"{int(diff.total_seconds() / 60)}m ago"
                    elif hours < 24:
                        time_ago = f"{hours}h ago"
                    else:
                        time_ago = f"{int(hours / 24)}d ago"

                    articles.append({
                        "title": a["title"],
                        "description": a.get("description", ""),
                        "source": a["source"]["name"],
                        "time": time_ago,
                        "url": a.get("url", ""),
                    })
                return articles
    except Exception:
        pass

    # No API key or request failed
    return []


# ============================================================
# Session State
# ============================================================

for key in ["ai_news", "ai_planner"]:
    if key not in st.session_state:
        st.session_state[key] = None

if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []


# ============================================================
# Sidebar
# ============================================================

with st.sidebar:
    st.markdown("## ⚙️ Settings")
    city = st.text_input("🏙️ City", value="Bangkok")

    st.divider()
    st.markdown("### 📰 News Interests")
    topics = st.multiselect("Select topics",
        ["Tech", "Finance", "World", "Business", "Science", "Sports"],
        default=["Tech", "Finance"])

    st.divider()
    st.caption("🌅 Wakeflow v1.0 — ESADE PDAI")


# ============================================================
# Header
# ============================================================

hour = datetime.now().hour
if hour < 12:
    greeting = "Good Morning"
elif hour < 17:
    greeting = "Good Afternoon"
else:
    greeting = "Good Evening"

st.caption(datetime.now().strftime("%A, %B %d, %Y"))
st.title(f"{greeting} ✨")

weather = get_weather(city)


# ============================================================
# Tabs
# ============================================================

tab_planner, tab_weather, tab_news = st.tabs([
    "📅 My Day", "🌤️ Weather", "📰 News"
])


# -- Tab 1: Daily Planner --
with tab_planner:
    st.subheader("📅 Today's Schedule")

    # NEW WIDGET: st.date_input — pick a date to view schedule
    selected_date = st.date_input(
        "📆 Select date",
        value=datetime.now(),
        min_value=datetime.now() - timedelta(days=7),
        max_value=datetime.now() + timedelta(days=7)
    )

    events = get_mock_calendar(selected_date)
    type_icons = {"meeting": "🤝", "class": "📚", "personal": "🧘", "deadline": "⏰"}

    # Display events with expanders for details
    for e in events:
        icon = type_icons.get(e["type"], "📌")
        loc = f" · 📍 {e['location']}" if e["location"] else ""

        # NEW WIDGET: st.expander — click to see event details
        with st.expander(f"**{e['time']} - {e['end']}** {icon} {e['title']}{loc}"):
            col_a, col_b = st.columns(2)
            col_a.markdown(f"**Type:** {e['type'].capitalize()}")
            col_b.markdown(f"**Duration:** {e['time']} - {e['end']}")
            if e["location"]:
                st.markdown(f"📍 **Location:** {e['location']}")
            if e["notes"]:
                st.markdown(f"📝 **Notes:** {e['notes']}")

    st.divider()

    # AI Daily Planner
    st.subheader("🤖 AI Daily Planner")

    if st.button("✨ Plan My Day", key="btn_planner"):
        schedule_text = "\n".join([
            f"- {e['time']}-{e['end']}: {e['title']} ({e['type']})"
            for e in events
        ])

        prompt = f"""You are a smart daily planner assistant. Here is the user's schedule for {selected_date.strftime('%A, %B %d')}:

{schedule_text}

Weather: {weather['temp']}°C, {weather['condition']} in {weather['city']}.
Current time: {datetime.now().strftime('%H:%M')}.

Please provide:
1. A quick summary of the day (1-2 sentences)
2. Top 3 priorities to focus on
3. One time management tip based on the schedule
4. Any weather-related advice

Keep it short, practical, and friendly. Use emojis."""

        with st.spinner("Planning your day..."):
            result = call_llm(prompt)
            if result:
                st.session_state["ai_planner"] = result
            else:
                meetings = [e for e in events if e["type"] == "meeting"]
                classes = [e for e in events if e["type"] == "class"]
                deadlines = [e for e in events if e["type"] == "deadline"]

                st.session_state["ai_planner"] = f"""📋 **Your Day at a Glance — {selected_date.strftime('%A, %B %d')}**

You have **{len(events)} events** today — {len(classes)} class(es), {len(meetings)} meeting(s), and {len(deadlines)} deadline(s).

🎯 **Top Priorities:**
1. {"⏰ " + deadlines[0]["title"] + " — don't miss this!" if deadlines else "Stay on top of your meetings"}
2. {classes[0]["title"] if classes else "Review your notes"}
3. {classes[1]["title"] if len(classes) > 1 else "Take breaks between tasks"}

💡 **Tip:** {"You have back-to-back events in the morning. Grab breakfast early!" if len(events) > 4 else "Relaxed schedule today — great time for deep work!"}

🌤️ It's {weather['temp']}°C and {weather['condition'].lower()} in {weather['city']}. {"Stay hydrated!" if weather['temp'] >= 30 else "Enjoy the weather!"}

_Connect an API key for personalized AI planning!_"""

    if st.session_state["ai_planner"]:
        st.markdown(st.session_state["ai_planner"])

    st.divider()

    # NEW WIDGET: st.chat_input — ask AI about your day
    st.subheader("💬 Ask AI About Your Day")
    st.caption("Type a question about your schedule, e.g. 'When is my free time?' or 'What should I prepare for?'")

    # Show chat history
    for msg in st.session_state["chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_question = st.chat_input("Ask about your day...")

    if user_question:
        # Show user message
        st.session_state["chat_history"].append({"role": "user", "content": user_question})
        with st.chat_message("user"):
            st.markdown(user_question)

        # Build context for AI
        schedule_text = "\n".join([
            f"- {e['time']}-{e['end']}: {e['title']} ({e['type']}, {e['location']})"
            for e in events
        ])

        chat_prompt = f"""You are Wakeflow, a smart daily assistant. Answer the user's question based on their schedule.

Today's schedule for {selected_date.strftime('%A, %B %d')}:
{schedule_text}

Weather: {weather['temp']}°C, {weather['condition']} in {weather['city']}.
Current time: {datetime.now().strftime('%H:%M')}.

User's question: {user_question}

Give a helpful, concise answer. Use emojis."""

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                result = call_llm(chat_prompt)
                if result:
                    st.markdown(result)
                    st.session_state["chat_history"].append({"role": "assistant", "content": result})
                else:
                    # Simple fallback
                    fallback = f"Based on your schedule, you have {len(events)} events today. "
                    free_slots = []
                    for i in range(len(events) - 1):
                        gap_start = events[i]["end"]
                        gap_end = events[i + 1]["time"]
                        if gap_start != gap_end:
                            free_slots.append(f"{gap_start}-{gap_end}")
                    if free_slots:
                        fallback += f"Your free time slots are: {', '.join(free_slots)}. "
                    fallback += "Connect an API key for smarter answers! 🔑"
                    st.markdown(fallback)
                    st.session_state["chat_history"].append({"role": "assistant", "content": fallback})


# -- Tab 2: Weather --
with tab_weather:
    st.subheader(f"{weather['icon']} {weather['city']}")

    c1, c2, c3 = st.columns(3)
    c1.metric("🌡️ Temperature", f"{weather['temp']}°C")
    c2.metric("🤔 Feels Like", f"{weather['feels_like']}°C")
    c3.metric("💧 Humidity", f"{weather['humidity']}%")

    st.divider()

    if weather["temp"] >= 30:
        st.info("☀️ It's hot today! Stay hydrated and wear light clothing.")
    elif weather["temp"] >= 20:
        st.info("🌤️ Pleasant weather — great day to be outdoors!")
    else:
        st.info("🧥 It's cool today. Don't forget a jacket!")


# -- Tab 3: News --
with tab_news:
    st.subheader("📰 Morning Briefing")
    st.caption(f"Updated · {datetime.now().strftime('%I:%M %p')}")

    # Fetch real news based on selected topics
    if topics:
        articles = get_news(tuple(topics))  # tuple for caching

        if articles:
            for a in articles:
                with st.expander(f"📰 **{a['title']}**"):
                    if a["description"]:
                        st.markdown(a["description"])
                    st.caption(f"🗞️ {a['source']} · {a['time']}")
                    if a["url"]:
                        st.markdown(f"[Read full article →]({a['url']})")
        else:
            st.warning("⚠️ Could not load news. Check your NEWS_API_KEY in secrets.toml.")
    else:
        st.info("Select topics in the sidebar to see news.")

    st.divider()

    # AI News Briefing
    st.subheader("🤖 AI News Summary")
    st.caption("Let AI summarize today's top stories for you.")

    if st.button("🔄 Summarize Today's News", key="btn_news"):
        articles = get_news(tuple(topics)) if topics else []
        headlines = "\n".join([f"- {a['title']}" for a in articles[:5]])

        prompt = f"""Here are today's top news headlines:
{headlines}

User's interests: {', '.join(topics)}.
City: {city}.

Please provide:
1. A brief 2-3 sentence summary of the overall news today
2. Which story is most relevant for someone interested in {', '.join(topics)}
3. One key takeaway or trend you notice

Keep it concise and insightful."""

        with st.spinner("Summarizing..."):
            result = call_llm(prompt)
            if result:
                st.session_state["ai_news"] = result
            else:
                st.session_state["ai_news"] = f"""📋 **Today's Summary — {datetime.now().strftime('%B %d, %Y')}**

Here are the top {len(articles)} stories matching your interests in {', '.join(topics)}:

{"".join([f'**{i+1}.** {a["title"]} ({a["source"]}){chr(10)}{chr(10)}' for i, a in enumerate(articles[:5])])}
_Connect an API key for AI-powered news analysis!_"""

    if st.session_state["ai_news"]:
        st.markdown(st.session_state["ai_news"])