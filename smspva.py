"""
SMSPVA client.
- Api:       Activation V2 (https://api.smspva.com, header `apikey`, key >= 30 ký tự).
             Docs: https://docs.smspva.com/en/docs/api/
- LegacyApi: API cũ priemnik.php (nhận cả key ngắn kiểu cũ). Cùng interface, trả về Resp dạng V2.

Thread-safe: nhiều luồng gọi song song, các request bắt đầu cách nhau >= interval,
tự giãn khi bị rate limit (402 RATELIMIT_EXCEED / 411 ACCESS_LIMITED).
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_URL = "https://api.smspva.com"
LEGACY_URL = "https://smspva.com/priemnik.php"
LEGACY_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy_data.json")
HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(HERE, "orders.jsonl")
LEGACY_SERVICE = "opt132"  # bản ghi cũ (chỉ có ChatGPT) không lưu service

log = print  # web.py sẽ thay bằng logger của nó


class Resp:
    __slots__ = ("status", "data", "etype", "edesc", "raw")

    def __init__(self, status, data=None, etype=None, edesc=None, raw=None):
        self.status, self.data, self.etype, self.edesc, self.raw = status, data, etype, edesc, raw

    @property
    def ok(self):
        return self.status == 200

    def err(self):
        return f"{self.status} {self.etype or ''} {self.edesc or ''}".strip()


class Api:
    mode = "v2"

    def __init__(self, key, interval=0.1):
        self.key = key
        self.interval = self._base = interval
        self._lock = threading.Lock()
        self._next = 0.0

    # ---------- rate limit ----------
    def _slot(self):
        with self._lock:
            now = time.time()
            t = max(now, self._next)
            self._next = t + self.interval
        if t > now:
            time.sleep(t - now)

    def _backoff(self, sec):
        with self._lock:
            now = time.time()
            if self._next > now + 1:  # đang tạm dừng rồi
                return
            self._next = now + sec
            self.interval = min(max(self.interval * 2, 0.3), 3.0)
        log(f"! SMSPVA giới hạn request, tạm dừng {sec}s (giãn {self.interval:.2f}s/req)")

    def _recover(self):
        if self.interval > self._base:
            with self._lock:
                self.interval = max(self._base, self.interval * 0.9)

    # ---------- core ----------
    def request(self, method, path, params=None, retry=True):
        url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(4):
            self._slot()
            req = urllib.request.Request(url, method=method, data=b"" if method == "PUT" else None, headers={
                "apikey": self.key, "Accept": "application/json", "User-Agent": "smspva-studio/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    http, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                http, body = e.code, e.read()
            except Exception as e:
                if not retry or attempt == 3:
                    return Resp(0, None, "NETWORK", str(e))
                time.sleep(2 * (attempt + 1))
                continue
            try:
                j = json.loads(body)
            except ValueError:
                j = None
            if not isinstance(j, dict):
                return Resp(http, None, "BAD_RESPONSE", body[:300].decode("utf-8", "replace"))
            status = int(j.get("statusCode") or http)
            err = j.get("error") if isinstance(j.get("error"), dict) else {}
            if status == 402 or (status == 411 and retry):
                self._backoff(15 if status == 402 else 10)
                if attempt < 3:
                    continue
            if status == 200:
                self._recover()
            return Resp(status, j.get("data"), err.get("type"), err.get("description"), j)
        return Resp(0, None, "RETRY_EXHAUSTED", "")

    # ---------- endpoints ----------
    def balance(self):
        r = self.request("GET", "/activation/balance")
        if not r.ok:
            raise RuntimeError(r.err())
        return float(r.data["balance"])

    def userinfo(self):
        return self.request("GET", "/activation/userinfo")

    def services_prices(self):
        return self.request("GET", "/activation/servicesprices", {"voice": 0})

    def count_numbers(self):
        return self.request("GET", "/activation/countnumbers")

    def service_prices(self, service):
        return self.request("GET", f"/activation/serviceprices/{q(service)}", {"voice": 0})

    def get_number(self, country, service, operator=None):
        path = f"/activation/number/{q(country)}/{q(service)}" + (f"/{q(operator)}" if operator else "")
        return self.request("GET", path, {"voice": 0}, retry=False)  # không retry lệnh mua số

    def get_sms(self, order):
        return self.request("GET", f"/activation/sms/{q(order['id'])}")

    def orders(self):
        return self.request("GET", "/activation/orders")

    def cancel(self, order):
        return self.request("PUT", f"/activation/cancelorder/{q(order['id'])}")

    def block(self, order):
        return self.request("PUT", f"/activation/blocknumber/{q(order['id'])}")


SITE_URL = "https://smspva.com/"
CATALOG_TTL = 600
_catalog = {"ts": 0.0, "data": None}
_catalog_lock = threading.Lock()


def fetch_site_catalog():
    """Lấy danh sách service + country LÚC RUNTIME từ trang chủ smspva.com
    (window.__ORDER_PAGE_STATE__: allServices, allCountries, topServicesOpts — dữ liệu công khai)."""
    req = urllib.request.Request(SITE_URL, headers={"User-Agent": "Mozilla/5.0 (smspva-studio)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    i = page.find("window.__ORDER_PAGE_STATE__")
    if i < 0:
        raise RuntimeError("không thấy __ORDER_PAGE_STATE__ trên smspva.com")
    body = page[i:page.find("</script>", i)]
    dec = json.JSONDecoder()

    def grab(key):
        m = re.search(r"[{,\s]" + key + r"\s*:\s*", body)
        return dec.raw_decode(body[m.end():])[0] if m else None

    services = {}
    for x in grab("allServices") or []:
        code = x.get("service_opt")
        if code and re.fullmatch(r"opt\d+", code):
            name = re.sub(r"^1 (?=OpenAI)", "", (x.get("service_name") or code).strip())
            services[code] = name
    countries = [x.get("shortname") for x in grab("allCountries") or [] if x.get("shortname")]
    if not services:
        raise RuntimeError("smspva.com không trả danh sách service")
    return {"services": [{"code": c, "name": n} for c, n in services.items()],
            "countries": countries, "top": grab("topServicesOpts") or [],
            "source": SITE_URL, "fetched": datetime.now().isoformat(timespec="seconds")}


def legacy_data():
    """Catalog cho chế độ key cũ: runtime từ smspva.com (cache 10 phút),
    lỗi mạng -> dùng bản lưu gần nhất legacy_data.json."""
    with _catalog_lock:
        if _catalog["data"] and time.time() - _catalog["ts"] < CATALOG_TTL:
            return _catalog["data"]
        try:
            d = fetch_site_catalog()
            try:
                with open(LEGACY_DATA, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=0)
            except OSError:
                pass
        except Exception as e:
            log(f"! Không tải được danh sách service từ smspva.com ({e}) — dùng bản lưu gần nhất")
            try:
                with open(LEGACY_DATA, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                d = {"services": [{"code": LEGACY_SERVICE, "name": "OpenAI API (chatGPT, DALL-e 2)"}], "countries": []}
        if "RU" not in d["countries"]:
            d["countries"].append("RU")
        _catalog.update(ts=time.time(), data=d)
        return d


class LegacyApi(Api):
    """API cũ priemnik.php — dùng khi key không được V2 chấp nhận (key kiểu cũ < 30 ký tự).
    Giới hạn: không có endpoint liệt kê giá/kho cho mọi service, không có danh sách order."""
    mode = "legacy"

    def call(self, metod, retry=True, **params):
        params.update(metod=metod, apikey=self.key)
        url = LEGACY_URL + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            self._slot()
            req = urllib.request.Request(url, headers={"User-Agent": "smspva-studio/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    text = r.read().decode("utf-8", "replace").strip()
            except Exception as e:
                if not retry or attempt == 3:
                    return None, f"NETWORK {e}"
                time.sleep(2 * (attempt + 1))
                continue
            try:
                d = json.loads(text)
            except ValueError:
                d = None
            if not isinstance(d, dict):
                return None, text[:300]
            code = str(d.get("response", d.get("responce", "")))
            if code == "5" and attempt < 3:
                self._backoff(15)
                continue
            if code == "7" and metod != "get_number" and attempt < 3:
                self._backoff(5)
                continue
            self._recover()
            return d, code
        return None, "RETRY_EXHAUSTED"

    @staticmethod
    def _fail(d, code):
        if d is None:
            low = (code or "").lower()
            if "key" in low:
                return Resp(401, None, "USER_NOT_FOUND", code)
            if "balance" in low or "money" in low or "fund" in low:
                return Resp(407, None, "LOW_BALANCE", code)
            if low.startswith("network"):
                return Resp(0, None, "NETWORK", code)
            return Resp(500, None, "LEGACY_ERROR", code)
        if code == "6":
            return Resp(411, None, "ACCESS_LIMITED", "karma âm, bị khoá 10 phút", d)
        if code == "7":
            return Resp(411, None, "ACCESS_LIMITED", "quá nhiều order đồng thời", d)
        return Resp(500, None, "LEGACY_ERROR", json.dumps(d, ensure_ascii=False), d)

    def balance(self):
        d, code = self.call("get_balance", service=LEGACY_SERVICE)
        b = to_float(d.get("balance")) if d else None
        if b is None:
            raise RuntimeError(f"legacy: {code if d is None else d}")
        return b

    def userinfo(self):
        d, code = self.call("get_userinfo", service=LEGACY_SERVICE)
        if d and code == "1":
            return Resp(200, {"balance": d.get("balance"), "karma": d.get("karma")}, raw=d)
        return self._fail(d, code)

    def services_prices(self):
        return Resp(404, None, "UNSUPPORTED", "legacy")

    def count_numbers(self):
        return Resp(404, None, "UNSUPPORTED", "legacy")

    def service_prices(self, service, workers=10):
        from concurrent.futures import ThreadPoolExecutor
        ld = legacy_data()
        name = next((s["name"] for s in ld["services"] if s["code"] == service), service)

        def one(c):
            d, _ = self.call("get_count_new", service=service, country=c)
            n = to_int(d.get("online")) if d else 0
            price = None
            if n > 0:
                p, _ = self.call("get_service_price", service=service, country=c)
                price = to_float(p.get("price")) if p else None
            return {"ccode": c, "cname": None, "opers": [{"opcode": "auto", "price": price, "count": n}]}

        with ThreadPoolExecutor(workers) as ex:
            clist = list(ex.map(one, ld["countries"]))
        return Resp(200, {"scode": service, "sname": name, "clist": clist})

    def get_number(self, country, service, operator=None):
        params = {"country": country, "service": service}
        if operator and operator != "auto":
            params["operator"] = operator
        d, code = self.call("get_number", retry=False, **params)
        if d and code == "1" and d.get("number") and str(d.get("id")) not in ("", "-1"):
            return Resp(200, {"orderId": d["id"], "phoneNumber": d["number"], "countryCode": country,
                              "orderExpireIn": 600}, raw=d)
        if d and code == "2":
            return Resp(502, None, "NUMBER_NOT_FOUND", "hết số", d)
        return self._fail(d, code)

    def get_sms(self, order):
        d, code = self.call("get_sms", service=order.get("service") or LEGACY_SERVICE,
                            country=order.get("country"), id=order["id"])
        if d and d.get("sms") not in (None, "", "null") and code != "3":
            return Resp(200, {"sms": {"code": str(d["sms"]), "fullText": str(d.get("text") or d["sms"])}}, raw=d)
        if d and code == "2":
            return Resp(202, None, "SMS_NOT_YET_RECEIVED", "Waiting for SMS", d)
        if d and code == "3":
            return Resp(410, None, "ORDER_CLOSED", "hết hạn / không có SMS", d)
        return self._fail(d, code)

    def orders(self):
        return Resp(404, None, "UNSUPPORTED", "legacy")

    def cancel(self, order):
        d, code = self.call("denial", service=order.get("service") or LEGACY_SERVICE,
                            country=order.get("country"), id=order["id"])
        return Resp(200, {"orderId": order["id"]}, raw=d) if d and code == "1" else self._fail(d, code)

    def block(self, order):
        d, code = self.call("ban", service=order.get("service") or LEGACY_SERVICE, id=order["id"])
        return Resp(200, {"orderId": order["id"]}, raw=d) if d and code == "1" else self._fail(d, code)


def q(x):
    return urllib.parse.quote(str(x), safe="")


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_code(code, full):
    """Ưu tiên data.sms.code; nếu code rỗng/không phải số thì tách 4-8 chữ số từ fullText."""
    code = (str(code).strip() if code not in (None, "null") else "")
    full = (str(full).strip() if full not in (None, "null") else "")
    if re.fullmatch(r"[0-9A-Za-z-]{3,10}", code):
        return code
    m = re.search(r"\b\d{4,8}\b", full) or re.search(r"\b\d{4,8}\b", code)
    return m.group(0) if m else (code or full)


# ---------- lịch sử ----------
_file_lock = threading.Lock()


def save(record):
    record = {**record, "time": datetime.now().isoformat(timespec="seconds")}
    with _file_lock, open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_history():
    rows = []
    if os.path.exists(RESULT_FILE):
        with _file_lock, open(RESULT_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def stats(rows=None):
    """{(service, country): [ok, fail]}.
    ok = nhận được SMS; fail = hết hạn / bạn tự huỷ 1 số / block số.
    Bị huỷ tự động vì số khác trong race đã nhận SMS -> không tính."""
    final = {}
    for r in rows if rows is not None else read_history():
        key = (r.get("id"), r.get("service") or LEGACY_SERVICE, r.get("country"))
        ev = r.get("event")
        if ev == "sms":
            final[key] = "ok"
        elif final.get(key) != "ok":
            if ev in ("expired", "blocked") or (ev == "cancelled" and r.get("reason") == "manual"):
                final[key] = "fail"
    out = {}
    for (_, s, c), res in final.items():
        v = out.setdefault((s, c), [0, 0])
        v[0 if res == "ok" else 1] += 1
    return out


def recent_codes(rows=None, limit=30):
    res = [r for r in (rows if rows is not None else read_history()) if r.get("event") == "sms"]
    return res[-limit:][::-1]


# ---------- country ----------
# code SMSPVA (UK thay vì GB) : tên : mã vùng
_C = """AF:Afghanistan:93 AL:Albania:355 DZ:Algeria:213 AO:Angola:244 AR:Argentina:54 AM:Armenia:374
AU:Australia:61 AT:Austria:43 AZ:Azerbaijan:994 BH:Bahrain:973 BD:Bangladesh:880 BY:Belarus:375
BE:Belgium:32 BJ:Benin:229 BO:Bolivia:591 BA:Bosnia:387 BW:Botswana:267 BR:Brazil:55 BN:Brunei:673
BG:Bulgaria:359 BF:Burkina_Faso:226 KH:Cambodia:855 CM:Cameroon:237 CA:Canada:1 CL:Chile:56 CN:China:86
CO:Colombia:57 CD:DR_Congo:243 CG:Congo:242 CR:Costa_Rica:506 CI:Ivory_Coast:225 HR:Croatia:385
CY:Cyprus:357 CZ:Czechia:420 DK:Denmark:45 DO:Dominican_Rep.:1 EC:Ecuador:593 EG:Egypt:20
SV:El_Salvador:503 EE:Estonia:372 ET:Ethiopia:251 FI:Finland:358 FR:France:33 GA:Gabon:241 GM:Gambia:220
GE:Georgia:995 DE:Germany:49 GH:Ghana:233 GR:Greece:30 GT:Guatemala:502 GN:Guinea:224 HT:Haiti:509
HN:Honduras:504 HK:Hong_Kong:852 HU:Hungary:36 IS:Iceland:354 IN:India:91 ID:Indonesia:62 IQ:Iraq:964
IE:Ireland:353 IL:Israel:972 IT:Italy:39 JM:Jamaica:1 JP:Japan:81 JO:Jordan:962 KZ:Kazakhstan:7
KE:Kenya:254 KW:Kuwait:965 KG:Kyrgyzstan:996 LA:Laos:856 LV:Latvia:371 LB:Lebanon:961 LT:Lithuania:370
LU:Luxembourg:352 MO:Macau:853 MK:North_Macedonia:389 MG:Madagascar:261 MW:Malawi:265 MY:Malaysia:60
ML:Mali:223 MT:Malta:356 MX:Mexico:52 MD:Moldova:373 MN:Mongolia:976 ME:Montenegro:382 MA:Morocco:212
MZ:Mozambique:258 MM:Myanmar:95 NA:Namibia:264 NP:Nepal:977 NL:Netherlands:31 NZ:New_Zealand:64
NI:Nicaragua:505 NE:Niger:227 NG:Nigeria:234 NO:Norway:47 OM:Oman:968 PK:Pakistan:92 PA:Panama:507
PG:Papua_New_Guinea:675 PY:Paraguay:595 PE:Peru:51 PH:Philippines:63 PL:Poland:48 PT:Portugal:351
PR:Puerto_Rico:1 QA:Qatar:974 RO:Romania:40 RU:Russia:7 RW:Rwanda:250 SA:Saudi_Arabia:966 SN:Senegal:221
RS:Serbia:381 SL:Sierra_Leone:232 SG:Singapore:65 SK:Slovakia:421 SI:Slovenia:386 ZA:South_Africa:27
KR:South_Korea:82 ES:Spain:34 LK:Sri_Lanka:94 SE:Sweden:46 CH:Switzerland:41 TW:Taiwan:886
TJ:Tajikistan:992 TZ:Tanzania:255 TH:Thailand:66 TG:Togo:228 TT:Trinidad_and_Tobago:1 TN:Tunisia:216
TR:Turkey:90 TM:Turkmenistan:993 UG:Uganda:256 UA:Ukraine:380 AE:UAE:971 UK:United_Kingdom:44
GB:United_Kingdom:44 US:United_States:1 UY:Uruguay:598 UZ:Uzbekistan:998 VE:Venezuela:58 VN:Vietnam:84
YE:Yemen:967 ZM:Zambia:260 ZW:Zimbabwe:263 GI:Gibraltar:350 TD:Chad:235"""
COUNTRIES = {}
for _item in _C.split():
    _code, _name, _dial = _item.split(":")
    COUNTRIES[_code] = (_name.replace("_", " "), _dial)


def country_name(code, fallback=None):
    return fallback or COUNTRIES.get(code, (code, ""))[0]


def country_dial(code):
    return COUNTRIES.get(code, ("", ""))[1]
