# 🌅 Wakeflow — Your AI Morning Briefing

An AI-powered morning briefing app built with **Streamlit** for the ESADE PDAI course. Open it every morning to see your schedule, weather, and news — all in one place.

## ✨ Features

| Feature | Description | AI Component |
|---------|-------------|--------------|
| 📅 My Day | Daily schedule with expandable event details | ✅ AI analyzes schedule → priorities, tips & advice |
| 💬 Ask AI | Chat with AI about your day | ✅ AI answers questions about your schedule |
| 🌤️ Weather | Real-time weather data for any city | — (OpenWeatherMap API) |
| 📰 News | Live news feed filtered by your interests | ✅ AI summarizes today's top stories |

## 🚀 Quick Start

### 1. Clone the repository
git clone https://github.com/engpongtanya-16/WakeFlow.git
cd WakeFlow

### 2. Install dependencies
pip install -r requirements.txt

### 3. Add API keys
Create .streamlit/secrets.toml:
WEATHER_API_KEY = "your-openweathermap-key"
NEWS_API_KEY = "your-newsapi-key"
OPENAI_API_KEY = "your-key-here"  (optional — for AI features)

### 4. Run the app
streamlit run app.py

## 🛠️ Tech Stack

- **Frontend:** Streamlit
- **Weather API:** OpenWeatherMap (free tier)
- **News API:** NewsAPI.org (free tier)
- **AI/LLM:** OpenAI GPT-4o-mini / Anthropic Claude (optional)
- **Deployment:** Hugging Face Spaces

## 📦 Streamlit Concepts Used

- st.tabs() — Multi-tab layout (My Day, Weather, News)
- st.columns() — Grid layout for weather metrics
- st.sidebar — City selection and news topic preferences
- st.session_state — Persist AI responses and chat history
- @st.cache_data — Cache weather and news API calls
- st.metric() — Weather data display
- st.expander() — Expandable event details and news articles
- st.date_input() — Date picker for schedule
- st.chat_input() / st.chat_message() — Chat with AI assistant
- st.spinner() — Loading indicators
- config.toml — Custom sunrise theme

## 🔮 Future Improvements (v2)

- Google Calendar API integration (currently mock data)
- AI Outfit Advisor based on weather
- Habit Tracker with AI coaching
- Spotify integration for mood-based playlists

## 👤 Author

Eng — ESADE MIBA24 | PDAI Course Prototype