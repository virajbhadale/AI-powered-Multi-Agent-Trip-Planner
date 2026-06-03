# travel_planner_app.py
import os
os.environ["STREAMLIT_WATCHER_TYPE"] = "none"

import streamlit as st
import time
import requests
import smtplib
import base64
from datetime import datetime, timedelta
from typing import TypedDict, List, Dict, Any, Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import json
from io import BytesIO
from markdown_pdf import MarkdownPdf, Section
import tempfile
import re
import airportsdata

# LangGraph and LangChain imports
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from operator import add

from langchain_google_genai import ChatGoogleGenerativeAI
from typing_extensions import Annotated
from langchain_community.utilities import WikipediaAPIWrapper
from tavily import TavilyClient
import googlemaps

# PDF generation (reportlab imports kept for compatibility)
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch

# ----------------------------
# Streamlit page config
# ----------------------------
st.set_page_config(
    page_title="AI Trip Planner",
    page_icon="✈️",
    layout="wide"
)

# ----------------------------
# Utilities
# ----------------------------
def retry_api_call(fn, attempts: int = 3, backoff: float = 0.8, exceptions=(Exception,)):
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn()
        except exceptions as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            else:
                raise last_exc

def safe_json_parse(text: str) -> Any:
    if not text:
        return {}
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return {"raw": cleaned[:2000], "note": "could not parse as JSON"}

def sanitize_text_for_pdf(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", "", text)
    lines = []
    for line in text.splitlines():
        lines.append(line if len(line) <= 1200 else line[:1200] + "...")
    return "\n".join(lines)

# ----------------------------
# NEW: USD -> INR rate (cached once)
# ----------------------------
@st.cache_resource
def get_usd_to_inr_rate() -> float:
    """Fetch live USD→INR exchange rate with fallback."""
    url = "https://api.exchangerate.host/latest?base=USD&symbols=INR"
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return float(r.json()["rates"]["INR"])
    except Exception:
        return 84.0  # fallback if API fails

# Fetch once at startup (cached)
USD_TO_INR = get_usd_to_inr_rate()

# ----------------------------
# Currency conversion helpers (kept older helper but primary conversion uses USD_TO_INR)
# ----------------------------
@st.cache_resource
def get_cached_usd_to_inr_rate() -> Optional[float]:
    """Legacy helper preserved but primary conversion will use USD_TO_INR above."""
    try:
        r = requests.get("https://api.exchangerate.host/latest", params={"base": "USD", "symbols": "INR"}, timeout=6)
        r.raise_for_status()
        data = r.json()
        rate = float(data.get("rates", {}).get("INR"))
        return rate
    except Exception:
        return None

def parse_numeric_amount(price_str: Any) -> Optional[float]:
    """Try to extract a numeric value from price string like '$1,234' or '1234'."""
    if price_str is None:
        return None
    if isinstance(price_str, (int, float)):
        return float(price_str)
    s = str(price_str)
    # if it's like "₹ 10,000" treat as INR already
    if "₹" in s or "INR" in s.upper():
        s_clean = re.sub(r"[^\d\.\-]", "", s)
        try:
            return float(s_clean)
        except Exception:
            return None
    # attempt to find numeric amount (USD or plain)
    m = re.search(r"\$?\s*([0-9\.,]+)", s)
    if m:
        num = m.group(1).replace(",", "")
        try:
            return float(num)
        except Exception:
            return None
    m2 = re.search(r"([0-9\.,]+)", s)
    if m2:
        try:
            return float(m2.group(1).replace(",", ""))
        except Exception:
            return None
    return None

def format_inr(amount: float) -> str:
    """Format float as Indian-rupee string with symbol and grouping, rounded to nearest rupee."""
    try:
        amt = int(round(amount))
        return "₹{:,.0f}".format(amt)
    except Exception:
        return f"₹{amount:.2f}"

def convert_price_to_inr(price_str: Any) -> Optional[str]:
    """
    Convert a price string (possibly USD like '$153.0') to INR using cached USD_TO_INR.
    Returns formatted INR string or original string if conversion not possible.
    """
    if price_str is None:
        return None
    s = str(price_str)
    # If price already contains rupee / INR, return formatted
    if "₹" in s or "INR" in s.upper():
        amt = parse_numeric_amount(s)
        return format_inr(amt) if amt is not None else s
    # parse numeric (USD or plain)
    num = parse_numeric_amount(s)
    if num is None:
        return s
    rate = USD_TO_INR  # use cached module-level rate
    try:
        inr = num * float(rate)
        return format_inr(inr)
    except Exception:
        # fallback: return USD but with note
        return f"${num} (INR conversion unavailable)"

# ----------------------------
# NEW: Replace USD mentions in itinerary text with INR
# ----------------------------
USD_PATTERN_DOLLAR = re.compile(r"\$\s?([0-9]{1,3}(?:[0-9,]*)(?:\.[0-9]+)?)")
USD_PATTERN_CODE = re.compile(r"\bUSD\s*([0-9]{1,3}(?:[0-9,]*)(?:\.[0-9]+)?)\b", re.IGNORECASE)

def replace_usd_with_inr(text: str) -> str:
    """Find USD mentions like $123 or USD 123 and replace with formatted INR using USD_TO_INR."""
    if not text:
        return text

    def _dollar_repl(m: re.Match) -> str:
        num_str = m.group(1).replace(",", "")
        try:
            usd_val = float(num_str)
            inr_val = usd_val * float(USD_TO_INR)
            return format_inr(inr_val)
        except Exception:
            return m.group(0)

    def _code_repl(m: re.Match) -> str:
        num_str = m.group(1).replace(",", "")
        try:
            usd_val = float(num_str)
            inr_val = usd_val * float(USD_TO_INR)
            return format_inr(inr_val)
        except Exception:
            return m.group(0)

    # First replace $123 patterns
    text = USD_PATTERN_DOLLAR.sub(_dollar_repl, text)
    # Then replace "USD 123" patterns
    text = USD_PATTERN_CODE.sub(_code_repl, text)
    return text

# ----------------------------
# API initializers
# ----------------------------
@st.cache_resource
def initialize_apis():
    """Initialize LLM and other API clients if keys present in secrets."""
    llm_client = None
    tavily_client = None
    wiki = None

    # Google Gemini (langchain_google_genai)
    g_key = st.secrets.get("google_api_key", "") or st.secrets.get("GOOGLE_API_KEY", "")
    try:
        if g_key:
            llm_client = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=g_key,
                temperature=0.65
            )
    except Exception:
        llm_client = None

    # Tavily
    try:
        t_key = st.secrets.get("tavily_api_key", "") or st.secrets.get("TAVILY_API_KEY", "")
        if t_key:
            tavily_client = TavilyClient(api_key=t_key)
    except Exception:
        tavily_client = None

    # Wikipedia wrapper
    try:
        wiki = WikipediaAPIWrapper()
    except Exception:
        wiki = None

    return llm_client, tavily_client, wiki

llm, tavily_client, wikipedia = initialize_apis()

# load airports data once
_IATA_AIRPORTS = airportsdata.load("IATA")

# ----------------------------
# Small helpers
# ----------------------------
def get_wikipedia_image(query: str, delay: float = 0.5) -> Optional[str]:
    """Fetch image URL from Tavily (best-effort)."""
    try:
        time.sleep(delay)
        url = "https://api.tavily.com/search"
        headers = {"Authorization": f"Bearer {st.secrets.get('tavily_api_key','') or st.secrets.get('TAVILY_API_KEY','')}", "Content-Type": "application/json"}
        payload = {"query": query, "include_images": True, "limit": 1}
        resp = requests.post(url, headers=headers, json=payload, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("images"):
            first = data["images"][0]
            if isinstance(first, dict) and first.get("url"):
                return first["url"]
    except Exception:
        pass
    return None

def find_iata_for_city(city_name: str) -> Optional[str]:
    """Resolve city name to IATA using airportsdata; fallback to Google Places if available."""
    if not city_name:
        return None
    cname = city_name.strip().lower()
    # exact city match
    for code, info in _IATA_AIRPORTS.items():
        try:
            city_field = (info.get("city") or "").strip().lower()
            if city_field == cname:
                return code
        except Exception:
            continue
    # substring match in name or city
    for code, info in _IATA_AIRPORTS.items():
        try:
            name = (info.get("name") or "").lower()
            city_field = (info.get("city") or "").lower()
            if cname in name or cname in city_field:
                return code
        except Exception:
            continue
    # Google Places fallback
    gkey = st.secrets.get("google_api_key", "") or st.secrets.get("GOOGLE_API_KEY", "")
    if gkey and googlemaps:
        try:
            client = googlemaps.Client(key=gkey)
            geocode_res = client.geocode(city_name)
            if geocode_res:
                loc = geocode_res[0]["geometry"]["location"]
                lat, lng = loc["lat"], loc["lng"]
                places = client.places_nearby(location=(lat, lng), radius=50000, type="airport")
                results = places.get("results", []) if isinstance(places, dict) else []
                if not results:
                    places = client.places_nearby(location=(lat, lng), radius=150000, type="airport")
                    results = places.get("results", []) if isinstance(places, dict) else []
                if results:
                    airport_name = results[0].get("name", "")
                    m = re.search(r"\(([A-Z]{3})\)", airport_name)
                    if m:
                        return m.group(1)
                    m2 = re.search(r"\b([A-Z]{3})\b", airport_name)
                    if m2:
                        return m2.group(1)
        except Exception:
            pass
    return None

# ----------------------------
# Type for shared state
# ----------------------------
class TripPlannerState(TypedDict):
    destination: str
    start_location: str
    start_date: str
    end_date: str
    budget: int
    num_travelers: int
    interests: List[str]

    destination_info: Dict[str, Any]
    destination_image: Optional[str]
    places_info: List[Dict[str, Any]]
    flight_options: List[Dict[str, Any]]
    hotel_options: List[Dict[str, Any]]
    activities: List[Dict[str, Any]]
    weather_info: Dict[str, Any]
    final_itinerary: str

    current_step: Annotated[List[str], add]
    error_messages: Annotated[List[str], add]
    progress: Annotated[int, add]

# ----------------------------
# Agents (research, places, weather, activities, hotels, flights, itinerary)
# ----------------------------
def research_agent(state: TripPlannerState) -> Dict[str, Any]:
    dest = state["destination"]
    st.write(f"🔍 Researching {dest}...")  # safe in agent (LangGraph call happens in main thread)
    res = {"destination_info": {}, "destination_image": None, "current_step": ["research_complete"], "progress": 5}
    wiki_text = ""
    try:
        if wikipedia:
            wiki_text = retry_api_call(lambda: wikipedia.run(f"{dest} travel tourism"), attempts=2, backoff=0.5)
    except Exception:
        wiki_text = ""
    dest_img = None
    try:
        dest_img = get_wikipedia_image(f"{dest} landmark")
    except Exception:
        dest_img = None

    prompt = f"""
    Extract a travel-friendly JSON summary for '{dest}' with keys:
    description (2-sentence), best_time_to_visit, currency, language, timezone, key_facts (list of 3).
    Use WIKI text: {wiki_text[:2000]}
    Return strict JSON.
    """
    if llm:
        try:
            content = retry_api_call(lambda: llm.invoke(prompt).content, attempts=2, backoff=0.5)
            parsed = safe_json_parse(content)
            if isinstance(parsed, dict):
                res["destination_info"] = parsed
            else:
                res["destination_info"] = {"description": (content or "")[:800]}
        except Exception:
            res["destination_info"] = {"description": (wiki_text or "")[:800], "note": "LLM parse failed"}
    else:
        res["destination_info"] = {"description": (wiki_text or "")[:800], "note": "LLM not configured"}

    res["destination_image"] = dest_img
    return res

def places_agent(state: TripPlannerState) -> Dict[str, Any]:
    dest = state["destination"]
    res = {"places_info": [], "current_step": ["places_complete"], "progress": 10}
    try:
        if tavily_client:
            tavres = retry_api_call(lambda: tavily_client.search(query=f"top tourist attractions in {dest}", max_results=6, search_depth="basic"), attempts=2, backoff=0.5)
            items = tavres.get("results", []) if isinstance(tavres, dict) else []
            for r in items[:6]:
                res["places_info"].append({
                    "name": r.get("title", ""),
                    "description": r.get("content", "")[:400],
                    "url": r.get("url", ""),
                    "rating": None
                })
        else:
            res["places_info"] = [{"name": f"{dest} top site {i+1}", "description": ""} for i in range(3)]
    except Exception:
        res["places_info"] = res.get("places_info", [])
    return res

def weather_agent(state: TripPlannerState) -> Dict[str, Any]:
    dest = state["destination"]
    res = {"weather_info": {}, "current_step": ["weather_complete"], "progress": 5}
    try:
        owm_key = st.secrets.get("openweather_key", "") or st.secrets.get("OPENWEATHER_KEY", "") or st.secrets.get("OPENWEATHER_API_KEY", "")
        if owm_key:
            geocode = retry_api_call(lambda: requests.get("http://api.openweathermap.org/geo/1.0/direct", params={"q": dest, "limit": 1, "appid": owm_key}, timeout=8).json(), attempts=2, backoff=0.5)
            if geocode:
                lat, lon = geocode[0]["lat"], geocode[0]["lon"]
                forecast = retry_api_call(lambda: requests.get("https://api.openweathermap.org/data/2.5/onecall", params={"lat": lat, "lon": lon, "exclude": "minutely,hourly", "units": "metric", "appid": owm_key}, timeout=10).json(), attempts=2, backoff=0.5)
                res["weather_info"] = {"current": forecast.get("current", {}), "daily": forecast.get("daily", [])[:7]}
            else:
                res["weather_info"] = {"forecast": "Geocode not returned by OpenWeather"}
        else:
            if tavily_client:
                tavres = retry_api_call(lambda: tavily_client.search(query=f"climate summary {dest}", max_results=1, search_depth="basic"), attempts=2, backoff=0.5)
                res["weather_info"] = {"forecast": tavres.get("results", [{}])[0].get("content", "") if isinstance(tavres, dict) else "No weather summary found."}
            else:
                res["weather_info"] = {"forecast": "Weather lookup not configured."}
    except Exception as e:
        res["weather_info"] = {"error": str(e)}
    return res

def activities_agent(state: TripPlannerState) -> Dict[str, Any]:
    dest = state["destination"]
    interests = state.get("interests", [])[:3]
    activities: List[Dict[str, Any]] = []
    try:
        if tavily_client and interests:
            for it in interests:
                tavres = retry_api_call(lambda q=it: tavily_client.search(query=f"{q} activities {dest}", max_results=2, search_depth="basic"), attempts=2, backoff=0.5)
                for r in (tavres.get("results", []) if isinstance(tavres, dict) else []):
                    activities.append({"title": r.get("title", ""), "description": r.get("content", "")[:250], "url": r.get("url", ""), "category": it})
        else:
            for i, it in enumerate(interests[:3]):
                activities.append({"title": f"{it} experience {i+1}", "description": "", "category": it})
    except Exception:
        activities = activities
    return {"activities": activities[:8], "current_step": ["activities_complete"], "progress": 10}

# ----------------------------
# Flight parsing helpers
# (unchanged)
# ----------------------------
def _extract_airline_from_legs(legs: List[Dict[str, Any]]) -> Optional[str]:
    if not legs:
        return None
    for seg in legs:
        for key in ("airline", "carrier", "operating_airline", "marketing_airline"):
            val = seg.get(key)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, dict):
                name = val.get("name") or val.get("id") or val.get("code")
                if name:
                    return name
        txt = json.dumps(seg)
        m = re.search(r"\"airline\"\s*:\s*\"([^\"]+)\"", txt)
        if m:
            return m.group(1)
    return None

def _parse_serp_flight_item(item: Dict[str, Any], dep_input: str, arr_input: str) -> Dict[str, Any]:
    out = {
        "airline": item.get("airline") or item.get("carrier") or None,
        "price": item.get("price", "N/A"),
        "duration": item.get("duration") or item.get("total_duration") or "N/A",
        "stops": item.get("stops", "N/A"),
        "departure_airport": dep_input,
        "arrival_airport": arr_input,
        "departure_time": None,
        "arrival_time": None,
        "booking_link": item.get("booking_link", "") or item.get("link", "") or item.get("deep_link", "") or ""
    }

    legs = None
    for key in ("flights", "segments", "legs", "itinerary", "trip"):
        if key in item and isinstance(item[key], list):
            legs = item[key]
            break
    if not legs:
        for key in ("data", "slices", "itineraries"):
            val = item.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                for k2 in ("segments", "legs", "flights"):
                    if k2 in val[0] and isinstance(val[0][k2], list):
                        legs = val[0][k2]
                        break
            if legs:
                break

    if not out["airline"]:
        candidate = None
        if legs:
            candidate = _extract_airline_from_legs(legs)
        out["airline"] = candidate or "Unknown"

    try:
        if legs and isinstance(legs, list) and legs:
            first = legs[0]
            last = legs[-1]
            for pat in ("departure_airport", "origin", "from", "dep_airport"):
                v = first.get(pat)
                if isinstance(v, dict):
                    out["departure_airport"] = v.get("name") or v.get("id") or out["departure_airport"]
                    out["departure_time"] = out["departure_time"] or v.get("time") or first.get("departure_time") or first.get("departure")
                elif isinstance(v, str):
                    out["departure_airport"] = v
            for pat in ("arrival_airport", "destination", "to", "arr_airport"):
                v = last.get(pat)
                if isinstance(v, dict):
                    out["arrival_airport"] = v.get("name") or v.get("id") or out["arrival_airport"]
                    out["arrival_time"] = out["arrival_time"] or v.get("time") or last.get("arrival_time") or last.get("arrival")
                elif isinstance(v, str):
                    out["arrival_airport"] = v
            out["departure_time"] = out["departure_time"] or first.get("departure_time") or first.get("dep_time") or first.get("time")
            out["arrival_time"] = out["arrival_time"] or last.get("arrival_time") or last.get("arr_time") or last.get("time")
            out["stops"] = len(legs) - 1
    except Exception:
        pass

    if out["price"] in (None, "N/A"):
        text = json.dumps(item)
        m = re.search(r"\$\s*([0-9,]+)", text)
        if m:
            out["price"] = m.group(1).replace(",", "")

    return out

def flights_agent(state: TripPlannerState) -> Dict[str, Any]:
    st.write("✈️ Searching flights...")
    serp_key = st.secrets.get("serpapi_api_key", "") or st.secrets.get("SERP_API_KEY", "") or st.secrets.get("SERPAPI_API_KEY", "") or st.secrets.get("SERPAPI_KEY", "")
    origin_city = state.get("start_location") or ""
    dest_city = state.get("destination") or ""
    dep_iata = state.get("departure_iata") or find_iata_for_city(origin_city)
    arr_iata = state.get("arrival_iata") or find_iata_for_city(dest_city)

    if not dep_iata:
        dep_iata = find_iata_for_city(origin_city)
    if not arr_iata:
        arr_iata = find_iata_for_city(dest_city)

    dep_input = dep_iata or origin_city
    arr_input = arr_iata or dest_city
    outbound = state.get("start_date")
    return_date = state.get("end_date")

    if not serp_key:
        return {"flight_options": [], "error_messages": ["Missing SERP API key in secrets."]}

    params = {
        "engine": "google_flights",
        "departure_id": dep_input,
        "arrival_id": arr_input,
        "outbound_date": outbound,
        "return_date": return_date,
        "currency": "USD",
        "hl": "en",
        "api_key": serp_key
    }

    try:
        def call_serp():
            r = requests.get("https://serpapi.com/search", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        data = retry_api_call(call_serp, attempts=2, backoff=0.5)

        candidates: List[Dict[str, Any]] = []
        for key in ("best_flights", "other_flights", "flights", "flight_results", "trips", "itineraries"):
            val = data.get(key)
            if isinstance(val, list):
                candidates.extend(val)
            elif isinstance(val, dict) and isinstance(val.get("results"), list):
                candidates.extend(val.get("results"))

        if not candidates:
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    sample = json.dumps(v[0])
                    if "price" in sample or "airline" in sample or "departure_airport" in sample:
                        candidates.extend(v)

        parsed_list = []
        seen = set()
        for item in candidates:
            key = json.dumps(item, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            parsed = _parse_serp_flight_item(item, dep_input, arr_input)
            parsed_list.append(parsed)
            if len(parsed_list) >= 12:
                break

        if not parsed_list:
            parsed_list = [{
                "airline": "No flights found",
                "price": "N/A",
                "duration": "N/A",
                "stops": "N/A",
                "departure_airport": dep_input,
                "arrival_airport": arr_input,
                "departure_time": "N/A",
                "arrival_time": "N/A",
                "booking_link": ""
            }]

        # Populate price_in_inr for each parsed flight using the cached USD_TO_INR
        for p in parsed_list:
            p["price_in_inr"] = convert_price_to_inr(p.get("price"))

        return {"flight_options": parsed_list, "current_step": ["flights_complete"], "progress": 10}
    except Exception as e:
        return {"flight_options": [], "error_messages": [f"Flight search error (SerpAPI): {str(e)}"]}

def hotels_agent(state: TripPlannerState) -> Dict[str, Any]:
    destination = state["destination"]
    budget = state["budget"]
    hotel_options: List[Dict[str, Any]] = []
    try:
        if tavily_client:
            tavres = retry_api_call(lambda: tavily_client.search(query=f"best hotels {destination} prices reviews", max_results=6, search_depth="basic"), attempts=2, backoff=0.5)
            results = tavres.get("results", []) if isinstance(tavres, dict) else []
            if llm:
                prompt = f"Extract up to 4 hotels for {destination} from these snippets:\n{json.dumps([r.get('title','') + ' - ' + r.get('content','')[:150] for r in results])}\nReturn JSON list of objects with name, price_per_night, rating, location, amenities, booking_platform."
                try:
                    content = retry_api_call(lambda: llm.invoke(prompt).content, attempts=2, backoff=0.5)
                    parsed = safe_json_parse(content)
                    if isinstance(parsed, list):
                        hotel_options = parsed
                    else:
                        for r in results[:4]:
                            hotel_options.append({"name": r.get("title", ""), "price_per_night": "See link", "rating": "N/A", "location": "", "amenities": "", "booking_platform": r.get("url", "")})
                except Exception:
                    for r in results[:4]:
                        hotel_options.append({"name": r.get("title", ""), "price_per_night": "See link", "rating": "N/A", "location": "", "amenities": "", "booking_platform": r.get("url", "")})
            else:
                for r in results[:3]:
                    hotel_options.append({"name": r.get("title", ""), "price_per_night": "$100", "rating": "4.0", "location": "Central", "amenities": "WiFi"})
        else:
            hotel_options = [{"name": f"{destination} Sample Hotel", "price_per_night": "$100", "rating": "4.0", "location": "Central", "amenities": "WiFi"}]
    except Exception:
        hotel_options = hotel_options

    # Convert hotel prices if possible
    normalized: List[Dict[str, Any]] = []
    for h in hotel_options:
        if isinstance(h, dict):
            h_copy = dict(h)
            price_orig = h_copy.get("price_per_night") or h_copy.get("price")
            if price_orig:
                h_copy["price_per_night_in_inr"] = convert_price_to_inr(price_orig)
            else:
                h_copy["price_per_night_in_inr"] = None
            normalized.append(h_copy)
        elif isinstance(h, str):
            normalized.append({"name": h})
        else:
            try:
                normalized.append(dict(h))
            except Exception:
                normalized.append({"name": str(h)})
    return {"hotel_options": normalized, "current_step": ["hotels_complete"], "progress": 10}

# ----------------------------
# Itinerary agent (unchanged)
# ----------------------------
def itinerary_agent(state: TripPlannerState) -> Dict[str, Any]:
    """Generate final comprehensive itinerary using Gemini LLM (gemini-2.5-flash)."""
    try:
        st.write("📝 Generating your personalized itinerary...")
        # Calculate trip duration
        start = datetime.strptime(state["start_date"], "%Y-%m-%d")
        end = datetime.strptime(state["end_date"], "%Y-%m-%d")
        duration = max(1, (end - start).days)

        prompt = f"""
        Create a detailed {duration}-day trip itinerary for {state["destination"]}.

        TRIP DETAILS:
        - Destination: {state["destination"]}
        - Dates: {state["start_date"]} to {state["end_date"]} ({duration} days)
        - Budget: ${state["budget"]} for {state["num_travelers"]} travelers
        - Interests: {", ".join(state["interests"])}

        AVAILABLE DATA:

        Destination Info: {json.dumps(state.get("destination_info", {}))}

        Top Places: {json.dumps([p.get("name", "") + " (Rating: " + str(p.get("rating", "N/A")) + ")" for p in state.get("places_info", [])[:5]])}

        Flight Options: {json.dumps(state.get("flight_options", []))}

        Hotel Options: {json.dumps(state.get("hotel_options", []))}

        Activities Available: {json.dumps([a.get("title", "") + " - " + a.get("category", "") for a in state.get("activities", [])[:6]])}

        Weather: {json.dumps(state.get("weather_info", {}))}

        CREATE A COMPREHENSIVE ITINERARY WITH:

        ## 🗓️ TRIP OVERVIEW
        - Destination summary
        - Duration and dates
        - Budget breakdown

        ## ✈️ FLIGHTS & ARRIVAL
        - Recommended flight options (from the search results)
        - Airport transfer tips

        ## 🏨 ACCOMMODATION
        - Top 3 hotel recommendations (from search results with prices)
        - Area recommendations

        ## 📅 DAY-BY-DAY ITINERARY
        For each day, include:
        - Morning activity (specific place/attraction)
        - Afternoon activity
        - Evening suggestion
        - Estimated costs
        - Transportation tips

        ## 🎯 MUST-DO ACTIVITIES
        - Top attractions (from places found)
        - Experience recommendations
        - Booking tips

        ## 💰 BUDGET BREAKDOWN
        - Flights: estimated cost
        - Hotels: per night cost
        - Activities: daily budget
        - Food: daily budget
        - Transport: local transport

        ## 📋 PRACTICAL TIPS
        - Local customs
        - Transportation
        - Safety tips
        - Emergency contacts

        Make it engaging, practical, and well-formatted with emojis and clear sections.
        """

        if llm:
            response = retry_api_call(lambda: llm.invoke(prompt).content, attempts=2, backoff=0.5)
            content = getattr(response, "content", response) if hasattr(response, "content") else response
            # ensure string
            content = content if isinstance(content, str) else str(content)
        else:
            md = f"# {state['destination']} — {duration}-day sample itinerary\n\n"
            md += f"**Dates:** {state.get('start_date')} to {state.get('end_date')}\n\n"
            md += "**Places to visit:**\n"
            for p in state.get("places_info", []):
                md += f"- {p.get('name')}\n"
            content = md

        return {"final_itinerary": content, "current_step": ["complete"], "progress": 15}
    except Exception as e:
        return {"final_itinerary": f"Error generating itinerary: {str(e)}", "error_messages": [f"Itinerary error: {str(e)}"]}

# ----------------------------
# Helper: flights list -> Markdown table (for PDF/itinerary)
# ----------------------------
def flights_to_markdown_table(flights: List[Dict[str, Any]]) -> str:
    if not flights:
        return "No flight options available."
    headers = ["Airline", "Price (INR)", "Duration", "Stops", "Departure Airport", "Departure Time", "Arrival Airport", "Arrival Time", "Booking Link"]
    md = "| " + " | ".join(headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for f in flights:
        price_inr = f.get("price_in_inr") or convert_price_to_inr(f.get("price"))
        row = [
            str(f.get("airline", "Unknown")),
            str(price_inr),
            str(f.get("duration", "N/A")),
            str(f.get("stops", "N/A")),
            str(f.get("departure_airport", "")),
            str(f.get("departure_time") or "N/A"),
            str(f.get("arrival_airport", "")),
            str(f.get("arrival_time") or "N/A"),
            str(f.get("booking_link") or "N/A")
        ]
        # escape pipes
        row = [c.replace("|", "\\|") for c in row]
        md += "| " + " | ".join(row) + " |\n"
    return md
# ----------------------------
# Helper: insert flights markdown table right after the FLIGHTS & ARRIVAL header
# ----------------------------
def insert_flights_table_into_itinerary(itinerary_md: str, flights_md: str) -> str:
    """
    Insert flights_md immediately after the 'FLIGHTS & ARRIVAL' header in itinerary_md.
    - Removes any existing 'Flight Options' section to avoid duplicates.
    - Matches both '✈️' and plain '✈' variants and basic spacing.
    """
    if not itinerary_md:
        return "## ✈️ FLIGHTS & ARRIVAL\n\n" + flights_md

    text = itinerary_md

    # Remove any previously appended "Flight Options" block (common patterns)
    text = re.sub(r"(?is)\n##+\s*✈(?:️)?\s*Flight Options\b.*$", "", text).rstrip()

    # Pattern to find the FLIGHTS & ARRIVAL header (gemini uses various emoji forms)
    header_pattern = re.compile(r"(?m)(^(?:#{1,6}\s*)?(?:✈️|✈)\s*FLIGHTS\s*&\s*ARRIVAL[^\n]*\n?)", re.IGNORECASE)

    m = header_pattern.search(text)
    if m:
        insert_pos = m.end()
        # Insert with spacing so it renders after the header
        new_text = text[:insert_pos] + "\n\n" + "## ✈️ Flight Options\n\n" + flights_md + "\n\n" + text[insert_pos:]
        return new_text
    else:
        # fallback: insert after first top-level title or at top
        title_match = re.search(r"(?m)^\s*#\s.*\n", text)
        if title_match:
            pos = title_match.end()
            new_text = text[:pos] + "\n\n## ✈️ FLIGHTS & ARRIVAL\n\n" + "## ✈️ Flight Options\n\n" + flights_md + "\n\n" + text[pos:]
            return new_text
        # final fallback: append at top
        return "## ✈️ FLIGHTS & ARRIVAL\n\n" + "## ✈️ Flight Options\n\n" + flights_md + "\n\n" + text

# ----------------------------
# PDF & Email functions (kept, but allow receiving combined markdown)
# ----------------------------
def generate_pdf(itinerary_text: str, destination: str) -> BytesIO:
    sanitized = sanitize_text_for_pdf(itinerary_text)
    buf = BytesIO()
    if 'MarkdownPdf' in globals() and MarkdownPdf:
        try:
            pdf = MarkdownPdf(toc_level=0)
            pdf.add_section(Section(f"# Trip Itinerary: {destination}\n\n{sanitized}"))
            pdf.save(buf)
            buf.seek(0)
            return buf
        except Exception:
            pass
    buf.write((f"Trip Itinerary: {destination}\n\n" + sanitized).encode("utf-8"))
    buf.seek(0)
    return buf

def send_email_with_pdf(recipient: str, pdf_buffer: BytesIO, destination: str) -> bool:
    sender = st.secrets.get("gmail_user", "") or st.secrets.get("GMAIL_USER", "")
    password = st.secrets.get("gmail_app_password", "") or st.secrets.get("GMAIL_APP_PASSWORD", "")
    if not sender or not password:
        st.error("Email credentials missing (GMAIL_USER, GMAIL_APP_PASSWORD).")
        return False
    try:
        pdf_buffer.seek(0)
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = recipient
        msg["Subject"] = f"Your Trip Itinerary for {destination}"
        body = f"Hello,\n\nAttached is your itinerary for {destination}.\n\nSafe travels!"
        msg.attach(MIMEText(body, "plain"))

        part = MIMEBase("application", "octet-stream")
        part.set_payload(pdf_buffer.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{destination.replace(" ", "_")}_itinerary.pdf"')
        msg.attach(part)

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        return True
    except Exception as e:
        st.error(f"Email send failed: {e}")
        return False

# ----------------------------
# Workflow orchestration (LangGraph)
# ----------------------------
def create_workflow():
    workflow = StateGraph(TripPlannerState)
    workflow.add_node("research", research_agent)
    workflow.add_node("places", places_agent)
    workflow.add_node("weather", weather_agent)
    workflow.add_node("flights", flights_agent)
    workflow.add_node("hotels", hotels_agent)
    workflow.add_node("activities", activities_agent)
    workflow.add_node("itinerary", itinerary_agent)
    workflow.add_node("sync_gate", lambda s: s)

    workflow.set_entry_point("research")
    workflow.add_edge("research", "places")
    workflow.add_edge("places", "flights")
    workflow.add_edge("places", "hotels")
    workflow.add_edge("places", "weather")
    workflow.add_edge("flights", "sync_gate")
    workflow.add_edge("hotels", "sync_gate")
    workflow.add_edge("weather", "sync_gate")

    def _barrier_condition(state: TripPlannerState) -> str:
        steps = set(state.get("current_step", []))
        required = {"flights_complete", "hotels_complete", "weather_complete"}
        return "go" if required.issubset(steps) else "wait"

    workflow.add_conditional_edges("sync_gate", _barrier_condition, {"go": "activities", "wait": END})
    workflow.add_edge("activities", "itinerary")
    workflow.add_edge("itinerary", END)
    return workflow.compile()

# ----------------------------
# Streamlit UI (main)
# - uses replace_usd_with_inr before display and PDF generation
# ----------------------------
def main():
    st.title("🌍 AI Multi-Agent Trip Planner")
    st.markdown("Plan your perfect trip with AI-powered research, real-time search, and beautiful itineraries!")

    with st.sidebar:
        st.header("🔑 Configuration")
        if llm and tavily_client:
            st.success("✅ All APIs connected!")
        else:
            st.info("Populate secrets: google_api_key (Gemini), tavily_api_key (optional), serpapi_api_key (required for flights)")
        st.markdown("---")
        
        if st.button("🗑️ Clear Trip Cache"):
            for k in list(st.session_state.keys()):
                if k.startswith(("trip_", "pdf_")):
                    del st.session_state[k]
            st.experimental_rerun()

    with st.form("enhanced_trip_form"):
        col1, col2 = st.columns(2)
        with col1:
            start_location = st.text_input("🛫 Start Location / Departure City", placeholder="e.g., Mumbai, India")
            destination = st.text_input("🏙️ Destination", placeholder="e.g., Tokyo, Japan")
            start_date = st.date_input("📅 Start Date", datetime.now() + timedelta(days=7))
            budget = st.number_input("💰 Budget (USD)", min_value=500, max_value=50000, value=3000, step=100)
        with col2:
            end_date = st.date_input("📅 End Date", datetime.now() + timedelta(days=14))
            num_travelers = st.number_input("👥 Travelers", min_value=1, max_value=10, value=2)
            interests = st.multiselect("🎯 Your Interests", ["Culture", "Food", "Adventure", "Museums", "Nature", "Shopping", "Nightlife", "History", "Art", "Music", "Architecture", "Local Experiences"], default=["Culture", "Food"])
        submitted = st.form_submit_button("🚀 Plan My Amazing Trip!", use_container_width=True)

    trip_key = None
    if destination:
        trip_key = f"trip_{destination}_{start_date}_{end_date}_{budget}_{num_travelers}"

    if submitted:
        if not start_location or not destination:
            st.error("Please enter both start location and destination.")
            st.stop()
        if start_date >= end_date:
            st.error("End date must be after start date.")
            st.stop()
        if not interests:
            st.error("Select at least one interest.")
            st.stop()

        if trip_key and trip_key in st.session_state:
            st.info("Using cached plan")
            final_state = st.session_state[trip_key]
        else:
            initial_state = TripPlannerState(
                destination=destination,
                start_location=start_location,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                budget=budget,
                num_travelers=num_travelers,
                interests=interests,
                destination_info={},
                destination_image=None,
                places_info=[],
                flight_options=[],
                hotel_options=[],
                activities=[],
                weather_info={},
                final_itinerary="",
                current_step=["starting"],
                error_messages=[],
                progress=0
            )

            with st.spinner("Planning your trip..."):
                try:
                    app = create_workflow()
                    final_state = app.invoke(initial_state)
                    if trip_key:
                        st.session_state[trip_key] = final_state
                except Exception as e:
                    st.error(f"❌ Workflow execution error: {str(e)}")
                    st.info("Please check your API keys and internet connection.")
                    return

        # Display results
        st.markdown("## 📋 Itinerary")
        if final_state.get("final_itinerary"):
            # Replace USD mentions with INR in the itinerary text before display
            processed_itinerary = replace_usd_with_inr(final_state["final_itinerary"])
            if final_state.get("destination_image"):
                st.image(final_state["destination_image"], caption=f"{destination}", use_column_width=True)
            st.markdown(processed_itinerary)
        else:
            st.info("No itinerary generated.")

        # Activities (kept)
        st.markdown("### 🎯 Activities")
        for a in final_state.get("activities", [])[:6]:
            st.write(f"- {a.get('title')} ({a.get('url','')})")

        # FLIGHTS & ARRIVAL — SHOW RECOMMENDED BLOCK + TRANSFER TIPS + TABLE
        st.markdown("## ✈️ FLIGHTS & ARRIVAL")
        flights = final_state.get("flight_options", []) or []
        if flights:
            # recommended flight block (first option)
            recommended = flights[0] if flights else None
            if recommended:
                st.markdown("### Recommended Flight Option")
                airline = recommended.get("airline", "Unknown")
                price_inr = recommended.get("price_in_inr") or convert_price_to_inr(recommended.get("price"))
                duration = recommended.get("duration", "N/A")
                dep_ap = recommended.get("departure_airport", "")
                arr_ap = recommended.get("arrival_airport", "")
                dep_time = recommended.get("departure_time") or "N/A"
                arr_time = recommended.get("arrival_time") or "N/A"
                booking = recommended.get("booking_link") or "(Not provided)"
                # nicely formatted bullet list for recommended flight
                st.markdown(f"- **Airline:** {airline}")
                st.markdown(f"- **Price:** {price_inr}")
                st.markdown(f"- **Duration:** {duration}")
                st.markdown(f"- **Stops:** {recommended.get('stops','N/A')}")
                st.markdown(f"- **Departure Airport:** {dep_ap} at {dep_time}")
                st.markdown(f"- **Arrival Airport:** {arr_ap} at {arr_time}")
                st.markdown(f"- **Booking Link:** {booking}")

                # Airport Transfer Tips (kept)
                st.markdown("**Airport Transfer Tips:**")
                st.markdown(
                    f"- Upon arrival at {arr_ap}:  \n"
                    f"  - Pre-booked Taxi: Many hotels offer airport pick-up services.  \n"
                    f"  - Ride-Sharing Apps: Uber and Ola (where available).  \n"
                    f"  - Prepaid Taxi Counters: Fixed fares available inside the terminal.  \n"
                )
                st.markdown(
                    "- Journey Time: Expect city-center transfer times to vary by traffic (commonly 20-60 minutes)."
                )

            # TABLE: all flight options (INR prices)
            import pandas as pd
            df_rows = []
            for f in flights:
                price_inr = f.get("price_in_inr") or convert_price_to_inr(f.get("price"))
                df_rows.append({
                    "Airline": f.get("airline", "Unknown"),
                    "Price (INR)": price_inr,
                    "Duration": f.get("duration", "N/A"),
                    "Stops": f.get("stops", "N/A"),
                    "Departure Airport": f.get("departure_airport", ""),
                    "Departure Time": f.get("departure_time") or "N/A",
                    "Arrival Airport": f.get("arrival_airport", ""),
                    "Arrival Time": f.get("arrival_time") or "N/A",
                    "Booking Link": f.get("booking_link", "") or "N/A"
                })
            st.dataframe(pd.DataFrame(df_rows), use_container_width=True)
        else:
            st.info("No flight options found.")

        # ALSO include the flights table inside the itinerary PDF: combine processed_itinerary + flights table markdown
        if final_state.get("final_itinerary"):
            flights_md = flights_to_markdown_table(flights)
            # Use processed itinerary (USD->INR replaced) for the PDF
            processed_itinerary = replace_usd_with_inr(final_state["final_itinerary"])
            # Insert the flights table right after the FLIGHTS & ARRIVAL header (or fallback)
            combined_itinerary_md = insert_flights_table_into_itinerary(processed_itinerary, flights_md)


            pdf_key = f"pdf_{trip_key}"
            if pdf_key not in st.session_state:
                st.session_state[pdf_key] = generate_pdf(combined_itinerary_md, final_state.get("destination", "trip"))
            pdf_buf = st.session_state[pdf_key]

            dl_col, email_col = st.columns(2)
            with dl_col:
                st.download_button("📄 Download PDF Itinerary", data=pdf_buf.getvalue(), file_name=f"{final_state.get('destination','trip').replace(' ', '_')}_itinerary.pdf", mime="application/pdf")
                st.download_button("📝 Download Text", data=combined_itinerary_md, file_name=f"{final_state.get('destination','trip').replace(' ','_')}_itinerary.txt", mime="text/plain")
            with email_col:
                recipient = st.text_input("Email itinerary to (optional)", "")
                if st.button("✉️ Send Email") and recipient:
                    ok = send_email_with_pdf(recipient, pdf_buf, final_state.get("destination", "trip"))
                    if ok:
                        st.success("Email sent!")
                    else:
                        st.error("Email failed. Please check credentials.")

    # Note: Destination Info JSON and the Hotels summary block at the end have been removed per request.

if __name__ == "__main__":
    main()
