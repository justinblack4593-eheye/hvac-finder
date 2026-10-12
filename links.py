import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

url = "https://bearcreektownshipmi.gov/planning-commission-meeting-minutes/"
soup = BeautifulSoup(requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text, "html.parser")
for a in soup.find_all("a", href=True):
    t = a.get_text(" ", strip=True)
    if any(w in t.lower() for w in ("click", "document", "agenda")):
        print(t, "->", urljoin(url, a["href"]))