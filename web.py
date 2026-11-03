#!/usr/bin/env python3
"""
SMSPVA Studio — local dashboard.

  python3 web.py          -> mở http://127.0.0.1:8765, nhập API key

- Danh sách TẤT CẢ service SMSPVA hỗ trợ (giá, số lượng, số country).
- Chọn service -> thanh bên trái là các country còn số (giá, kho, operator, lịch sử OK).
- Order 1 số, hoặc RACE nhiều country: số đầu tiên nhận SMS -> tự huỷ các số còn lại.
- Poll toàn bộ order bằng 1 request /activation/orders mỗi 3s (kể cả order tạo trên web SMSPVA).
Server chỉ nghe 127.0.0.1, API key chỉ giữ trong RAM.
"""
import json
import os
import threading
import time
import traceback
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import smspva as sp

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
INDEX = os.path.join(sp.HERE, "index.html")
SERVICES_TTL = 60      # SMSPVA cache count 60s
SERVICE_TTL = 30
POLL_ACTIVE = 3        # giây giữa các lần poll khi có order đang chờ
POLL_IDLE = 20         # khi không có order (để phát hiện order tạo trên web SMSPVA)
RACE_WORKERS = 5


class Busy(Exception):
    pass


class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.api = None
        self.balance = None
        self.user = {}
        self.services = []
        self.services_ts = 0.0
        self.svc_cache = {}
        self.orders = {}
        self.races = {}  # race_id -> {"service":..., "stop": Event, "winner": id}
        self.logs = deque(maxlen=300)
        self.job = None
        self._seen_raw = {}
        threading.Thread(target=self._poller, daemon=True).start()

    # ---------- tiện ích ----------
    def log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)

    def need_api(self):
        if not self.api:
            raise Busy("Chưa nhập API key")
        return self.api

    def bg(self, name, fn, *args):
        def wrap():
            try:
                fn(*args)
            except Exception as e:
                self.log(f"LỖI {name}: {e}")
                traceback.print_exc()
        threading.Thread(target=wrap, daemon=True).start()

    # ---------- key ----------
    def set_key(self, key):
        key = (key or "").strip()
        if not key:
            raise ValueError("API key trống")
        api = sp.Api(key)
        try:
            bal = api.balance()
        except Exception as e_v2:
            # V2 chỉ nhận key >= 30 ký tự; key kiểu cũ -> thử API cũ priemnik.php
            api = sp.LegacyApi(key)
            try:
                bal = api.balance()
            except Exception as e_old:
                raise ValueError(f"SMSPVA từ chối key ({len(key)} ký tự). API v2: {e_v2} · API cũ: {e_old}")
        with self.lock:
            self.api, self.balance = api, bal
            self.services, self.services_ts, self.svc_cache = [], 0.0, {}
        self.log(f"API key OK ({'API v2' if api.mode == 'v2' else 'API cũ priemnik.php — key kiểu cũ'}) · số dư {bal}")
        self.bg("userinfo", self.refresh_user)
        self.bg("services", self.load_services, True)

    def refresh_user(self):
        api = self.need_api()
        r = api.userinfo()
        if r.ok and isinstance(r.data, dict):
            self.user = r.data
            b = sp.to_float(r.data.get("balance"))
            if b is not None:
                self.balance = b
        else:
            try:
                self.balance = api.balance()
            except Exception:
                pass

    # ---------- services ----------
    def load_services(self, force=False):
        if not force and self.services and time.time() - self.services_ts < SERVICES_TTL:
            return self.services
        api = self.need_api()
        if api.mode == "legacy":  # API cũ: danh sách service lấy runtime từ smspva.com (không có giá/kho tổng)
            cat = sp.legacy_data()
            top = set(cat.get("top") or [])
            out = [{"code": s["code"], "name": s["name"], "countries": None, "min_price": None, "stock": None,
                    "top": s["code"] in top} for s in cat["services"]]
            out.sort(key=lambda x: x["name"].lower())
            with self.lock:
                self.services, self.services_ts = out, time.time()
            return out
        with ThreadPoolExecutor(2) as ex:
            fp, fc = ex.submit(api.services_prices), ex.submit(api.count_numbers)
            rp, rc = fp.result(), fc.result()
        if not rp.ok:
            raise RuntimeError(f"servicesprices lỗi: {rp.err()}")
        names, prices = {}, {}
        for row in rp.data or []:
            s = row.get("service")
            if not s:
                continue
            names[s] = row.get("serviceDescription") or names.get(s) or s
            p = sp.to_float(row.get("price"))
            if p is not None:
                prices.setdefault(s, {})[row.get("country")] = p
        stock, stock_c = {}, {}
        if rc.ok:
            for op in rc.data or []:
                c = op.get("country")
                for sv in op.get("services") or []:
                    t = sp.to_int(sv.get("total"))
                    if t > 0:
                        s = sv.get("service")
                        stock[s] = stock.get(s, 0) + t
                        stock_c.setdefault(s, set()).add(c)
        else:
            self.log(f"countnumbers lỗi: {rc.err()} (vẫn hiện giá)")
        out = []
        for s, name in names.items():
            pc = prices.get(s, {})
            avail = stock_c.get(s, set()) if rc.ok else None
            cands = [p for c, p in pc.items() if avail is None or c in avail] or list(pc.values())
            out.append({"code": s, "name": name,
                        "countries": len(avail) if avail is not None else len(pc),
                        "min_price": min(cands) if cands else None,
                        "stock": stock.get(s, 0) if rc.ok else None})
        out.sort(key=lambda x: x["name"].lower())
        with self.lock:
            self.services, self.services_ts = out, time.time()
        return out

    def service_detail(self, code, force=False):
        hit = self.svc_cache.get(code)
        if hit and not force and time.time() - hit[0] < SERVICE_TTL:
            return hit[1]
        r = self.need_api().service_prices(code)
        if not r.ok or not isinstance(r.data, dict):
            raise RuntimeError(f"serviceprices/{code} lỗi: {r.err()}")
        st = sp.stats()
        countries = []
        for c in r.data.get("clist") or []:
            cc = c.get("ccode")
            opers = sorted(
                [{"op": o.get("opcode"), "price": sp.to_float(o.get("price")), "count": sp.to_int(o.get("count"))}
                 for o in c.get("opers") or []],
                key=lambda o: (o["count"] <= 0, o["price"] if o["price"] is not None else 1e9))
            live = [o for o in opers if o["count"] > 0]
            ok, fail = st.get((code, cc), [0, 0])
            countries.append({
                "code": cc, "name": sp.country_name(cc, c.get("cname")), "dial": sp.country_dial(cc),
                "min_price": min((o["price"] for o in live if o["price"] is not None), default=None),
                "count": sum(min(o["count"], 9999) for o in live),
                "unlimited": any(o["count"] >= 9999 for o in live),
                "opers": opers, "ok": ok, "fail": fail})
        d = {"code": r.data.get("scode") or code, "name": r.data.get("sname") or code,
             "countries": countries, "ts": time.time()}
        self.svc_cache[code] = (time.time(), d)
        return d

    def service_name(self, code):
        for s in self.services:
            if s["code"] == code:
                return s["name"]
        hit = self.svc_cache.get(code)
        return hit[1]["name"] if hit else (code or "?")

    # ---------- order ----------
    def _new_order(self, oid, service, country, number, expire_in, price=None, operator=None,
                   race=None, dial=None, source="dashboard"):
        now = time.time()
        return {"id": str(oid), "service": service, "sname": self.service_name(service),
                "country": country, "cname": sp.country_name(country), "dial": dial or sp.country_dial(country),
                "number": str(number), "price": price, "operator": operator, "race": race,
                "status": "waiting", "note": "", "sms": None, "sms_text": None, "source": source,
                "ordered_at": now, "expires_at": now + (expire_in or 600)}

    def order(self, service, country, operator=None, price=None, race=None):
        api = self.need_api()
        r = api.get_number(country, service, operator)
        if r.ok and isinstance(r.data, dict) and r.data.get("orderId"):
            d = r.data
            o = self._new_order(d["orderId"], service, country, d.get("phoneNumber"),
                                sp.to_int(d.get("orderExpireIn"), 600), price, operator, race)
            with self.lock:
                self.orders[o["id"]] = o
            self.log(f"+ {o['sname']} · {country} +{o['dial']} {o['number']}" + (f" ({price}$)" if price else ""))
            sp.save({"event": "ordered", **o})
            rc = self.races.get(race)
            if rc and rc["stop"].is_set():  # race đã có người thắng trong lúc đang order
                self._cancel(o, "auto")
            return {"ok": True, "order": o}
        if r.status == 0:
            self.log(f"{country}: lỗi mạng khi mua số ({r.edesc}) — sẽ tự kiểm tra danh sách order")
        else:
            self.log(f"{country} {service}: không order được · {r.err()}")
        return {"ok": False, "error": r.err(), "type": r.etype}

    def race(self, service, picks):
        """picks: [{"country":..,"price":..}] — order song song, số đầu tiên nhận SMS thắng."""
        if not picks:
            raise ValueError("Chưa chọn country nào")
        rid = f"r{int(time.time() * 1000)}"
        stop = threading.Event()
        with self.lock:
            if self.job:
                raise Busy(f"Đang {self.job}")
            self.job = "race"
            self.races[rid] = {"service": service, "stop": stop, "winner": None}

        def run():
            try:
                self.log(f"⚡ Race {self.service_name(service)}: {len(picks)} country")
                bal = self.balance or 0
                todo = [p for p in picks if not p.get("price") or not bal or p["price"] <= bal]
                with ThreadPoolExecutor(RACE_WORKERS) as ex:
                    res = list(ex.map(lambda p: self._race_one(service, p, rid, stop), todo))
                n = sum(1 for x in res if x and x.get("ok"))
                self.log(f"⚡ Race: order được {n}/{len(todo)} số · chỉ trả tiền số nhận SMS")
                self.refresh_user()
            except Exception as e:
                self.log(f"LỖI race: {e}")
            finally:
                with self.lock:
                    self.job = None

        threading.Thread(target=run, daemon=True).start()
        return rid

    def _race_one(self, service, p, rid, stop):
        if stop.is_set():
            return None
        return self.order(service, p["country"], None, p.get("price"), rid)

    # ---------- huỷ / block ----------
    def _cancel(self, o, reason="manual"):
        if o["status"] != "waiting":
            return
        o["status"] = "cancelling"
        r = self.need_api().cancel(o)
        if r.ok:
            o["status"] = "cancelled"
            sp.save({"event": "cancelled", "reason": reason, **o})
        else:
            o["status"] = "waiting"
            o["note"] = f"huỷ lỗi: {r.err()}"
            self.log(f"✗ huỷ {o['country']} {o['number']}: {r.err()}")

    def cancel(self, oid):
        o = self.orders.get(oid)
        if o:
            self._cancel(o, "manual")

    def block(self, oid):
        o = self.orders.get(oid)
        if not o or o["status"] != "waiting":
            return
        r = self.need_api().block(o)
        if r.ok:
            o["status"] = "blocked"
            sp.save({"event": "blocked", **o})
            self.log(f"⛔ block {o['country']} {o['number']}")
        else:
            o["note"] = f"block lỗi: {r.err()}"

    def cancel_all(self):
        for rc in self.races.values():
            rc["stop"].set()
        lst = [o for o in self.orders.values() if o["status"] == "waiting"]
        with ThreadPoolExecutor(RACE_WORKERS) as ex:
            list(ex.map(lambda o: self._cancel(o, "manual_all"), lst))
        if lst:
            self.log(f"Đã huỷ {len(lst)} số")

    def clear_done(self):
        with self.lock:
            self.orders = {k: o for k, o in self.orders.items() if o["status"] in ("waiting", "cancelling")}

    # ---------- nhận SMS ----------
    def _got_sms(self, o, code, full):
        if o["status"] != "waiting":
            return
        o.update(status="sms", sms=sp.extract_code(code, full), sms_text=full or code, sms_at=time.time(), note="")
        self.log(f"✅ SMS {o['sname']} · {o['country']} +{o['dial']} {o['number']}: {o['sms']}")
        sp.save({"event": "sms", **o})
        rc = self.races.get(o.get("race"))
        if rc and not rc["winner"]:
            rc["winner"] = o["id"]
            rc["stop"].set()
            others = [x for x in self.orders.values() if x.get("race") == o["race"] and x["status"] == "waiting"]
            if others:
                self.log(f"Race thắng → huỷ {len(others)} số còn lại")
                with ThreadPoolExecutor(RACE_WORKERS) as ex:
                    list(ex.map(lambda x: self._cancel(x, "auto"), others))
        self.bg("balance", self.refresh_user)

    def _read_sms(self, o):
        r = self.api.get_sms(o)
        key = (r.status, r.etype)
        if self._seen_raw.get(o["id"]) != key and r.status not in (200, 202):
            self._seen_raw[o["id"]] = key
            self.log(f"  {o['country']} {o['number']}: /sms → {r.err()}")
        if r.ok and isinstance(r.data, dict):
            sms = r.data.get("sms") or {}
            exp = r.data.get("orderExpireIn")
            if exp is not None:
                o["expires_at"] = time.time() + sp.to_int(exp)
            if sms.get("code") or sms.get("fullText"):
                self._got_sms(o, sms.get("code"), sms.get("fullText"))
        elif r.status == 410:
            o["status"] = "expired"
            sp.save({"event": "expired", **o})
        elif r.status == 407:
            o["note"] = "SMS đã về nhưng số dư không đủ để trả — nạp tiền để xem code"

    def _poller(self):
        last = 0.0
        while True:
            time.sleep(1)
            if not self.api:
                continue
            waiting = [o for o in list(self.orders.values()) if o["status"] == "waiting"]
            if time.time() - last < (POLL_ACTIVE if waiting else POLL_IDLE):
                continue
            last = time.time()
            try:
                self._poll_once(waiting)
            except Exception as e:
                self.log(f"LỖI poll: {e}")
                traceback.print_exc()

    def _poll_once(self, waiting):
        r = self.api.orders()
        listing = None
        if r.ok and isinstance(r.data, dict):
            listing = {str(x.get("orderId")): x for x in r.data.get("orders") or []}
        elif r.ok and isinstance(r.data, list):
            listing = {str(x.get("orderId")): x for x in r.data}
        now = time.time()
        if listing is not None:
            for oid, x in listing.items():  # order tạo ở nơi khác (web SMSPVA, mất kết nối khi mua...)
                if oid not in self.orders:
                    o = self._new_order(oid, x.get("service"), x.get("country"), x.get("phoneNumber"),
                                        sp.to_int(x.get("orderExpireIn"), 600),
                                        dial=str(x.get("countryCode") or "").lstrip("+") or None, source="smspva")
                    with self.lock:
                        self.orders[oid] = o
                    self.log(f"Phát hiện order {o['sname']} {o['country']} {o['number']}")
                    waiting.append(o)
        for o in waiting:
            if o["status"] != "waiting":
                continue
            x = listing.get(o["id"]) if listing is not None else None
            if x is not None:
                exp = x.get("orderExpireIn")
                if exp is not None:
                    o["expires_at"] = now + sp.to_int(exp)
                st = str(x.get("status") or "")
                o["remote"] = st
                sms = x.get("sms") or {}
                if st == "PENDING_PAYMENT":
                    o["note"] = "SMS đã về nhưng số dư không đủ để trả — nạp tiền để xem code"
                if sms.get("code") or sms.get("fullText") or st in ("SMS_READY", "PENDING_PAYMENT"):
                    self._read_sms(o)  # đọc chính thức qua /sms (xác nhận + thanh toán)
            else:
                # không có trong danh sách (vừa tạo, đã đóng, hoặc /orders lỗi) -> hỏi trực tiếp
                self._read_sms(o)
            if o["status"] == "waiting" and now > o["expires_at"] + 20:
                o["status"] = "expired"
                sp.save({"event": "expired", **o})

    # ---------- state ----------
    def state(self):
        now = time.time()
        with self.lock:
            orders = sorted(self.orders.values(), key=lambda o: -o["ordered_at"])
            out = [dict(o, remaining=max(0, int(o["expires_at"] - now))) for o in orders]
        return {"has_key": bool(self.api), "mode": self.api.mode if self.api else None, "balance": self.balance,
                "karma": self.user.get("karma"), "user": self.user.get("name") or self.user.get("login"),
                "job": self.job, "orders": out,
                "waiting": sum(1 for o in out if o["status"] == "waiting"),
                "logs": list(self.logs)[-120:]}


app = App()
sp.log = app.log


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return False
        return not origin or origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def _run(self, fn):
        try:
            return self._send(200, fn())
        except Busy as e:
            return self._send(409, {"error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(502, {"error": str(e)})

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        qs = dict(urllib.parse.parse_qsl(u.query))
        force = qs.get("force") == "1"
        if u.path == "/":
            with open(INDEX, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, app.state())
        if u.path == "/api/services":
            return self._run(lambda: {"services": app.load_services(force), "ts": app.services_ts})
        if u.path == "/api/service":
            return self._run(lambda: app.service_detail(qs["code"], force))
        if u.path == "/api/codes":
            return self._run(lambda: {"codes": sp.recent_codes()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        path = urllib.parse.urlparse(self.path).path
        routes = {
            "/api/key": lambda: app.set_key(b.get("key")) or {"ok": True},
            "/api/order": lambda: app.order(b["service"], b["country"], b.get("operator"), b.get("price")),
            "/api/race": lambda: {"race": app.race(b["service"], b["picks"])},
            "/api/cancel": lambda: app.cancel(str(b["id"])) or {"ok": True},
            "/api/block": lambda: app.block(str(b["id"])) or {"ok": True},
            "/api/cancel-all": lambda: app.cancel_all() or {"ok": True},
            "/api/clear": lambda: app.clear_done() or {"ok": True},
            "/api/refresh-user": lambda: app.refresh_user() or {"ok": True},
        }
        fn = routes.get(path)
        if not fn:
            return self._send(404, {"error": "not found"})
        if path != "/api/key" and not app.api:
            return self._send(400, {"error": "Chưa nhập API key"})
        self._run(fn)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Mở trình duyệt: http://{HOST}:{PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
        if app.api:
            app.cancel_all()


if __name__ == "__main__":
    main()
