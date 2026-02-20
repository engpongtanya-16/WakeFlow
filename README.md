# 🌅 Morning Briefing — AI-Powered Daily Dashboard

An AI-powered morning briefing prototype built with **Streamlit** for the ESADE PDAI course.

## ✨ Features

| Feature | Description | AI Component |
|---------|-------------|--------------|
| 🌤️ Weather | Real-time weather data for any city | — |
| 👗 Outfit Advisor | Personalized outfit suggestions | ✅ LLM generates advice based on weather + style |
| 📰 News Briefing | Curated news by interest | ✅ LLM generates personalized briefing |
| ✅ Habit Tracker | Track daily habits with progress bars | ✅ LLM provides coaching feedback |

## 🚀 Quick Start

### 1. Clone the repository
```bash
git clone https://github.com/YOUR_USERNAME/morning-briefing.git
cd morning-briefing
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. (Optional) Add API keys
Create `.streamlit/secrets.toml`:
```toml
OPENAI_API_KEY = "sk-..."
WEATHER_API_KEY = "your-openweathermap-key"
```
> The app works without API keys using rule-based fallbacks!

### 4. Run the app
```bash
streamlit run app.py
```

## 🛠️ Tech Stack

- **Frontend:** Streamlit
- **AI/LLM:** OpenAI GPT-4o-mini / Anthropic Claude (optional)
- **Weather API:** OpenWeatherMap (optional)
- **Deployment:** Hugging Face Spaces

## 📦 Streamlit Concepts Used

- `st.tabs()` — Multi-tab layout
- `st.columns()` — Grid layout for metrics
- `st.sidebar` — User preferences panel
- `st.session_state` — Persist habit counters across re-runs
- `@st.cache_data` — Cache API calls to avoid redundant requests
- `st.progress()` — Visual progress bars
- `st.metric()` — Weather data display
- `st.spinner()` — Loading indicators for AI calls
- `config.toml` — Custom dark theme

## 👤 Author

Eng — ESADE MIBA24 | PDAI Course Prototype