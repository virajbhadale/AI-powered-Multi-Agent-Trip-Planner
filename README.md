# 🌍 AI Multi-Agent Trip Planner

An intelligent travel planning application built using **LangGraph, LangChain, Google Gemini, Streamlit, and multiple travel APIs**.

The system uses a network of AI agents to automatically research destinations, search flights and hotels, analyze weather, recommend activities, and generate personalized travel itineraries.

---

## ✨ Features

### 🔍 Destination Research Agent

* Collects destination information using Wikipedia and Tavily Search
* Provides travel insights, local information, language, currency, and key facts

### 🏛️ Places Discovery Agent

* Finds top tourist attractions
* Retrieves descriptions and travel information

### 🌤️ Weather Agent

* Fetches weather forecasts using OpenWeather API
* Provides travel-friendly weather recommendations

### ✈️ Flight Search Agent

* Searches real flight options using SerpAPI Google Flights
* Displays:

  * Airline
  * Duration
  * Stops
  * Departure & Arrival Airports
  * Flight Prices

### 🏨 Hotel Recommendation Agent

* Finds hotels using web search
* Extracts:

  * Hotel Name
  * Ratings
  * Amenities
  * Estimated Pricing

### 🎯 Activity Recommendation Agent

* Generates activities based on user interests such as:

  * Culture
  * Food
  * Adventure
  * Nature
  * Shopping
  * Nightlife

### 📝 AI Itinerary Generator

* Creates detailed day-by-day travel plans
* Includes:

  * Daily activities
  * Budget breakdown
  * Transportation guidance
  * Travel tips

### 📄 PDF Export

* Download itinerary as PDF
* Download itinerary as text file

### ✉️ Email Integration

* Send generated itinerary directly to email

### 💱 Currency Conversion

* Automatically converts travel costs from USD to INR

---

## 🛠️ Tech Stack

### Frontend

* Streamlit

### AI & Agent Framework

* LangGraph
* LangChain
* Google Gemini 2.5 Flash

### APIs & Services

* Tavily Search API
* OpenWeather API
* SerpAPI Google Flights
* Google Maps API

### Utilities

* Wikipedia API
* AirportsData
* ReportLab
* Markdown-PDF

---

## 🏗️ Multi-Agent Workflow

Research Agent
↓
Places Agent
↓
├── Flights Agent
├── Hotels Agent
└── Weather Agent
↓
Activities Agent
↓
Itinerary Agent
↓
PDF & Email Export

---

## 📂 Project Structure

```text
RP_Project/
│
├── .streamlit/
│   └── secrets.toml
│
├── agents_final_6.py
├── requirements.txt
├── README.md
│
└── generated_itineraries/
```

## 🔑 Required API Keys

Add the following keys in:

```text
.streamlit/secrets.toml
```

```toml
google_api_key="YOUR_GEMINI_API_KEY"

tavily_api_key="YOUR_TAVILY_API_KEY"

serpapi_api_key="YOUR_SERPAPI_KEY"

openweather_key="YOUR_OPENWEATHER_API_KEY"

gmail_user="YOUR_EMAIL"

gmail_app_password="YOUR_GMAIL_APP_PASSWORD"
```

---

## 🚀 Installation

### Clone Repository

```bash
git clone https://github.com/yourusername/AI-Travel-Agent.git
cd AI-Travel-Agent
```

### Create Virtual Environment

```bash
python -m venv venv
```

### Activate Environment

Windows:

```bash
venv\Scripts\activate
```

Linux/Mac:

```bash
source venv/bin/activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Run Application

```bash
streamlit run agents_final_6.py
```

---

## 🎯 Example Use Case

User Input:

* Departure City: Mumbai
* Destination: Tokyo
* Budget: $3000
* Travelers: 2
* Interests:

  * Culture
  * Food
  * Nature

Output:

* Flight Recommendations
* Hotel Suggestions
* Weather Forecast
* Tourist Attractions
* Activity Recommendations
* Detailed Day-wise Itinerary
* Downloadable PDF Travel Guide

---

## Future Enhancements

* Live Hotel Booking APIs
* Flight Price Tracking
* Interactive Maps
* Budget Optimization Agent
* Multi-City Trip Planning
* Voice-Based Travel Assistant

---

## Author

**Viraj Bhadale**

M.Sc. Data Science Student

AI | Machine Learning | Agentic AI | Generative AI
