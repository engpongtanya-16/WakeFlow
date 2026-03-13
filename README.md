# 🌅 WakeFlow — AI Morning Briefing App

> Your personalized morning command center — calendar, weather, news, and AI assistant in one place.

WakeFlow is a Python Dash web application that consolidates everything you need to start your day. Built as part of the **PDAI (Prototyping & Deploying AI Applications)** course at ESADE Business School.

---

## ✨ Features

### 📅 My Day — Smart Daily Planner
- Connects to **Google Calendar** via OAuth 2.0
- Displays all events in an interactive **Gantt chart** with colors matching your Google Calendar
- Click any event to get **AI-generated preparation tips** (powered by GPT-4o-mini)
- **Google Maps integration** — see your event location on an embedded map and get directions instantly
- Navigate to any date with the date picker

### 💬 AI Assistant — Conversational Day Planner
- Chat with WakeFlow about your schedule, weather, or news
- Powered by **GPT-4o-mini with Tool Calling** — the AI automatically fetches live data before answering:
  - 📅 `get_calendar_events` — real events from Google Calendar
  - 🌤️ `get_weather` — live weather from OpenWeatherMap
  - 📰 `get_news` — top headlines from NewsAPI
- Understands relative dates ("this Sunday", "tomorrow", "next Monday")
- Maintains conversation history for multi-turn dialogue

### 🌤️ Weather — Live Forecast
- Real-time weather data from **OpenWeatherMap API**
- Shows temperature, feels like, humidity, and weather condition
- Smart outfit advice based on current conditions
- Visual bar chart for quick weather overview

### 📰 News — Personalized Headlines
- Live headlines from **NewsAPI**
- Filter by topics: Tech, Finance, World, Sports, and more
- Clean card layout with source and description

### 📧 Daily Email — Automated Morning Briefing
- Sends a beautiful HTML email every morning at your chosen time
- Email includes:
  - Today's Google Calendar schedule
  - Live weather for your city
  - Top 5 news headlines
- Powered by **APScheduler** for reliable cron-based scheduling
- Configure your recipient email and send time from the app

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|------------|
| Framework | Python Dash + Flask |
| AI / LLM | OpenAI GPT-4o-mini (tool calling) |
| Calendar | Google Calendar API (OAuth 2.0) |
| Weather | OpenWeatherMap API |
| News | NewsAPI |
| Email | Gmail SMTP + APScheduler |
| Charts | Plotly (Gantt, Bar) |
| Maps | Google Maps Embed API |
| Styling | Custom CSS, gradient background |

---